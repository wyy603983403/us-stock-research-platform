"""Read-only, pre-registered monthly-rebalance backtest on a verified snapshot.

Timing: signals use closes up to the rebalance day (last common trading day of the month);
trades fill at the close ``execution_lag_days`` later and earn returns from the next day.
Costs: ``transaction_cost_bps`` (spread/slippage) on every traded dollar, plus an optional
broker scenario (commission with per-order minimum, per-share fee, dividend withholding tax)
applied to a notional portfolio size. Without a broker scenario results are gross of commission
and tax. Parameters come from the contract only; there is no optimisation here, so every reported
window is out of sample for the rule.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from us_stock_research.config import load_settings
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.snapshots import Bundle, load_snapshot
from us_stock_research.research.trials import record_and_assess

TRADING_DAYS = 252
CASH = "__CASH__"  # zero-return cash when no cash_symbol is declared


@dataclass(frozen=True)
class BrokerCosts:
    """Commission and tax model of one broker. Fee per order =
    max(min_per_order_usd, commission_bps * value + per_share_usd * shares), capped at
    max_pct_of_value * value when that cap is set."""

    name: str
    commission_bps: float = 0.0
    per_share_usd: float = 0.0
    min_per_order_usd: float = 0.0
    max_pct_of_value: float | None = None
    dividend_withholding_rate: float = 0.0

    def order_fee(self, value_usd: float, price: float) -> float:
        if value_usd <= 0:
            return 0.0
        shares = value_usd / price if price > 0 else 0.0
        fee = max(
            self.min_per_order_usd,
            self.commission_bps / 10_000 * value_usd + self.per_share_usd * shares,
        )
        if self.max_pct_of_value is not None:
            fee = min(fee, self.max_pct_of_value * value_usd)
        return fee


GROSS = BrokerCosts(name="gross")


@dataclass(frozen=True)
class Panel:
    days: list[date]
    prices: dict[str, list[float]]  # adj_close aligned to ``days``
    closes: dict[str, list[float]]  # raw close (order sizing, dividend yield)
    div_yield: dict[str, list[float]]  # cash dividend on day t / raw close on day t-1


def align(bundle: Bundle, start: date, end: date) -> Panel:
    bars = {k.upper(): v for k, v in bundle.bars.items()}
    maps = {s: {b.day: b for b in rows} for s, rows in bars.items()}
    common = set.intersection(*(set(m) for m in maps.values()))
    days = sorted(d for d in common if start <= d <= end)
    if len(days) < 2:
        raise ValueError("fewer than two common trading days in the study window")
    closes = {s: [m[d].close for d in days] for s, m in maps.items()}
    div_yield: dict[str, list[float]] = {}
    for s in maps:
        divs = {k: v for k, v in bundle.dividends.get(s, {}).items() if start <= k <= end}
        series = [0.0] * len(days)
        pos = {d: i for i, d in enumerate(days)}
        for ex_day, amount in divs.items():
            # ex-dates missing from the common calendar roll to the next common day
            i = pos.get(ex_day)
            if i is None:
                later = [j for j, d in enumerate(days) if d > ex_day]
                if not later:
                    continue
                i = later[0]
            if i > 0:
                series[i] += amount / closes[s][i - 1]
        div_yield[s] = series
    return Panel(
        days=days,
        prices={s: [m[d].adj_close for d in days] for s, m in maps.items()},
        closes=closes,
        div_yield=div_yield,
    )


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
    elif c.strategy == "vol_target_v1":
        # Equal-weight risk basket scaled so its realised volatility hits the target; never
        # levered (exposure <= 1), the remainder sits in the cash asset.
        n = p.vol_lookback_days
        basket = [
            sum(panel.prices[s][j] / panel.prices[s][j - 1] - 1.0 for s in risk) / len(risk)
            for j in range(i - n + 1, i + 1)
        ]
        mean = sum(basket) / n
        vol = (sum((r - mean) ** 2 for r in basket) / (n - 1)) ** 0.5 * TRADING_DAYS**0.5
        exposure = 1.0 if vol <= 0 else min(1.0, p.vol_target / vol)
        for s in risk:
            add(s, exposure / len(risk))
        if exposure < 1.0:
            add(cash, 1.0 - exposure)
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


def _ret(panel: Panel, sym: str, t: int, withholding: float = 0.0) -> float:
    """Adjusted (dividends reinvested) return, minus the tax withheld on that day's dividend."""
    if sym == CASH:
        return 0.0
    series = panel.prices[sym]
    return series[t] / series[t - 1] - 1.0 - withholding * panel.div_yield[sym][t]


@dataclass
class CostLedger:
    commission_usd: float = 0.0
    slippage_usd: float = 0.0
    orders: int = 0


