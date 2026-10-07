"""Independent re-implementation of the trend-vote study (verification only).

From the contract text with different structures: ``statistics`` for averages and volatility,
deques for the moving windows, months keyed as "YYYY-MM" from a dict walk.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import date
from typing import Any


def monthly_returns(
    contract: dict[str, Any], days: list[date], prices: list[float], yields: list[float | None]
) -> dict[str, float]:
    rule, data = contract["rule"], contract["data"]
    smas = [int(x) for x in rule["signals"]["sma_days"]]
    mom = int(rule["signals"]["momentum_days"])
    nvol, goal = int(rule["vol_window_days"]), float(rule["vol_target"])
    cap, band = float(rule["max_leverage"]), float(rule["rebalance_band"])
    bps = float(rule["trading_cost_bps"]) / 10_000
    windows = {n: deque(maxlen=n) for n in smas}  # type: ignore[var-annotated]
    moves: deque[float] = deque(maxlen=nvol)
    growth_bill: deque[float] = deque(maxlen=mom)
    aim: dict[int, float] = {}
    for i, p in enumerate(prices):
        for w in windows.values():
            w.append(p)
        if i:
            moves.append(p / prices[i - 1] - 1)
            y = yields[i - 1]
            growth_bill.append(math.nan if y is None else 1 + y / 100 / 252)
        if i < max(max(smas) - 1, mom) or len(moves) < nvol:
            continue
        points = sum(p > statistics.fmean(windows[n]) for n in smas)
        bill = math.prod(growth_bill)
        if math.isnan(bill):
            continue
        points += p / prices[i - mom] > bill
        share = points / (len(smas) + 1)
        if share == 0:
            aim[i] = 0.0
            continue
        vol = statistics.stdev(moves) * math.sqrt(252)
        aim[i] = share * (cap if vol == 0 else min(cap, goal / vol))
    growth: dict[str, float] = {}
    exposure: float | None = None
    for k in range(2, len(days)):
        if k - 2 not in aim or yields[k - 1] is None:
            continue
        t = aim[k - 2]
        fee = 0.0
        if exposure is None:
            exposure = t
        elif (t == 0) != (exposure == 0) or abs(t - exposure) > band:
            fee = abs(t - exposure) * bps
            exposure = t
        y = float(yields[k - 1]) / 100  # type: ignore[arg-type]
        r = prices[k] / prices[k - 1] - 1
        if exposure <= 1:
            day = exposure * r + (1 - exposure) * y / 252
        else:
            day = exposure * r - (exposure - 1) * (y + 0.005 + 0.009) / 252
        day = (1 + day) * (1 - fee) - 1
        if data["start"] <= days[k] <= data["end"]:
            key = f"{days[k].year:04d}-{days[k].month:02d}"
            growth[key] = growth.get(key, 1.0) * (1 + day)
    return {m: g - 1 for m, g in growth.items()}
