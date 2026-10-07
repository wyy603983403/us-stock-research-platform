"""Independent re-implementation of the 40/40/20 factor-sleeve mix (verification only).

Written from the contract text with different structures: (year, month) keys, an explicit
holdings walk in dollars for the month-end rebalance, and the drag compounded per day inside the
monthly product. The defensive sleeve reuses the independent ``verify_mix`` walk; the aggressive
sleeve is verified separately (``usr-verify-lt``).
"""

from __future__ import annotations

from datetime import date

from us_stock_research.research.verify_mix import _asset_months


def _port_months(
    rows: list[tuple[date, float]], drag: float, start: date, end: date
) -> dict[tuple[int, int], float]:
    keep = 1 - drag / 252
    growth: dict[tuple[int, int], float] = {}
    for d, r in rows:
        if start <= d <= end:
            growth[(d.year, d.month)] = growth.get((d.year, d.month), 1.0) * (1 + r) * keep
    return {k: g - 1 for k, g in growth.items()}


def mix_monthly(
    *,
    vt_monthly: dict[str, float],
    inputs: dict[str, tuple[list[date], list[float], list[float | None]]],
    sma: int,
    bps: float,
    portfolios: dict[str, list[tuple[date, float]]],
    drag: float,
    weights: dict[str, float],
    start: date,
    end: date,
) -> dict[str, float]:
    assets = [_asset_months(*inputs[s], sma, bps, start, end) for s in sorted(inputs)]
    ports = [_port_months(portfolios[p], drag, start, end) for p in sorted(portfolios)]
    vt = {(int(m[:4]), int(m[5:7])): r for m, r in vt_monthly.items()}
    keys = set(vt)
    for x in assets + ports:
        keys &= set(x)
    out: dict[str, float] = {}
    for key in sorted(keys):
        rets = {
            "aggressive": vt[key],
            "defensive": sum(a[key] for a in assets) / len(assets),
            "factor": sum(p[key] for p in ports) / len(ports),
        }
        dollars = {n: weights[n] * (1 + rets[n]) for n in weights}
        total = sum(dollars.values())
        traded = sum(abs(dollars[n] - weights[n] * total) for n in weights)
        out[f"{key[0]:04d}-{key[1]:02d}"] = total * (1 - traded / total * bps / 10_000) - 1
    return out
