"""Independent re-implementation of the backtest engine, used to catch logic bugs.

The production engine (``backtest.simulate``) walks day by day with a Python loop. This module
recomputes the same three strategies with pandas, in a different formulation: positions are held
as *units of growth* between rebalances (a cumulative-product per asset) instead of being
re-weighted every day. Both are fed the same snapshot and the same contract; the equity curves
must agree to floating-point precision. It covers the gross case (bps slippage only, no broker
commission or dividend tax), which is what the rule logic and timing depend on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from us_stock_research.config import load_settings
from us_stock_research.research.backtest import (
    align,
    metrics,
    month_end_indices,
    simulate,
)
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.snapshots import Bundle, load_snapshot

TOLERANCE = 1e-9


def independent_curve(c: StudyContract, bundle: Bundle) -> tuple[list[Any], list[float]]:
    import pandas as pd  # optional dependency: pip install -e '.[verify]'

    panel = align(bundle, c.data.start, c.data.end)
    prices = pd.DataFrame(panel.prices, index=pd.to_datetime(panel.days))
    rets = prices.pct_change()
    p = c.parameters
    risk = [s.upper() for s in c.risk_universe()]
    cash = p.cash_symbol.upper() if p.cash_symbol else None
    need = max(p.sma_days, p.lookback_days, p.vol_lookback_days + 1, c.validation.warmup_days)
    rebalance = [i for i in month_end_indices(panel.days) if i >= need]
    fills = {
        i + p.execution_lag_days: i for i in rebalance if i + p.execution_lag_days < len(prices)
    }
    fill_days = sorted(fills)
    slip = p.transaction_cost_bps / 10_000

    sma = prices.rolling(p.sma_days).mean()
    momentum = prices / prices.shift(p.lookback_days) - 1.0

    def target(i: int) -> dict[str, float]:
        w: dict[str, float] = {}

        def add(sym: str, weight: float) -> None:
            w[sym] = w.get(sym, 0.0) + weight

        if c.strategy == "buy_and_hold_v1":
            for s in risk:
                add(s, 1 / len(risk))
        elif c.strategy == "trend_sma_v1":
            for s in risk:
                add(s if prices[s].iloc[i] > sma[s].iloc[i] else str(cash), 1 / len(risk))
        elif c.strategy == "vol_target_v1":
            basket = rets[risk].mean(axis=1)
            vol = basket.rolling(p.vol_lookback_days).std(ddof=1).iloc[i] * 252**0.5
            exposure = 1.0 if vol <= 0 else min(1.0, p.vol_target / vol)
            for s in risk:
                add(s, exposure / len(risk))
            if exposure < 1.0:
                add(str(cash), 1.0 - exposure)
        else:  # dual_momentum_v1
            hurdle = momentum[cash].iloc[i] if cash else 0.0
            ranked = momentum.iloc[i][risk].sort_values(ascending=False).index[: p.top_k]
            for s in ranked:
                add(s if momentum[s].iloc[i] > hurdle else str(cash), 1 / p.top_k)
        return w

    equity = 1.0
    weights: dict[str, float] = {}
    curve: list[float] = []
    for n, t0 in enumerate(fill_days):
        w_new = target(fills[t0])
        keys = set(w_new) | set(weights)
        equity *= 1 - slip * sum(abs(w_new.get(k, 0.0) - weights.get(k, 0.0)) for k in keys)
        weights = w_new
        t1 = fill_days[n + 1] if n + 1 < len(fill_days) else len(prices) - 1
        segment = rets.iloc[t0 + 1 : t1 + 1][list(weights)]
        growth = (1.0 + segment).cumprod()
        values = growth.mul(pd.Series(weights), axis=1).sum(axis=1)
        if n == 0:
            curve.append(equity)
        else:
            curve[-1] = equity  # the fill day's close already includes the rebalance fee
        curve.extend((equity * values).tolist())
        equity *= float(values.iloc[-1]) if len(values) else 1.0
        if len(values):
            end_values = growth.iloc[-1] * pd.Series(weights)
            weights = (end_values / end_values.sum()).to_dict()
    return panel.days[fill_days[0] :], curve


def verify(c: StudyContract, bundle: Bundle) -> dict[str, Any]:
    panel = align(bundle, c.data.start, c.data.end)
    curve_a, idx, _ = simulate(c, panel)
    days_b, curve_b = independent_curve(c, bundle)
    n = min(len(curve_a), len(curve_b))
    worst = max(
        (abs(a / b - 1) for a, b in zip(curve_a[:n], curve_b[:n], strict=True)), default=1.0
    )
    days = [panel.days[i] for i in idx]
    ma, mb = metrics(curve_a, days), metrics(curve_b[:n], days[:n])
    return {
        "study": c.name,
        "strategy": c.strategy,
        "points_compared": n,
        "length_match": len(curve_a) == len(curve_b),
        "max_relative_difference": worst,
        "cagr": {"production": ma["cagr"], "independent": mb["cagr"]},
        "max_drawdown": {"production": ma["max_drawdown"], "independent": mb["max_drawdown"]},
        "passed": len(curve_a) == len(curve_b) and worst < TOLERANCE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--snapshots-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if not contract.data.snapshot_id:
        parser.error("contract has no data.snapshot_id")
    bundle = load_snapshot(
        contract.data.snapshot_id, args.snapshots_dir or load_settings().snapshots_dir
    )
    result = verify(contract, bundle)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
