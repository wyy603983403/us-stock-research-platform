"""Runtime settings from environment variables and an optional ``.env`` file (see .env.example).

Real environment variables win over ``.env``. ``USR_STORAGE_ROOT`` switches the data store from
CSV files under ``USR_DATA_DIR`` to zstd Parquet under that root (e.g. an external volume).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    storage_root: Path | None
    trading_enabled: bool
    http_timeout_seconds: float

    @property
    def snapshots_dir(self) -> Path:
        """Snapshots live next to the Parquet store when one is configured."""
        if self.storage_root is not None:
            return self.storage_root / "snapshots"
        return Path("artifacts/snapshots")


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for key, value in parse_dotenv(path.read_text()).items():
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    load_dotenv()
    trading = os.environ.get("TRADING_ENABLED", "false").strip().lower()
    if trading not in {"false", "0", ""}:
        raise RuntimeError("TRADING_ENABLED must stay false: this repository is research-only")
    root = os.environ.get("USR_STORAGE_ROOT", "").strip()
    return Settings(
        data_dir=Path(os.environ.get("USR_DATA_DIR", "data")),
        storage_root=Path(root) if root else None,
        trading_enabled=False,
        http_timeout_seconds=float(os.environ.get("USR_HTTP_TIMEOUT", "30")),
    )
