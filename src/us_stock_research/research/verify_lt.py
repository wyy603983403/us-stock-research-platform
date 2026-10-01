"""Independent re-implementation of the leveraged-trend backtest, used only to verify it.

Written from the contract's text with different structures (a deque for the moving average,
date-keyed dicts, months keyed by (year, month)). ``usr-verify-lt`` prints only differences.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any


def backtest(
    contract: dict[str, Any],
    days: list[date],
    prices: list[float],
    yields_pct: list[float | None],
) -> dict[tuple[int, int], float]:
    rule, data = contract["rule"], contract["data"]
    n_sma = int(rule["sma_days"])
    lev = float(rule["leverage"])
    cost = float(rule["switch_cost_bps"]) / 10_000
    window: deque[float] = deque(maxlen=n_sma)
    above: dict[date, bool] = {}
    for d, p in zip(days, prices, strict=True):
        window.append(p)
        if len(window) == n_sma:
            above[d] = p > sum(window) / n_sma
    growth: dict[tuple[int, int], float] = {}
    held: bool | None = None
    for k in range(2, len(days)):
        decided_on = days[k - 2]
        if decided_on not in above or yields_pct[k - 1] is None:
            continue
        today = days[k]
        on = above[decided_on]
        y = float(yields_pct[k - 1]) / 100  # type: ignore[arg-type]
        move = prices[k] / prices[k - 1] - 1
        if on:
            day_ret = lev * move - (lev - 1) * (y + 0.005) / 252 - (0.009 if lev != 1 else 0) / 252
        else:
            day_ret = y / 252
        switched = held is not None and held != on
        held = on
        if switched:
            day_ret = (1 + day_ret) * (1 - cost) - 1
        if not data["start"] <= today <= data["end"]:
            continue
        key = (today.year, today.month)
        growth[key] = growth.get(key, 1.0) * (1 + day_ret)
    return {k: v - 1 for k, v in growth.items()}


def compare(engine: dict[str, Any], independent: dict[tuple[int, int], float]) -> dict[str, Any]:
    keys = [(int(m[:4]), int(m[5:7])) for m in engine["months"]]
    if keys != sorted(independent):
        return {"match": False, "reason": "different months"}
    worst = max(abs(a - independent[k]) for k, a in zip(keys, engine["strategy"], strict=True))
    return {"months": len(keys), "max_abs_diff": worst, "match": worst < 1e-12}


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args(argv)
    contract = lt.load_lt_contract(args.contract)
    settings = load_settings()
    days, prices, yields, _ = lt.load_inputs(
        TableStore.from_settings(settings), open_store(settings), contract
    )
    report = compare(
        lt.run(contract, days, prices, yields), backtest(contract, days, prices, yields)
    )
    print(json.dumps(report, indent=2))
    return 0 if report["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
