"""Two-sleeve mix: the approved leveraged-trend strategy + a defensive trend sleeve.

The aggressive sleeve is the ``leveraged_trend`` engine run unchanged on its own contract. The
defensive sleeve holds each asset (e.g. TLT, IEF, GLD) in equal parts while its adjusted close is
above its moving average and T-bills otherwise; signal at close of day t, trade at close of t+1.
Sleeves are combined monthly with month-end rebalancing back to the target weights.
``usr-sleeve-mix`` writes the artifact and registers the trial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.research import leveraged_trend as lt

TRADING_DAYS = 252


def load_mix_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "sleeve_mix":
        raise ValueError(f"{path} is not a sleeve_mix contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        value = raw["data"][key]
        raw["data"][key] = value if isinstance(value, date) else date.fromisoformat(str(value))
    return raw


def asset_trend_daily(
    days: list[date],
    closes: list[float],
    yields_pct: list[float | None],
    sma_days: int,
    cost_bps: float,
    *,
    use_trend: bool = True,
) -> list[tuple[date, float]]:
    """Daily returns of one asset with a moving-average switch into T-bills.

    The state used for the move from day k-1 to k is decided at the close of k-2 (traded at the
    close of k-1). A change of state costs ``cost_bps`` on that day.
    """
    above: list[bool | None] = [None] * len(days)
    for i in range(sma_days - 1, len(closes)):
        above[i] = closes[i] > sum(closes[i - sma_days + 1 : i + 1]) / sma_days
    out: list[tuple[date, float]] = []
    prev: bool | None = None
    for k in range(2, len(days)):
        state = above[k - 2]
        rate = yields_pct[k - 1]
        if state is None or rate is None:
            continue
        on = state if use_trend else True
        ret = closes[k] / closes[k - 1] - 1 if on else rate / 100 / TRADING_DAYS
        if prev is not None and on != prev:
            ret = (1 + ret) * (1 - cost_bps / 10_000) - 1
        prev = on
        out.append((days[k], ret))
    return out


def defensive_monthly(
    series: dict[str, list[tuple[date, float]]], start: date, end: date
) -> dict[str, float]:
    """Equal-weight, monthly-rebalanced sleeve: mean of each asset's compounded monthly return."""
    per_asset: list[dict[str, float]] = []
    for rows in series.values():
        months = dict(lt.monthly([(d, r) for d, r in rows if start <= d <= end]))
        per_asset.append(months)
    common = sorted(set.intersection(*(set(m) for m in per_asset)))
    return {m: sum(a[m] for a in per_asset) / len(per_asset) for m in common}


def combine(
    aggressive: dict[str, float],
    defensive: dict[str, float],
    weight_aggressive: float,
    cost_bps: float,
) -> dict[str, float]:
    """Monthly mix rebalanced to target weights at each month end (cost on the weight drift)."""
    wa, wd = weight_aggressive, 1 - weight_aggressive
    out: dict[str, float] = {}
    for m in sorted(set(aggressive) & set(defensive)):
        a, d = aggressive[m], defensive[m]
        gross = wa * a + wd * d
        drifted = wa * (1 + a) / (1 + gross)
        turnover = 2 * abs(drifted - wa)
        out[m] = (1 + gross) * (1 - turnover * cost_bps / 10_000) - 1
    return out


def sharpe(rets: list[float]) -> float:
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1))
    return 0.0 if sd == 0 else mean / sd * math.sqrt(12)


def paired_sharpe_bootstrap(
    a: list[float], b: list[float], resamples: int, block: int, level: float, seed: int
) -> dict[str, Any]:
    """Moving-block bootstrap of Sharpe(a) - Sharpe(b) on the same resampled months."""
    n = len(a)
    if n != len(b) or n < block * 2:
        return {"error": "bad lengths"}
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(resamples):
        idx: list[int] = []
        while len(idx) < n:
            s = rng.randrange(0, n - block + 1)
            idx.extend(range(s, s + block))
        idx = idx[:n]
        diffs.append(sharpe([a[i] for i in idx]) - sharpe([b[i] for i in idx]))
    diffs.sort()
    return {
        "method": "paired_moving_block_bootstrap_sharpe_difference_v1",
        "months": n,
        "sharpe_difference": sharpe(a) - sharpe(b),
        "interval": [
            diffs[int((1 - level) / 2 * resamples)],
            diffs[min(int((1 + level) / 2 * resamples), resamples - 1)],
        ],
        "confidence_level": level,
        "probability_positive": sum(x > 0 for x in diffs) / resamples,
    }


def load_defensive_inputs(
    store: Any, symbols: list[str], rate: dict[date, float]
) -> dict[str, tuple[list[date], list[float], list[float | None]]]:
    out = {}
    for s in symbols:
        bars = [b for b in store.read_bars(s) if b.adj_close > 0]
        days = [b.day for b in bars]
        out[s] = (days, [b.adj_close for b in bars], lt.forward_fill(days, rate))
    return out


def defensive_series(
    inputs: dict[str, tuple[list[date], list[float], list[float | None]]],
    sma_days: int,
    cost_bps: float,
    *,
    use_trend: bool = True,
) -> dict[str, list[tuple[date, float]]]:
    return {
        s: asset_trend_daily(d, p, y, sma_days, cost_bps, use_trend=use_trend)
        for s, (d, p, y) in inputs.items()
    }


def calendar_years(monthly: dict[str, float]) -> dict[str, float]:
    years: dict[str, float] = {}
    for m, r in sorted(monthly.items()):
        years[m[:4]] = (1 + years.get(m[:4], 0.0)) * (1 + r) - 1
    return years


