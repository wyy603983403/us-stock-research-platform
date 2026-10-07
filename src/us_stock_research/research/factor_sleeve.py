"""Factor sleeve added to the approved two-sleeve mix (``research/factor-sleeve``).

Contract loader for now; the engine is written after registration and must follow the contract
text unchanged.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml


def load_fs_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "factor_sleeve":
        raise ValueError(f"{path} is not a factor_sleeve contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk", "success_criteria"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        value = raw["data"][key]
        raw["data"][key] = value if isinstance(value, date) else date.fromisoformat(str(value))
    weights = raw["rule"]["weights"]
    if abs(sum(float(v) for v in weights.values()) - 1) > 1e-9:
        raise ValueError(f"{path}: sleeve weights must sum to 1")
    return raw
