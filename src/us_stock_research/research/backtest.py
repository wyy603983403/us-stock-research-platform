"""Read-only, pre-registered monthly-rebalance backtest on a verified snapshot.

Timing: signals use closes up to the rebalance day (last common trading day of the month);
trades fill at the close ``execution_lag_days`` later and earn returns from the next day.
Costs are ``transaction_cost_bps`` on one-way turnover. Parameters come from the contract only;
there is no optimisation here, so every reported window is out of sample for the rule.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from us_stock_research.bars import DailyBar
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.snapshots import load_snapshot

TRADING_DAYS = 252
CASH = "__CASH__"  # zero-return cash when no cash_symbol is declared


@dataclass(frozen=True)
class Panel:
    days: list[date]
    prices: dict[str, list[float]]  # adj_close aligned to ``days``


def align(bars: dict[str, list[DailyBar]], start: date, end: date) -> Panel:
    maps = {s: {b.day: b.adj_close for b in rows} for s, rows in bars.items()}
    common = set.intersection(*(set(m) for m in maps.values()))
    days = sorted(d for d in common if start <= d <= end)
    if len(days) < 2:
        raise ValueError("fewer than two common trading days in the study window")
    return Panel(days=days, prices={s: [m[d] for d in days] for s, m in maps.items()})


def month_end_indices(days: list[date]) -> list[int]:
    return [i for i in range(len(days) - 1) if days[i + 1].month != days[i].month]


def target_weights(c: StudyContract, panel: Panel, i: int) -> dict[str, float]:
    p = c.parameters
    risk = [s.upper() for s in c.risk_universe()]
    cash = p.cash_symbol.upper() if p.cash_symbol else CASH
    weights: dict[str, float] = {}

    def add(sym: str, w: float) -> None:
        weights[sym] = weights.get(sym, 0.0) + w

    if c.strategy == "buy_and_hold_v1":
        for s in risk:
            add(s, 1.0 / len(risk))
    elif c.strategy == "trend_sma_v1":
        n = p.sma_days
        for s in risk:
            window = panel.prices[s][i - n + 1 : i + 1]
            add(s if panel.prices[s][i] > sum(window) / n else cash, 1.0 / len(risk))
    elif c.strategy == "dual_momentum_v1":
        lb = p.lookback_days

        def mom(sym: str) -> float:
            if sym == CASH:
                return 0.0
            return panel.prices[sym][i] / panel.prices[sym][i - lb] - 1.0

        hurdle = mom(cash)
        ranked = sorted(risk, key=mom, reverse=True)[: p.top_k]
        for s in ranked:
            add(s if mom(s) > hurdle else cash, 1.0 / p.top_k)
    else:  # guarded by the contract validator
        raise ValueError(c.strategy)
    return {k: v for k, v in weights.items() if v > 0}


def _ret(panel: Panel, sym: str, t: int) -> float:
    if sym == CASH:
        return 0.0
    series = panel.prices[sym]
    return series[t] / series[t - 1] - 1.0


def simulate(c: StudyContract, panel: Panel) -> tuple[list[float], list[int], float]:
    """Returns (daily equity from first fill, day index of each equity point, total turnover)."""
    p = c.parameters
    need = max(p.sma_days, p.lookback_days, c.validation.warmup_days)
    rebal = [i for i in month_end_indices(panel.days) if i >= need]
    fills = {
        i + p.execution_lag_days: i for i in rebal if i + p.execution_lag_days < len(panel.days)
    }
    if not fills:
        raise ValueError("not enough history after warm-up for a single rebalance")
    first = min(fills)
    cost = p.transaction_cost_bps / 10_000
    holdings: dict[str, float] = {}
    equity = 1.0
    curve: list[float] = []
    idx: list[int] = []
    turnover_total = 0.0
    for t in range(first, len(panel.days)):
        if t > first:
            gross = sum(w * (1 + _ret(panel, s, t)) for s, w in holdings.items())
            if holdings:
                holdings = {s: w * (1 + _ret(panel, s, t)) / gross for s, w in holdings.items()}
            equity *= gross if holdings else 1.0
        if t in fills:
            target = target_weights(c, panel, fills[t])
            keys = set(target) | set(holdings)
            turnover = sum(abs(target.get(k, 0.0) - holdings.get(k, 0.0)) for k in keys) / 2
            turnover_total += turnover
            equity *= 1 - cost * 2 * turnover
            holdings = target
        curve.append(equity)
        idx.append(t)
    return curve, idx, turnover_total


def metrics(curve: list[float], days: list[date]) -> dict[str, Any]:
    rets = [b / a - 1 for a, b in zip(curve, curve[1:], strict=False)]
    years = max((days[-1] - days[0]).days / 365.25, 1e-9)
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    vol = (var**0.5) * TRADING_DAYS**0.5
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rolling = [curve[i] / curve[i - TRADING_DAYS] - 1 for i in range(TRADING_DAYS, len(curve))]
    by_year: dict[int, tuple[float, float]] = {}
    prev = curve[0]
    for v, d in zip(curve, days, strict=True):
        start, _ = by_year.get(d.year, (prev, v))
        by_year[d.year] = (start, v)
        prev = v
    yearly = {str(y): e / s - 1 for y, (s, e) in by_year.items()}
    return {
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "total_return": curve[-1] / curve[0] - 1,
        "cagr": (curve[-1] / curve[0]) ** (1 / years) - 1,
        "annual_volatility": vol,
        "sharpe_rf0": (mean * TRADING_DAYS) / vol if vol > 0 else None,
        "max_drawdown": mdd,
        "worst_rolling_12m_return": min(rolling) if rolling else None,
        "worst_calendar_year_return": min(yearly.values()),
        "calendar_year_returns": yearly,
    }


def monthly_returns(curve: list[float], days: list[date]) -> list[float]:
    ends = [i for i in range(len(days) - 1) if days[i + 1].month != days[i].month]
    ends.append(len(days) - 1)
    points = [0, *ends]
    return [curve[b] / curve[a] - 1 for a, b in zip(points, points[1:], strict=False) if b > a]


def block_bootstrap(
    excess: list[float], resamples: int, block: int, level: float, seed: int
) -> dict[str, Any]:
    n = len(excess)
    if n < block * 2:
        return {"method": "moving_block_bootstrap_mean_excess_v1", "error": "too few months"}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < n:
            s = rng.randrange(0, n - block + 1)
            sample.extend(excess[s : s + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int((1 - level) / 2 * resamples)]
    hi = means[min(int((1 + level) / 2 * resamples), resamples - 1)]
    return {
        "method": "moving_block_bootstrap_mean_excess_v1",
        "months": n,
        "mean_monthly_excess": sum(excess) / n,
        "interval": [lo, hi],
        "confidence_level": level,
        "probability_positive": sum(m > 0 for m in means) / resamples,
    }


def walk_forward_windows(curve: list[float], days: list[date], months: int) -> list[dict[str, Any]]:
    ends = [i for i in range(len(days) - 1) if days[i + 1].month != days[i].month]
    cuts = [0, *ends[months - 1 :: months]]
    if cuts[-1] != len(days) - 1:
        cuts.append(len(days) - 1)
    out: list[dict[str, Any]] = []
    for a, b in zip(cuts, cuts[1:], strict=False):
        if b - a < 5:
            continue
        out.append(
            {
                "start": days[a].isoformat(),
                "end": days[b].isoformat(),
                "return": curve[b] / curve[a] - 1,
            }
        )
    return out


def git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_backtest(c: StudyContract, bars: dict[str, list[DailyBar]]) -> dict[str, Any]:
    panel = align({k.upper(): v for k, v in bars.items()}, c.data.start, c.data.end)
    curve, idx, turnover = simulate(c, panel)
    days = [panel.days[i] for i in idx]
    bench_sym = c.benchmark.symbol.upper()
    base = panel.prices[bench_sym][idx[0]]
    bench = [panel.prices[bench_sym][i] / base for i in idx]
    strat_m, bench_m = monthly_returns(curve, days), monthly_returns(bench, days)
    excess = [a - b for a, b in zip(strat_m, bench_m, strict=True)]
    inf = c.inference
    return {
        "study": c.name,
        "strategy": c.strategy,
        "snapshot_id": c.data.snapshot_id,
        "git_sha": git_sha(),
        "parameters": c.parameters.model_dump(),
        "strategy_metrics": metrics(curve, days) | {"turnover_one_way_total": turnover},
        "benchmark": {"symbol": bench_sym, "metrics": metrics(bench, days)},
        "walk_forward": walk_forward_windows(curve, days, c.validation.test_months),
        "oos_months": len(strat_m),
        "inference": block_bootstrap(
            excess, inf.resamples, inf.block_size_months, inf.confidence_level, c.random_seed
        ),
        "trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--snapshots-dir", type=Path, default=Path("artifacts/snapshots"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if not contract.data.snapshot_id:
        parser.error("contract has no data.snapshot_id; create and record a snapshot first")
    bars = load_snapshot(contract.data.snapshot_id, args.snapshots_dir)
    result = run_backtest(contract, bars)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["strategy_metrics"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
