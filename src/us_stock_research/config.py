"""Runtime settings read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    trading_enabled: bool
    http_timeout_seconds: float


def load_settings() -> Settings:
    trading = os.environ.get("TRADING_ENABLED", "false").strip().lower()
    if trading not in {"false", "0", ""}:
        raise RuntimeError("TRADING_ENABLED must stay false: this repository is research-only")
    return Settings(
        data_dir=Path(os.environ.get("USR_DATA_DIR", "data")),
        trading_enabled=False,
        http_timeout_seconds=float(os.environ.get("USR_HTTP_TIMEOUT", "30")),
    )
