"""Content-addressed data snapshots.

A snapshot copies the daily CSVs (and dividend CSVs when present) of a study universe into
``<snapshots_dir>/<sha256>/`` together with a manifest. The id
``daily-bundle-v1:sha256:<hex>`` is the hash of the sorted (file key, file sha256) list, so the
same bytes always give the same id and any change gives a new one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from us_stock_research.bars import (
    DailyBar,
    dividends_path,
    read_bars,
    read_dividends,
    symbol_path,
    write_bars,
    write_dividends,
)
from us_stock_research.storage import (
    BarStore,
    CsvStore,
    add_store_args,
    open_store,
    settings_from_args,
)

PREFIX = "daily-bundle-v1:sha256:"
DIV_SUFFIX = ".dividends"


@dataclass(frozen=True)
class Bundle:
    bars: dict[str, list[DailyBar]]
    dividends: dict[str, dict[date, float]]  # empty dict when a symbol has no dividend file


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_digest(file_hashes: dict[str, str]) -> str:
    canonical = json.dumps(sorted(file_hashes.items()), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_snapshot(source: Path | BarStore, symbols: list[str], snapshots_dir: Path) -> str:
    """Snapshot ``symbols`` from a CSV directory or any bar store.

    Files are always re-written in the canonical CSV layout before hashing, so the id depends
    only on the data itself, never on the storage format it was read from.
    """
    store: BarStore = CsvStore(source) if isinstance(source, Path) else source
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    staging = snapshots_dir / f".staging-{hashlib.sha256(str(symbols).encode()).hexdigest()[:12]}"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "daily").mkdir(parents=True)
    try:
        missing = [s.upper() for s in symbols if not store.has_bars(s)]
        if missing:
            raise FileNotFoundError(f"missing daily data for {missing}")
        hashes: dict[str, str] = {}
        for s in symbols:
            sym = s.upper()
            write_bars(symbol_path(staging, sym), store.read_bars(sym))
            hashes[sym] = file_sha256(staging / "daily" / f"{sym}.csv")
            if store.has_dividends(sym):
                write_dividends(dividends_path(staging, sym), store.read_dividends(sym))
                hashes[sym + DIV_SUFFIX] = file_sha256(staging / "daily" / f"{sym}{DIV_SUFFIX}.csv")
        digest = bundle_digest(hashes)
        target = snapshots_dir / digest
        if not target.exists():
            manifest = {"snapshot_id": PREFIX + digest, "files": hashes}
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return PREFIX + digest


def load_snapshot(snapshot_id: str, snapshots_dir: Path) -> Bundle:
    """Load and verify a snapshot; raises if any byte changed since it was taken."""
    if not snapshot_id.startswith(PREFIX):
        raise ValueError(f"unsupported snapshot id {snapshot_id!r}")
    digest = snapshot_id.removeprefix(PREFIX)
    root = snapshots_dir / digest
    manifest = json.loads((root / "manifest.json").read_text())
    hashes: dict[str, str] = manifest["files"]
    actual = {k: file_sha256(root / "daily" / f"{k}.csv") for k in hashes}
    if actual != hashes or bundle_digest(actual) != digest:
        raise ValueError(f"snapshot {snapshot_id} failed integrity verification")
    symbols = sorted(k for k in hashes if not k.endswith(DIV_SUFFIX))
    return Bundle(
        bars={s: read_bars(root / "daily" / f"{s}.csv") for s in symbols},
        dividends={
            s: read_dividends(root / "daily" / f"{s}{DIV_SUFFIX}.csv")
            if f"{s}{DIV_SUFFIX}" in hashes
            else {}
            for s in symbols
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    add_store_args(parser)
    parser.add_argument("--snapshots-dir", type=Path, help="default: <storage root>/snapshots")
    args = parser.parse_args(argv)
    settings = settings_from_args(args)
    snapshots_dir = args.snapshots_dir or settings.snapshots_dir
    print(create_snapshot(open_store(settings), args.symbols, snapshots_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
