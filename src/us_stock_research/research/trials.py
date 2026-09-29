"""Trial registry and multiple-testing correction (Deflated Sharpe Ratio).

Every distinct parameter set ever backtested is appended to ``research/trials.jsonl`` (tracked in
Git). The more variants you try, the higher the best Sharpe you would see by luck alone, so a
result is judged against the expected maximum Sharpe of ``N`` trials, not against zero
(Bailey & Lopez de Prado, 2014). Re-running the same parameters on the same snapshot does not
count as a new trial.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

EULER_GAMMA = 0.5772156649015329
NORMAL = NormalDist()
DSR_THRESHOLD = 0.95


def moment_stats(returns: list[float]) -> dict[str, float]:
    """Per-period Sharpe (rf = 0), skewness and raw kurtosis (normal = 3)."""
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(var)
    if sd == 0:
        return {"sharpe": 0.0, "skew": 0.0, "kurtosis": 3.0, "n": float(n)}
    skew = sum(((r - mean) / sd) ** 3 for r in returns) / n
    kurt = sum(((r - mean) / sd) ** 4 for r in returns) / n
    return {"sharpe": mean / sd, "skew": skew, "kurtosis": kurt, "n": float(n)}


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Expected best per-period Sharpe among ``n_trials`` skill-less trials."""
    if n_trials < 2 or sharpe_variance <= 0:
        return 0.0
    return math.sqrt(sharpe_variance) * (
        (1 - EULER_GAMMA) * NORMAL.inv_cdf(1 - 1 / n_trials)
        + EULER_GAMMA * NORMAL.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def deflated_sharpe(stats: dict[str, float], n_trials: int, sharpe_variance: float) -> float:
    """Probability that the true Sharpe exceeds what ``n_trials`` lucky trials would show."""
    sr, t = stats["sharpe"], stats["n"]
    sr0 = expected_max_sharpe(n_trials, sharpe_variance)
    denom = 1 - stats["skew"] * sr + (stats["kurtosis"] - 1) / 4 * sr**2
    if t < 2 or denom <= 0:
        return 0.0
    return NORMAL.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def param_hash(strategy: str, parameters: dict[str, Any], universe: list[str]) -> str:
    blob = json.dumps(
        {"strategy": strategy, "parameters": parameters, "universe": sorted(universe)},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_trials(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def record_and_assess(path: Path, artifact: dict[str, Any], universe: list[str]) -> dict[str, Any]:
    """Register this run (once per parameter set + snapshot) and deflate its Sharpe."""
    returns: list[float] = artifact["strategy_monthly_returns"]
    stats = moment_stats(returns)
    key = param_hash(artifact["strategy"], artifact["parameters"], universe)
    trials = load_trials(path)
    if not any(
        t["param_hash"] == key and t["snapshot_id"] == artifact["snapshot_id"] for t in trials
    ):
        entry = {
            "study": artifact["study"],
            "strategy": artifact["strategy"],
            "param_hash": key,
            "snapshot_id": artifact["snapshot_id"],
            "months": len(returns),
            "sharpe_monthly": stats["sharpe"],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        trials.append(entry)
    distinct = {t["param_hash"]: t["sharpe_monthly"] for t in trials}
    sharpes = list(distinct.values())
    n = len(sharpes)
    mean = sum(sharpes) / n
    variance = sum((s - mean) ** 2 for s in sharpes) / (n - 1) if n > 1 else 0.0
    dsr = deflated_sharpe(stats, n, variance)
    return {
        "trials_registered": n,
        "trial_sharpe_variance": variance,
        "sharpe_monthly": stats["sharpe"],
        "sharpe_expected_max_under_luck": expected_max_sharpe(n, variance),
        "deflated_sharpe": dsr,
        "threshold": DSR_THRESHOLD,
        "registry": str(path),
    }