def evaluate(
    contract: dict[str, Any], test: dict[str, Any], metrics: dict[str, Any], dsr: float | None,
    verified: bool,
) -> list[str]:  # fmt: skip
    fails = []
    interval = test.get("interval")
    if not interval or interval[0] <= 0:
        fails.append("夏普差 95% 置信区间下限不为正")
    cap = float(contract["risk"]["max_worst_12m_loss"])
    worst = metrics["worst_rolling_12m_return"]
    if worst is not None and worst < -cap:
        fails.append(f"最差滚动 12 个月 {worst:.1%}，超过 {cap:.0%} 亏损上限")
    if dsr is None or dsr < 0.95:
        fails.append(f"Deflated Sharpe {dsr if dsr is None else round(dsr, 3)} < 0.95")
    if not verified:
        fails.append("独立实现不一致")
    return fails


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research import verify_mix
    from us_stock_research.research.trials import record_and_assess
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    contract = load_mix_contract(args.contract)
    data, rule, inf = contract["data"], contract["rule"], contract["inference"]
    start, end = data["start"], data["end"]
    settings = load_settings()
    tables = TableStore.from_settings(settings)
    store = open_store(settings)

    vt_contract = lt.load_lt_contract(Path(data["aggressive_sleeve"]))
    days, prices, yields, _ = lt.load_inputs(tables, store, vt_contract)
    vt = lt.run(vt_contract, days, prices, yields, start=start, end=end)
    aggressive = dict(zip(vt["months"], vt["strategy"], strict=True))
    spx = dict(zip(vt["months"], vt["benchmark"], strict=True))

    rows = tables.read("macro", data["cash_rate"], "date, value")
    rate = {d: float(v) for d, v in rows if v is not None and v == v}
    inputs = load_defensive_inputs(store, list(data["defensive_assets"]), rate)
    sma, bps = int(rule["sma_days"]), float(rule["trading_cost_bps"])
    defensive = defensive_monthly(defensive_series(inputs, sma, bps), start, end)
    hold = defensive_monthly(defensive_series(inputs, sma, bps, use_trend=False), start, end)
    w = float(rule["aggressive_weight"])
    mix = combine(aggressive, defensive, w, bps)
    months = sorted(mix)
    m_mix = [mix[m] for m in months]
    m_vt = [aggressive[m] for m in months]
    test = paired_sharpe_bootstrap(
        m_mix,
        m_vt,
        int(inf["resamples"]),
        int(inf["block_size_months"]),
        float(inf["confidence_level"]),
        int(inf["random_seed"]),
    )
    independent = verify_mix.mix_monthly(
        vt_monthly=aggressive, inputs=inputs, sma=sma, bps=bps, weight=w, start=start, end=end
    )
    worst_diff = max(abs(independent[m] - mix[m]) for m in months) if set(independent) == set(
        mix
    ) else float("inf")  # fmt: skip
    verified = worst_diff < 1e-12

    digest = hashlib.sha256()
    for m in months:
        digest.update(repr((m, aggressive[m], defensive[m])).encode())
    artifact: dict[str, Any] = {
        "study": contract["name"],
        "strategy": "sleeve_mix_v1",
        "parameters": {"rule": rule, "data": {k: str(v) for k, v in data.items()}},
        "snapshot_id": "mix-data-v1:sha256:" + digest.hexdigest(),
        "trading_enabled": False,
        "strategy_monthly_returns": m_mix,
        "months": months,
    }
    mt = record_and_assess(args.registry, artifact, list(data["defensive_assets"]) + ["^GSPC"])

    def pick(series: dict[str, float]) -> dict[str, Any]:
        return lt.metrics([series[m] for m in months])

    table = {
        "mix": pick(mix),
        "approved_vt": pick(aggressive),
        "defensive_trend": pick(defensive),
        "defensive_buy_hold": pick(hold),
        "sp500_total_return": pick(spx),
    }
    corr_src = [(aggressive[m], defensive[m]) for m in months]
    ma = sum(a for a, _ in corr_src) / len(corr_src)
    md = sum(d for _, d in corr_src) / len(corr_src)
    cov = sum((a - ma) * (d - md) for a, d in corr_src)
    corr = cov / math.sqrt(
        sum((a - ma) ** 2 for a, _ in corr_src) * sum((d - md) ** 2 for _, d in corr_src)
    )
    sens = {}
    for wa, _wd in contract.get("reporting", {}).get("weights_sensitivity", []):
        alt = combine(aggressive, defensive, float(wa), bps)
        sens[f"aggressive_{wa:g}"] = {
            **{k: v for k, v in pick(alt).items() if k != "growth_of_1"},
            "sharpe_minus_vt": sharpe([alt[m] for m in months]) - sharpe(m_vt),
        }
    years = {}
    for y in contract.get("reporting", {}).get("years", []):
        key = str(y)
        years[key] = {
            name: calendar_years(s).get(key)
            for name, s in (("mix", mix), ("approved_vt", aggressive), ("defensive", defensive),
                            ("sp500", spx))
        }  # fmt: skip
    artifact.update(
        metrics=table,
        sharpe_test=test,
        correlation_vt_defensive=corr,
        verification={"months": len(months), "max_abs_diff": worst_diff, "match": verified},
        sensitivity=sens,
        years=years,
        multiple_testing=mt,
    )
    artifact["failed_criteria"] = evaluate(
        contract, test, table["mix"], mt["deflated_sharpe"], verified
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    brief = {k: v for k, v in artifact.items() if k not in ("strategy_monthly_returns", "months")}
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
