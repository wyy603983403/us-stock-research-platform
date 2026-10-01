"""Trend-gated leveraged index backtest (study kind ``leveraged_trend``).

Daily simulation, one risky asset (its adjusted close is both signal and asset) and T-bills:

* signal on day d: close > simple moving average of the last ``sma_days`` closes (including d);
* the position over (k-1, k] follows the signal of day k-2 (decided at d's close, traded at the
  next close: ``execution_lag_days = 1``);
* risk-on day: ``L * r - (L - 1) * (y + spread) / 252 - fee / 252`` with y the previous day's
  3-month T-bill yield (fee only when L != 1); cash day: ``y / 252``;
* each switch between the two costs ``switch_cost_bps`` of NAV, charged on the first day of the
  new position.

``usr-leveraged-trend`` writes the result JSON, registers the trial and checks the pre-registered
criteria; ``validate_leverage_model`` compares the always-on leveraged model with a real
leveraged ETF (SSO) where one is stored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.research.stats import block_bootstrap

TRADING_DAYS = 252
FINANCING_SPREAD = 0.005
LEVERAGED_FEE = 0.009


def load_lt_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "leveraged_trend":
        raise ValueError(f"{path} is not a leveraged_trend contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        value = raw["data"][key]
        raw["data"][key] = value if isinstance(value, date) else date.fromisoformat(str(value))
    return raw


def forward_fill(days: list[date], points: dict[date, float]) -> list[float | None]:
    out: list[float | None] = []
    last: float | None = None
    keys = sorted(points)
    j = 0
    for d in days:
        while j < len(keys) and keys[j] <= d:
            last = points[keys[j]]
            j += 1
        out.append(last)
    return out


def simulate(
    days: list[date],
    prices: list[float],
    yields_pct: list[float | None],
    *,
    sma_days: int,
    leverage: float,
    switch_cost_bps: float,
    always_on: bool = False,
) -> list[tuple[date, float, float, bool]]:
    """(day, strategy return, asset return, risk-on) for every day with a defined position."""
    n = len(prices)
    signal: list[bool | None] = [None] * n
    window = 0.0
    for i in range(n):
        window += prices[i]
        if i >= sma_days:
            window -= prices[i - sma_days]
        if i >= sma_days - 1:
            signal[i] = prices[i] > window / sma_days
    out: list[tuple[date, float, float, bool]] = []
    prev_on: bool | None = None
    for k in range(2, n):
        on = True if always_on else signal[k - 2]
        y = yields_pct[k - 1]
        if on is None or y is None:
            continue
        rate = y / 100
        r = prices[k] / prices[k - 1] - 1
        if on:
            fee = LEVERAGED_FEE if leverage != 1.0 else 0.0
            ret = leverage * r - (leverage - 1) * (rate + FINANCING_SPREAD) / TRADING_DAYS
            ret -= fee / TRADING_DAYS
        else:
            ret = rate / TRADING_DAYS
        if prev_on is not None and on != prev_on:
            ret = (1 + ret) * (1 - switch_cost_bps / 10_000) - 1
        prev_on = on
        out.append((days[k], ret, r, on))
    return out


def monthly(daily: list[tuple[date, float]]) -> list[tuple[str, float]]:
    months: dict[str, float] = {}
    for d, r in daily:
        key = f"{d.year:04d}-{d.month:02d}"
        months[key] = months.get(key, 1.0) * (1 + r)
    return [(k, v - 1) for k, v in sorted(months.items())]


def metrics(rets: list[float]) -> dict[str, Any]:
    curve = [1.0]
    for r in rets:
        curve.append(curve[-1] * (1 + r))
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
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
        "growth_of_1": curve[-1],
    }


def run(
    contract: dict[str, Any],
    days: list[date],
    prices: list[float],
    yields_pct: list[float | None],
    *,
    leverage: float | None = None,
    switch_cost_bps: float | None = None,
) -> dict[str, Any]:
    rule, data = contract["rule"], contract["data"]
    sim = simulate(
        days,
        prices,
        yields_pct,
        sma_days=int(rule["sma_days"]),
        leverage=float(rule["leverage"] if leverage is None else leverage),
        switch_cost_bps=float(
            rule["switch_cost_bps"] if switch_cost_bps is None else switch_cost_bps
        ),
    )
    sim = [row for row in sim if data["start"] <= row[0] <= data["end"]]
    strat = monthly([(d, r) for d, r, _, _ in sim])
    bench = monthly([(d, a) for d, _, a, _ in sim])
    switches = sum(1 for a, b in zip(sim, sim[1:], strict=False) if a[3] != b[3])
    return {
        "first_day": sim[0][0].isoformat(),
        "last_day": sim[-1][0].isoformat(),
        "months": [m for m, _ in strat],
        "strategy": [r for _, r in strat],
        "benchmark": [r for _, r in bench],
        "time_in_market": sum(row[3] for row in sim) / len(sim),
        "switches": switches,
        "switches_per_year": switches / (len(sim) / TRADING_DAYS),
    }


def summarize(contract: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    inf = contract["inference"]
    excess = [a - b for a, b in zip(result["strategy"], result["benchmark"], strict=True)]
    return {
        "strategy": metrics(result["strategy"]),
        "benchmark": metrics(result["benchmark"]),
        "excess_vs_benchmark": block_bootstrap(
            excess,
            int(inf["resamples"]),
            int(inf["block_size_months"]),
            float(inf["confidence_level"]),
            int(inf["random_seed"]),
        ),
        "time_in_market": result["time_in_market"],
        "switches_per_year": result["switches_per_year"],
    }


def by_year(result: dict[str, Any]) -> dict[str, tuple[float, float]]:
    out: dict[str, list[float]] = {}
    for m, s, b in zip(result["months"], result["strategy"], result["benchmark"], strict=True):
        acc = out.setdefault(m[:4], [1.0, 1.0])
        acc[0] *= 1 + s
        acc[1] *= 1 + b
    return {y: (round(a - 1, 4), round(b - 1, 4)) for y, (a, b) in sorted(out.items())}


def validate_leverage_model(
    days: list[date],
    asset: list[float],
    yields_pct: list[float | None],
    etf: dict[date, float],
    leverage: float,
) -> dict[str, Any]:
    """Always-on model vs a real leveraged ETF's adjusted closes over the ETF's history."""
    sim = simulate(days, asset, yields_pct, sma_days=1, leverage=leverage, switch_cost_bps=0,
                   always_on=True)  # fmt: skip
    first = min(etf)
    model, real = 1.0, 1.0
    diffs = []
    prev = None
    for d, r, _, _ in sim:
        if d < first or d not in etf:
            continue
        if prev is not None and prev in etf:
            actual = etf[d] / etf[prev] - 1
            model *= 1 + r
            real *= 1 + actual
            diffs.append(r - actual)
        prev = d
    years = len(diffs) / TRADING_DAYS
    if years < 1:
        return {"error": "too little overlap"}
    mean = sum(diffs) / len(diffs)
    te = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1)) * math.sqrt(252)
    return {
        "from": first.isoformat(),
        "years": round(years, 2),
        "model_cagr": model ** (1 / years) - 1,
        "etf_cagr": real ** (1 / years) - 1,
        "annualized_diff": model ** (1 / years) - real ** (1 / years),
        "tracking_error": te,
    }


def evaluate(contract: dict[str, Any], summary: dict[str, Any], dsr: float | None,
             validation: dict[str, Any] | None) -> list[str]:  # fmt: skip
    fails: list[str] = []
    interval = summary["excess_vs_benchmark"].get("interval")
    if not interval or interval[0] <= 0:
        fails.append("相对 SPY 超额收益 95% 置信区间下限不为正")
    cap = float(contract["risk"]["max_worst_12m_loss"])
    worst = summary["strategy"]["worst_rolling_12m_return"]
    if worst is not None and worst < -cap:
        fails.append(f"最差滚动 12 个月 {worst:.1%}，超过 {cap:.0%} 亏损上限")
    if dsr is None or dsr < 0.95:
        fails.append(f"Deflated Sharpe {dsr if dsr is None else round(dsr, 3)} < 0.95")
    if validation is None or "annualized_diff" not in validation:
        fails.append("模型验证未完成（缺少 SSO 日线）")
    elif abs(validation["annualized_diff"]) > 0.015:
        fails.append(f"模型与 SSO 年化差异 {validation['annualized_diff']:+.2%} 超过 ±1.5%")
    return fails


def load_inputs(tables: Any, store: Any, contract: dict[str, Any]) -> tuple[
    list[date], list[float], list[float | None], dict[date, float] | None
]:  # fmt: skip
    symbol = contract["data"]["signal_and_asset"]
    bars = [b for b in store.read_bars(symbol) if b.adj_close > 0]
    days = [b.day for b in bars]
    prices = [b.adj_close for b in bars]
    rows = tables.read("macro", contract["data"]["cash_rate"], "date, value")
    rate = {d: float(v) for d, v in rows if v is not None and v == v}  # NaN = missing
    yields = forward_fill(days, rate)
    sso = None
    if store.has_bars("SSO"):
        sso = {b.day: b.adj_close for b in store.read_bars("SSO") if b.adj_close > 0}
    return days, prices, yields, sso


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research.trials import record_and_assess
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    contract = load_lt_contract(args.contract)
    settings = load_settings()
    tables = TableStore.from_settings(settings)
    store = open_store(settings)
    days, prices, yields, sso = load_inputs(tables, store, contract)
    result = run(contract, days, prices, yields)
    summary = summarize(contract, result)
    validation = (
        validate_leverage_model(days, prices, yields, sso, float(contract["rule"]["leverage"]))
        if sso
        else None
    )
    digest = hashlib.sha256()
    for row in zip(days, prices, yields, strict=True):
        digest.update(repr(row).encode())
    snapshot = "lt-data-v1:sha256:" + digest.hexdigest()
    artifact: dict[str, Any] = {
        "study": contract["name"],
        "strategy": "leveraged_trend_v1",
        "parameters": {"rule": contract["rule"], "data": {k: str(v) for k, v in
                                                          contract["data"].items()}},
        "snapshot_id": snapshot,
        "trading_enabled": False,
        "summary": summary,
        "by_year": by_year(result),
        "leverage_model_validation": validation,
        "strategy_monthly_returns": result["strategy"],
        "result": result,
    }  # fmt: skip
    sens = {}
    for lev in contract.get("sensitivity", {}).get("leverage", []):
        sens[f"leverage_{lev:g}"] = summarize(contract, run(contract, days, prices, yields,
                                                            leverage=float(lev)))  # fmt: skip
    for bps in contract.get("sensitivity", {}).get("switch_cost_bps", []):
        sens[f"switch_cost_{bps:g}bp"] = summarize(
            contract, run(contract, days, prices, yields, switch_cost_bps=float(bps))
        )
    artifact["sensitivity"] = {
        k: {"cagr": v["strategy"]["cagr"], "worst_12m": v["strategy"]["worst_rolling_12m_return"],
            "max_drawdown": v["strategy"]["max_drawdown"],
            "excess_interval": v["excess_vs_benchmark"].get("interval")}
        for k, v in sens.items()
    }  # fmt: skip
    mt = record_and_assess(args.registry, artifact, [contract["data"]["signal_and_asset"]])
    artifact["multiple_testing"] = mt
    artifact["failed_criteria"] = evaluate(contract, summary, mt["deflated_sharpe"], validation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    brief = {k: summary[k] for k in ("strategy", "benchmark", "excess_vs_benchmark")}
    brief.update(
        time_in_market=summary["time_in_market"],
        switches_per_year=summary["switches_per_year"],
        validation=validation,
        sensitivity=artifact["sensitivity"],
        deflated_sharpe=mt["deflated_sharpe"],
        failed_criteria=artifact["failed_criteria"],
    )
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
