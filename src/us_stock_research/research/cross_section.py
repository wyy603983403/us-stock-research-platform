"""Point-in-time S&P 500 cross-section backtest (read-only; no pydantic, runs anywhere).

At every month end the universe is the index membership *on that day* (``meta/sp500_history``),
priced from Yahoo daily bars or, for former members, ``daily_delisted``. A stock is eligible if it
has enough price history for the signal. The top ``top_n`` by the signal are held equal-weight for
one month; trades fill ``execution_lag_days`` after the signal. A holding whose price series ends
inside the month is sold at its last close and the proceeds earn zero; a haircut on those exits
can be applied as a sensitivity. The primary benchmark is the equal-weight portfolio of the same
eligible universe with the same costs, so the comparison isolates the signal.

The contract (``kind: cross_section``) is pre-registered; this module never changes parameters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.calendar import trading_days
from us_stock_research.config import load_settings
from us_stock_research.quality.ohlcv import load_exceptions
from us_stock_research.research.stats import block_bootstrap
from us_stock_research.research.trials import record_and_assess
from us_stock_research.tables import TableStore

MAX_STALE_DAYS = 5  # a signal price may come from up to 5 trading days earlier (halts, holidays)
SIGNALS = ("momentum_12_1", "low_volatility")
REQUIRED = ("name", "universe", "signal", "selection", "execution_lag_days", "inference")


def load_xs_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text())
    if raw.get("kind") != "cross_section":
        raise ValueError(f"{path} is not a cross_section contract")
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"{path} lacks {missing}")
    if raw["signal"]["name"] not in SIGNALS:
        raise ValueError(f"unknown signal {raw['signal']['name']!r}; known: {SIGNALS}")
    for key in ("start", "end"):
        value = raw["universe"][key]
        raw["universe"][key] = value if isinstance(value, date) else date.fromisoformat(value)
    return raw


@dataclass
class Prices:
    days: list[date]
    series: dict[str, list[float | None]]  # adj_close aligned to ``days``; None = no price

    def at(self, symbol: str, i: int, stale: int = 0) -> tuple[int, float] | None:
        """Price at ``i`` or, with ``stale`` > 0, the latest one up to ``stale`` days earlier."""
        s = self.series.get(symbol)
        if s is None:
            return None
        for j in range(i, max(i - stale, 0) - 1, -1):
            v = s[j]
            if v is not None:
                return j, v
        return None


Interval = tuple[str, date, date | None]


def members_on(history: list[Interval], day: date) -> set[str]:
    return {s for s, a, b in history if a <= day and (b is None or day < b)}


def month_end_indices(days: list[date], start: date, end: date) -> list[int]:
    out = []
    for i, d in enumerate(days):
        if start <= d <= end and (i + 1 == len(days) or days[i + 1].month != d.month):
            out.append(i)
    return out


def signal_value(prices: Prices, symbol: str, i: int, sig: dict[str, Any]) -> float | None:
    lookback, skip = int(sig["lookback_days"]), int(sig.get("skip_days", 0))
    if sig["name"] == "momentum_12_1":
        recent = prices.at(symbol, i - skip, MAX_STALE_DAYS)
        past = prices.at(symbol, i - lookback, MAX_STALE_DAYS)
        if not recent or not past or past[1] <= 0:
            return None
        return recent[1] / past[1] - 1
    # low_volatility: stdev of daily returns over the lookback, lower is better (negated below)
    s = prices.series.get(symbol)
    if s is None or i - lookback < 0:
        return None
    rets = [
        s[j] / s[j - 1] - 1  # type: ignore[operator]
        for j in range(i - lookback + 1, i + 1)
        if s[j] is not None and s[j - 1] is not None and s[j - 1] > 0  # type: ignore[operator]
    ]
    if len(rets) < 0.8 * lookback:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def holding_return(
    prices: Prices, symbol: str, entry: int, exit_: int, haircut: float
) -> tuple[float, bool]:
    """Return from ``entry`` to ``exit_`` and whether the price series ended early.

    Early end (delisting, merger): sell at the last close, hold cash, apply ``haircut``.
    """
    start = prices.at(symbol, entry, MAX_STALE_DAYS)
    if start is None:  # stopped trading right after the signal day
        return haircut, True
    end = prices.at(symbol, exit_, MAX_STALE_DAYS)
    if end is not None:
        return end[1] / start[1] - 1, False
    last = prices.at(symbol, exit_, exit_ - start[0])  # latest close since entry
    r = last[1] / start[1] - 1 if last else 0.0
    return (1 + r) * (1 + haircut) - 1, True


def rebalance_cost(old: dict[str, float], new: dict[str, float], bps: float) -> tuple[float, float]:
    traded = sum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in set(old) | set(new))
    return traded * bps / 10_000, traded / 2


def drift(weights: dict[str, float], rets: dict[str, float]) -> dict[str, float]:
    grown = {s: w * (1 + rets.get(s, 0.0)) for s, w in weights.items()}
    total = sum(grown.values())
    return {s: v / total for s, v in grown.items()} if total > 0 else {}


def run(
    contract: dict[str, Any],
    prices: Prices,
    history: list[Interval],
    *,
    haircut: float = 0.0,
    excluded: set[str] | None = None,
    benchmark_symbol: str = "SPY",
) -> dict[str, Any]:
    uni, sig, sel = contract["universe"], contract["signal"], contract["selection"]
    lag = int(contract["execution_lag_days"])
    bps = float(contract.get("transaction_cost_bps", 0.0))
    need = int(uni["min_history_days"])
    top_n = int(sel["top_n"])
    lower_is_better = sig["name"] == "low_volatility"
    excluded = excluded or set()
    ends = month_end_indices(prices.days, uni["start"], uni["end"])
    held: dict[str, float] = {}
    ew_held: dict[str, float] = {}
    months: list[dict[str, Any]] = []
    for t, t_next in zip(ends, ends[1:], strict=False):
        day = prices.days[t]
        members = members_on(history, day)
        priced = {s for s in members if s not in excluded and prices.at(s, t, MAX_STALE_DAYS)}
        eligible: dict[str, float] = {}
        for s in sorted(priced):
            if prices.at(s, t - need, MAX_STALE_DAYS) is None:
                continue
            v = signal_value(prices, s, t, sig)
            if v is not None:
                eligible[s] = v
        ranked = sorted(
            eligible, key=lambda s: (eligible[s] if lower_is_better else -eligible[s], s)
        )
        picks = ranked[:top_n]
        entry, exit_ = t + lag, min(t_next + lag, len(prices.days) - 1)
        rets: dict[str, float] = {}
        delisted = 0
        for s in eligible:
            r, early = holding_return(prices, s, entry, exit_, haircut)
            rets[s] = r
            delisted += early and s in picks
        target = {s: 1 / len(picks) for s in picks} if picks else {}
        ew_target = {s: 1 / len(eligible) for s in eligible} if eligible else {}
        cost, turnover = rebalance_cost(held, target, bps)
        ew_cost, _ = rebalance_cost(ew_held, ew_target, bps)
        strat = sum(w * rets[s] for s, w in target.items()) - cost
        ew = sum(w * rets[s] for s, w in ew_target.items()) - ew_cost
        bench = None
        if benchmark_symbol in prices.series:
            b0 = prices.at(benchmark_symbol, entry, MAX_STALE_DAYS)
            b1 = prices.at(benchmark_symbol, exit_, MAX_STALE_DAYS)
            bench = b1[1] / b0[1] - 1 if b0 and b1 else None
        held, ew_held = drift(target, rets), drift(ew_target, rets)
        months.append(
            {
                "date": day.isoformat(),
                "members": len(members),
                "priced": len(priced),
                "coverage": len(priced) / len(members) if members else 0.0,
                "eligible": len(eligible),
                "picks": len(picks),
                "strategy": strat,
                "equal_weight": ew,
                "benchmark": bench,
                "turnover_one_way": turnover,
                "early_exits": int(delisted),
                "top": picks[:10],
            }
        )
    return {"months": months}


def monthly_metrics(rets: list[float]) -> dict[str, Any]:
    curve = [1.0]
    for r in rets:
        curve.append(curve[-1] * (1 + r))
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    peak, mdd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rolling = [curve[i] / curve[i - 12] - 1 for i in range(12, len(curve))]
    return {
        "months": n,
        "cagr": curve[-1] ** (12 / n) - 1,
        "annual_volatility": sd * math.sqrt(12),
        "sharpe_rf0": mean / sd * math.sqrt(12) if sd else None,
        "max_drawdown": mdd,
        "worst_rolling_12m_return": min(rolling) if rolling else None,
    }


def summarize(contract: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    months = result["months"]
    strat = [m["strategy"] for m in months]
    ew = [m["equal_weight"] for m in months]
    inf = contract["inference"]
    excess = [a - b for a, b in zip(strat, ew, strict=True)]
    boot = block_bootstrap(
        excess,
        int(inf["resamples"]),
        int(inf["block_size_months"]),
        float(inf["confidence_level"]),
        int(inf["random_seed"]),
    )
    by_year: dict[str, list[float]] = {}
    for m in months:
        by_year.setdefault(m["date"][:4], []).append(m["coverage"])
    bench = [m["benchmark"] for m in months if m["benchmark"] is not None]
    return {
        "strategy": monthly_metrics(strat),
        "equal_weight_universe": monthly_metrics(ew),
        "spy": monthly_metrics(bench) if len(bench) == len(months) else None,
        "excess_vs_equal_weight": boot,
        "avg_turnover_one_way": sum(m["turnover_one_way"] for m in months) / len(months),
        "early_exits_held": sum(m["early_exits"] for m in months),
        "coverage_min": min(m["coverage"] for m in months),
        "coverage_by_year": {y: round(sum(v) / len(v), 4) for y, v in sorted(by_year.items())},
    }


def data_fingerprint(tables: TableStore, kinds: list[str]) -> str:
    """Content id of the price/membership files used: changes whenever any of them changes."""
    digest = hashlib.sha256()
    for kind in kinds:
        for key in tables.keys(kind):
            path = tables.path(kind, key)
            digest.update(f"{kind}/{key}:".encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "xsec-data-v1:sha256:" + digest.hexdigest()


def load_prices(tables: TableStore, start: date, end: date) -> Prices:
    days = trading_days(start, end)
    index = {d: i for i, d in enumerate(days)}
    series: dict[str, list[float | None]] = {}
    for kind in ("daily_delisted", "daily"):  # Yahoo ("daily") wins where both exist
        if not tables.keys(kind):
            continue
        pattern = str(tables.root / "parquet" / kind / "*.parquet").replace("'", "''")
        con = tables._duckdb().connect()  # noqa: SLF001 - bulk read of many files
        try:
            rows = con.execute(
                "SELECT regexp_extract(filename, '([^/]+)\\.parquet$', 1), date, adj_close "
                f"FROM read_parquet('{pattern}', filename=true) "
                f"WHERE date BETWEEN '{start}' AND '{end}' AND adj_close > 0"
            ).fetchall()
        finally:
            con.close()
        fresh: dict[str, list[float | None]] = {}
        for sym, d, v in rows:
            i = index.get(d)
            if i is None:
                continue
            fresh.setdefault(sym, [None] * len(days))[i] = float(v)
        series.update(fresh)
    return Prices(days=days, series=series)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def evaluate(contract: dict[str, Any], summary: dict[str, Any], dsr: float | None) -> list[str]:
    """Pre-registered success criteria -> list of failures (empty = all met)."""
    fails: list[str] = []
    interval = summary["excess_vs_equal_weight"].get("interval")
    if not interval or interval[0] <= 0:
        fails.append("超额收益 95% 置信区间下限不为正")
    if dsr is None or dsr < 0.95:
        fails.append(f"Deflated Sharpe {dsr if dsr is None else round(dsr, 3)} < 0.95")
    worst = summary["strategy"]["worst_rolling_12m_return"]
    if worst is not None and worst < -0.25:
        fails.append(f"最差滚动 12 个月 {worst:.1%}，超过 25% 亏损上限")
    if summary["coverage_min"] < 0.9:
        fails.append(f"最低价格覆盖率 {summary['coverage_min']:.1%} < 90%")
    return fails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    args = parser.parse_args(argv)
    contract = load_xs_contract(args.contract)
    tables = TableStore.from_settings(load_settings())
    uni = contract["universe"]
    history = [
        (s, a, b)
        for s, a, b in tables.read("meta", "sp500_history", "symbol, start_date, end_date")
    ]
    prices = load_prices(
        tables, uni["start"] - timedelta(days=500), uni["end"] + timedelta(days=45)
    )
    quarantined = set(load_exceptions(args.exceptions)[1])
    base = run(contract, prices, history, excluded=quarantined)
    haircut = float(contract.get("delisting", {}).get("haircut_sensitivity", 0.0))
    stressed = run(contract, prices, history, haircut=haircut, excluded=quarantined)
    summary = summarize(contract, base)
    fingerprint = data_fingerprint(tables, ["daily", "daily_delisted", "meta"])
    artifact: dict[str, Any] = {
        "study": contract["name"],
        "strategy": f"xsec_{contract['signal']['name']}",
        "parameters": {
            "signal": contract["signal"],
            "selection": contract["selection"],
            "execution_lag_days": contract["execution_lag_days"],
            "transaction_cost_bps": contract.get("transaction_cost_bps"),
        },
        "snapshot_id": fingerprint,
        "git_sha": _git_sha(),
        "trading_enabled": False,
        "excluded_quarantined": sorted(quarantined),
        "summary": summary,
        "haircut_sensitivity": {"haircut": haircut, "summary": summarize(contract, stressed)},
        "strategy_monthly_returns": [m["strategy"] for m in base["months"]],
        "months": base["months"],
    }
    mt = record_and_assess(args.registry, artifact, ["sp500_point_in_time"])
    artifact["multiple_testing"] = mt
    artifact["failed_criteria"] = evaluate(contract, summary, mt["deflated_sharpe"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    brief = {
        k: summary[k]
        for k in ("strategy", "equal_weight_universe", "spy", "excess_vs_equal_weight")
    }
    brief["coverage_min"] = summary["coverage_min"]
    brief["haircut_strategy_cagr"] = artifact["haircut_sensitivity"]["summary"]["strategy"]["cagr"]
    brief["deflated_sharpe"] = mt["deflated_sharpe"]
    brief["failed_criteria"] = artifact["failed_criteria"]
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
