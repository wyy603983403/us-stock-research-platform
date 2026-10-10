"""Small-cap factor sleeve vs the market (study small_factor_sleeve, ``usr-small-factor``)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml


def load_sf_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "small_factor":
        raise ValueError(f"{path} is not a small_factor contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk", "success_criteria"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        v = raw["data"][key]
        raw["data"][key] = v if isinstance(v, date) else date.fromisoformat(str(v))
    return raw
