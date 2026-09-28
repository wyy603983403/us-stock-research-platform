"""Content-addressed data snapshots.

A snapshot copies the daily CSVs of a study universe into
``<snapshots_dir>/<sha256>/`` together with a manifest. The id
``daily-bundle-v1:sha256:<hex>`` is the hash of the sorted (symbol, file sha256) list, so the
same bytes always give the same id and any change gives a new one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from us_stock_research.bars import DailyBar, read_bars, symbol_path

PREFIX = "daily-bundle-v1:sha256:"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_digest(file_hashes: dict[str, str]) -> str:
    canonical = json.dumps(sorted(file_hashes.items()), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_snapshot(data_dir: Path, symbols: list[str], snapshots_dir: Path) -> str:
    sources = {s.upper(): symbol_path(data_dir, s) for s in symbols}
    missing = [s for s, p in sources.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing daily CSVs for {missing}")
    hashes = {s: file_sha256(p) for s, p in sources.items()}
    digest = bundle_digest(hashes)
    target = snapshots_dir / digest
    if not target.exists():
        tmp = snapshots_dir / f".{digest}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "daily").mkdir(parents=True)
        for symbol, src in sources.items():
            shutil.copy2(src, tmp / "daily" / f"{symbol}.csv")
        manifest = {"snapshot_id": PREFIX + digest, "files": hashes}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.rename(target)
    return PREFIX + digest


def load_snapshot(snapshot_id: str, snapshots_dir: Path) -> dict[str, list[DailyBar]]:
    """Load and verify a snapshot; raises if any byte changed since it was taken."""
    if not snapshot_id.startswith(PREFIX):
        raise ValueError(f"unsupported snapshot id {snapshot_id!r}")
    digest = snapshot_id.removeprefix(PREFIX)
    root = snapshots_dir / digest
    manifest = json.loads((root / "manifest.json").read_text())
    hashes: dict[str, str] = manifest["files"]
    actual = {s: file_sha256(root / "daily" / f"{s}.csv") for s in hashes}
    if actual != hashes or bundle_digest(actual) != digest:
        raise ValueError(f"snapshot {snapshot_id} failed integrity verification")
    return {s: read_bars(root / "daily" / f"{s}.csv") for s in sorted(hashes)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--snapshots-dir", type=Path, default=Path("artifacts/snapshots"))
    args = parser.parse_args(argv)
    print(create_snapshot(args.data_dir, args.symbols, args.snapshots_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
