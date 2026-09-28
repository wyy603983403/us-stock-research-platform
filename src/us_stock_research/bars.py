"""Daily bar model and CSV storage shared by collectors, quality gate and backtests.

Layout: ``<data_dir>/daily/<SYMBOL>.csv`` with columns
``date,open,high,low,close,adj_close,volume`` sorted ascending by date (UTC exchange date).
``adj_close`` includes splits and dividends and is what backtests use for returns.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

COLUMNS = ("date", "open", "high", "low", "close", "adj_close", "volume")


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int


def symbol_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / "daily" / f"{symbol.upper()}.csv"


def write_bars(path: Path, bars: list[DailyBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for b in sorted(bars, key=lambda x: x.day):
            writer.writerow(
                [
                    b.day.isoformat(),
                    repr(b.open),
                    repr(b.high),
                    repr(b.low),
                    repr(b.close),
                    repr(b.adj_close),
                    b.volume,
                ]
            )
    tmp.replace(path)


def read_bars(path: Path) -> list[DailyBar]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f"{path}: unexpected header {reader.fieldnames}")
        return [
            DailyBar(
                day=date.fromisoformat(row["date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                adj_close=float(row["adj_close"]),
                volume=int(float(row["volume"])),
            )
            for row in reader
        ]


def merge_bars(existing: list[DailyBar], new: list[DailyBar]) -> list[DailyBar]:
    """Idempotent upsert keyed by date; newer download wins (adjustments can be restated)."""
    by_day = {b.day: b for b in existing}
    by_day.update({b.day: b for b in new})
    return [by_day[d] for d in sorted(by_day)]
