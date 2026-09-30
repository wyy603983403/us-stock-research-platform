"""Dependency-free statistics shared by the ETF backtest and the cross-section engine."""

from __future__ import annotations

import random
from typing import Any


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
