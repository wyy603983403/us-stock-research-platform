"""Pluggable bar storage: CSV files (default, no dependencies) or zstd Parquet via DuckDB.

Both back ends hold exactly the same logical data (daily bars and cash dividends per symbol) and
round-trip floats exactly, so a snapshot built from either has the same content hash.

Parquet layout under ``<root>``::

    parquet/daily/<SYMBOL>.parquet
    parquet/dividends/<SYMBOL>.parquet

Install DuckDB with ``pip install -e '.[storage]'``. Writes go to a temp file and are renamed, so
an interrupted run never leaves a half-written file.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from us_stock_research.bars import (
    DailyBar,
    dividends_path,
    read_bars,
    read_dividends,
    symbol_path,
    write_bars,
    write_dividends,
)
from us_stock_research.config import Settings, load_settings


class BarStore(Protocol):
    def has_bars(self, symbol: str) -> bool: ...
    def read_bars(self, symbol: str) -> list[DailyBar]: ...
    def write_bars(self, symbol: str, bars: list[DailyBar]) -> None: ...
    def has_dividends(self, symbol: str) -> bool: ...
    def read_dividends(self, symbol: str) -> dict[date, float]: ...
    def write_dividends(self, symbol: str, dividends: dict[date, float]) -> None: ...
    def location(self, symbol: str) -> str: ...


class CsvStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def has_bars(self, symbol: str) -> bool:
        return symbol_path(self.data_dir, symbol).exists()

    def read_bars(self, symbol: str) -> list[DailyBar]:
        return read_bars(symbol_path(self.data_dir, symbol))

    def write_bars(self, symbol: str, bars: list[DailyBar]) -> None:
        write_bars(symbol_path(self.data_dir, symbol), bars)

    def has_dividends(self, symbol: str) -> bool:
        return dividends_path(self.data_dir, symbol).exists()

    def read_dividends(self, symbol: str) -> dict[date, float]:
        return read_dividends(dividends_path(self.data_dir, symbol))

    def write_dividends(self, symbol: str, dividends: dict[date, float]) -> None:
        write_dividends(dividends_path(self.data_dir, symbol), dividends)

    def location(self, symbol: str) -> str:
        return str(symbol_path(self.data_dir, symbol))


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _duckdb() -> Any:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("Parquet storage needs DuckDB: pip install -e '.[storage]'") from exc
        return duckdb

    def _daily(self, symbol: str) -> Path:
        return self.root / "parquet" / "daily" / f"{symbol.upper()}.parquet"

    def _divs(self, symbol: str) -> Path:
        return self.root / "parquet" / "dividends" / f"{symbol.upper()}.parquet"

    def _write(
        self, path: Path, names: list[str], types: list[str], columns: list[list[Any]]
    ) -> None:
        """Write column lists as one zstd Parquet file (bulk insert: ~90x faster than rows)."""
        duckdb = self._duckdb()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        ddl = ", ".join(f"{n} {t}" for n, t in zip(names, types, strict=True))
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE t({ddl})")
            if columns[0]:
                unnest = ", ".join(f"unnest(?::{t}[])" for t in types)
                con.execute(f"INSERT INTO t SELECT {unnest}", columns)
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY date) TO {_quote(tmp)} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            con.close()
        tmp.replace(path)

    def _read(self, path: Path, columns: str) -> list[tuple[Any, ...]]:
        duckdb = self._duckdb()
        con = duckdb.connect()
        try:
            result: list[tuple[Any, ...]] = con.execute(
                f"SELECT {columns} FROM read_parquet({_quote(path)}) ORDER BY date"
            ).fetchall()
        finally:
            con.close()
        return result

    def has_bars(self, symbol: str) -> bool:
        return self._daily(symbol).exists()

    def read_bars(self, symbol: str) -> list[DailyBar]:
        rows = self._read(self._daily(symbol), "date, open, high, low, close, adj_close, volume")
        return [DailyBar(r[0], r[1], r[2], r[3], r[4], r[5], int(r[6])) for r in rows]

    def write_bars(self, symbol: str, bars: list[DailyBar]) -> None:
        self._write(
            self._daily(symbol),
            ["date", "open", "high", "low", "close", "adj_close", "volume"],
            ["DATE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "BIGINT"],
            [
                [b.day for b in bars],
                [b.open for b in bars],
                [b.high for b in bars],
                [b.low for b in bars],
                [b.close for b in bars],
                [b.adj_close for b in bars],
                [b.volume for b in bars],
            ],
        )

    def has_dividends(self, symbol: str) -> bool:
        return self._divs(symbol).exists()

    def read_dividends(self, symbol: str) -> dict[date, float]:
        return {r[0]: float(r[1]) for r in self._read(self._divs(symbol), "date, amount")}

    def write_dividends(self, symbol: str, dividends: dict[date, float]) -> None:
        days = sorted(dividends)
        self._write(
            self._divs(symbol),
            ["date", "amount"],
            ["DATE", "DOUBLE"],
            [days, [dividends[d] for d in days]],
        )

    def location(self, symbol: str) -> str:
        return str(self._daily(symbol))


def open_store(settings: Settings) -> BarStore:
    if settings.storage_root is not None:
        return ParquetStore(settings.storage_root)
    return CsvStore(settings.data_dir)


def add_store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, help="CSV data directory (default: USR_DATA_DIR)")
    parser.add_argument(
        "--storage-root", type=Path, help="Parquet storage root (default: USR_STORAGE_ROOT)"
    )


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if args.storage_root is not None:
        settings = replace(settings, storage_root=args.storage_root)
    elif args.data_dir is not None:
        settings = replace(settings, data_dir=args.data_dir, storage_root=None)
    return settings