def simulate(
    c: StudyContract,
    panel: Panel,
    broker: BrokerCosts = GROSS,
    portfolio_usd: float = 100_000.0,
    ledger: CostLedger | None = None,
) -> tuple[list[float], list[int], float]:
    """Returns (daily equity from first fill, day index of each equity point, total turnover).

    Equity starts at 1.0 = ``portfolio_usd``; fees are converted to fractions of current equity.
    """
    p = c.parameters
    need = max(p.sma_days, p.lookback_days, p.vol_lookback_days + 1, c.validation.warmup_days)
    rebal = [i for i in month_end_indices(panel.days) if i >= need]
    fills = {
        i + p.execution_lag_days: i for i in rebal if i + p.execution_lag_days < len(panel.days)
    }
    if not fills:
        raise ValueError("not enough history after warm-up for a single rebalance")
    first = min(fills)
    slip = p.transaction_cost_bps / 10_000
    tax = broker.dividend_withholding_rate
    book = ledger if ledger is not None else CostLedger()
    holdings: dict[str, float] = {}
    equity = 1.0
    curve: list[float] = []
    idx: list[int] = []
    turnover_total = 0.0
    for t in range(first, len(panel.days)):
        if t > first:
            gross = sum(w * (1 + _ret(panel, s, t, tax)) for s, w in holdings.items())
            if holdings:
                holdings = {
                    s: w * (1 + _ret(panel, s, t, tax)) / gross for s, w in holdings.items()
                }
            equity *= gross if holdings else 1.0
        if t in fills:
            target = target_weights(c, panel, fills[t])
            keys = set(target) | set(holdings)
            turnover = sum(abs(target.get(k, 0.0) - holdings.get(k, 0.0)) for k in keys) / 2
            turnover_total += turnover
            value = equity * portfolio_usd
            fees = 0.0
            for k in keys:
                traded = abs(target.get(k, 0.0) - holdings.get(k, 0.0)) * value
                if traded < 1e-6 or k == CASH:
                    continue
                commission = broker.order_fee(traded, panel.closes[k][t])
                book.commission_usd += commission
                book.slippage_usd += slip * traded
                book.orders += 1
                fees += commission + slip * traded
            equity *= 1 - fees / value
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


def benchmark_curve(
    panel: Panel, sym: str, idx: list[int], broker: BrokerCosts, portfolio_usd: float
) -> list[float]:
    """Buy and hold the benchmark once (one order), net of the same dividend withholding."""
    first_fee = broker.order_fee(portfolio_usd, panel.closes[sym][idx[0]]) / portfolio_usd
    equity = 1.0 - first_fee
    out = [equity]
    for t in idx[1:]:
        equity *= 1 + _ret(panel, sym, t, broker.dividend_withholding_rate)
        out.append(equity)
    return out


def run_backtest(
    c: StudyContract,
    bundle: Bundle,
    broker: BrokerCosts = GROSS,
    portfolio_usd: float = 100_000.0,
) -> dict[str, Any]:
    panel = align(bundle, c.data.start, c.data.end)
    ledger = CostLedger()
    curve, idx, turnover = simulate(c, panel, broker, portfolio_usd, ledger)
    days = [panel.days[i] for i in idx]
    bench_sym = c.benchmark.symbol.upper()
    bench = benchmark_curve(panel, bench_sym, idx, broker, portfolio_usd)
    strat_m, bench_m = monthly_returns(curve, days), monthly_returns(bench, days)
    excess = [a - b for a, b in zip(strat_m, bench_m, strict=True)]
    inf = c.inference
    return {
        "study": c.name,
        "strategy": c.strategy,
        "snapshot_id": c.data.snapshot_id,
        "git_sha": git_sha(),
        "parameters": c.parameters.model_dump(),
        "broker": asdict(broker),
        "portfolio_usd": portfolio_usd,
        "costs": asdict(ledger),
        "dividend_data": any(any(v) for v in panel.div_yield.values()),
        "strategy_metrics": metrics(curve, days) | {"turnover_one_way_total": turnover},
        "benchmark": {"symbol": bench_sym, "metrics": metrics(bench, days)},
        "walk_forward": walk_forward_windows(curve, days, c.validation.test_months),
        "oos_months": len(strat_m),
        "strategy_monthly_returns": strat_m,
        "equity_curve": {
            "dates": [d.isoformat() for d in days],
            "strategy": curve,
            "benchmark": bench,
        },
        "inference": block_bootstrap(
            excess, inf.resamples, inf.block_size_months, inf.confidence_level, c.random_seed
        ),
        "trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--snapshots-dir", type=Path, help="default: <storage root>/snapshots")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if not contract.data.snapshot_id:
        parser.error("contract has no data.snapshot_id; create and record a snapshot first")
    bundle = load_snapshot(
        contract.data.snapshot_id, args.snapshots_dir or load_settings().snapshots_dir
    )
    result = run_backtest(contract, bundle)
    result["multiple_testing"] = record_and_assess(args.registry, result, contract.data.universe)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["strategy_metrics"], indent=2))
    print(json.dumps(result["multiple_testing"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
