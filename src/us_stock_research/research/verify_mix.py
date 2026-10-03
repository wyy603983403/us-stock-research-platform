"""Independent re-implementation of the defensive sleeve and the monthly mix (verification only).

Written from the contract text with different structures: a deque for the moving average,
date-keyed dicts, (year, month) keys and an explicit holdings walk for the month-end rebalance.
The aggressive sleeve is already verified separately (``usr-verify-lt``).
"""

from __future__ import annotations

from collections import deque
from datetime import date


def _asset_months(
    days: list[date], prices: list[float], rates: list[float | None], sma: int, bps: float,
    start: date, end: date,
) -> dict[tuple[int, int], float]:  # fmt: skip
    window: deque[float] = deque(maxlen=sma)
    trend: dict[date, bool] = {}
    for d, p in zip(days, prices, strict=True):
        window.append(p)
        if len(window) == sma:
            trend[d] = p > sum(window) / sma
    growth: dict[tuple[int, int], float] = {}
    last: bool | None = None
    for k in range(2, len(days)):
        if days[k - 2] not in trend or rates[k - 1] is None:
            continue
        on = trend[days[k - 2]]
        if on:
            r = prices[k] / prices[k - 1] - 1
        else:
            r = float(rates[k - 1]) / 100 / 252  # type: ignore[arg-type]
        if last is not None and last != on:
            r = (1 + r) * (1 - bps / 10_000) - 1
        last = on
        if start <= days[k] <= end:
            key = (days[k].year, days[k].month)
            growth[key] = growth.get(key, 1.0) * (1 + r)
    return {k: g - 1 for k, g in growth.items()}


def mix_monthly(
    *,
    vt_monthly: dict[str, float],
    inputs: dict[str, tuple[list[date], list[float], list[float | None]]],
    sma: int,
    bps: float,
    weight: float,
    start: date,
    end: date,
) -> dict[str, float]:
    assets = [_asset_months(*inputs[s], sma, bps, start, end) for s in sorted(inputs)]
    keys = set(assets[0])
    for a in assets[1:]:
        keys &= set(a)
    vt = {(int(m[:4]), int(m[5:7])): r for m, r in vt_monthly.items()}
    out: dict[str, float] = {}
    for key in sorted(keys & set(vt)):
        defensive = sum(a[key] for a in assets) / len(assets)
        hold_vt, hold_def = weight * (1 + vt[key]), (1 - weight) * (1 + defensive)
        total = hold_vt + hold_def
        trade = abs(hold_vt - weight * total) + abs(hold_def - (1 - weight) * total)
        after = total - trade / total * total * bps / 10_000
        out[f"{key[0]:04d}-{key[1]:02d}"] = after - 1
    return out
