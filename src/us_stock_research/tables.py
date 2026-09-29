"""Generic columnar tables next to the price data (macro series, fundamentals, metadata).

Layout under ``<USR_STORAGE_ROOT>/parquet/<kind>/<key>.parquet`` (zstd, DuckDB). Same atomic
temp-file-and-rename writes and bulk inserts as the price store. Needs ``USR_STORAGE_ROOT``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from us_stock_research.config import Settings


class TableStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def from_settings(cls, settings: Settings) -> TableStore:
        if settings.storage_root is None:
            raise RuntimeError("this data needs Parquet storage: set USR_STORAGE_ROOT in .env")
        return cls(settings.storage_root)

    @staticmethod
    def _duckdb() -> Any:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("needs DuckDB: pip install -e '.[storage]'") from exc
        return duckdb

    def path(self, kind: str, key: str) -> Path:
        return self.root / "parquet" / kind / f"{key}.parquet"

    def has(self, kind: str, key: str) -> bool:
        return self.path(kind, key).exists()

    def keys(self, kind: str) -> list[str]:
        return sorted(p.stem for p in (self.root / "parquet" / kind).glob("*.parquet"))

    def write(
        self,
        kind: str,
        key: str,
        schema: dict[str, str],
        columns: list[list[Any]],
        order_by: str,
    ) -> None:
        """``schema`` maps column name -> DuckDB type; ``columns`` are parallel value lists."""
        target = self.path(kind, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".parquet.tmp")
        ddl = ", ".join(f"{n} {t}" for n, t in schema.items())
        con = self._duckdb().connect()
        try:
            con.execute(f"CREATE TABLE t({ddl})")
            if columns and columns[0]:
                unnest = ", ".join(f"unnest(?::{t}[])" for t in schema.values())
                con.execute(f"INSERT INTO t SELECT {unnest}", columns)
            quoted = str(tmp).replace("'", "''")
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY {order_by}) TO '{quoted}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            con.close()
        tmp.replace(target)

    def read(self, kind: str, key: str, columns: str = "*") -> list[tuple[Any, ...]]:
        con = self._duckdb().connect()
        try:
            quoted = str(self.path(kind, key)).replace("'", "''")
            rows: list[tuple[Any, ...]] = con.execute(
                f"SELECT {columns} FROM read_parquet('{quoted}')"
            ).fetchall()
        finally:
            con.close()
        return rows

    def aggregate(self, kind: str, select: str) -> tuple[Any, ...] | None:
        """One aggregate row over every file of ``kind`` (e.g. ``count(*), min(ts)``)."""
        if not self.keys(kind):
            return None
        pattern = str(self.root / "parquet" / kind / "*.parquet").replace("'", "''")
        con = self._duckdb().connect()
        try:
            row: tuple[Any, ...] | None = con.execute(
                f"SELECT {select} FROM read_parquet('{pattern}')"
            ).fetchone()
        finally:
            con.close()
        return row
