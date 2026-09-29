from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from us_stock_research.bars import DailyBar
from us_stock_research.config import Settings, parse_dotenv
from us_stock_research.research.snapshots import create_snapshot, load_snapshot
from us_stock_research.storage import CsvStore, ParquetStore, open_store


def _bars() -> list[DailyBar]:
    return [
        DailyBar(date(2024, 1, 2), 470.1, 472.3, 468.9, 471.25, 470.912345678901, 81_000_000),
        DailyBar(date(2024, 1, 3), 471.0, 472.0, 466.5, 467.1, 466.7777777777777, 92_500_000),
    ]


def test_parse_dotenv_and_settings_paths(tmp_path: Path) -> None:
    text = "# c\nA=1\nB = 'x y'\nC=\"z\"\nbroken\nUSR_STORAGE_ROOT=/Volumes/mysql/x\n"
    assert parse_dotenv(text) == {
        "A": "1",
        "B": "x y",
        "C": "z",
        "USR_STORAGE_ROOT": "/Volumes/mysql/x",
    }
    csv_mode = Settings(tmp_path, None, False, 30)
    assert csv_mode.snapshots_dir == Path("artifacts/snapshots")
    pq_mode = Settings(tmp_path, tmp_path / "root", False, 30)
    assert pq_mode.snapshots_dir == tmp_path / "root" / "snapshots"
    assert isinstance(open_store(csv_mode), CsvStore)
    assert isinstance(open_store(pq_mode), ParquetStore)


def test_parquet_roundtrip_is_exact_and_atomic(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    store = ParquetStore(tmp_path)
    store.write_bars("spy", _bars())
    store.write_dividends("SPY", {date(2024, 3, 15): 1.5900000000000001})
    assert store.has_bars("SPY") and store.has_dividends("spy")
    assert store.read_bars("SPY") == _bars()
    assert store.read_dividends("SPY") == {date(2024, 3, 15): 1.5900000000000001}
    assert not list(tmp_path.rglob("*.tmp"))
    store.write_dividends("QQQ", {})
    assert store.read_dividends("QQQ") == {}
    assert not store.has_bars("QQQ")


def test_snapshot_id_is_identical_for_csv_and_parquet(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    csv_store = CsvStore(tmp_path / "csv")
    pq_store = ParquetStore(tmp_path / "pq")
    for store in (csv_store, pq_store):
        store.write_bars("SPY", _bars())
        store.write_dividends("SPY", {date(2024, 3, 15): 1.59})
    id_csv = create_snapshot(csv_store, ["SPY"], tmp_path / "snap-a")
    id_pq = create_snapshot(pq_store, ["SPY"], tmp_path / "snap-b")
    assert id_csv == id_pq
    bundle = load_snapshot(id_pq, tmp_path / "snap-b")
    assert bundle.bars["SPY"] == _bars()
    assert bundle.dividends["SPY"] == {date(2024, 3, 15): 1.59}
    assert not [p for p in (tmp_path / "snap-b").iterdir() if p.name.startswith(".")]


def test_snapshot_reports_missing_symbol(tmp_path: Path) -> None:
    store = CsvStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        create_snapshot(store, ["NOPE"], tmp_path / "snaps")
    assert not [p for p in (tmp_path / "snaps").iterdir()]
