from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from us_stock_research.bars import DailyBar, symbol_path, write_bars


def synthetic_bars(start: date, n: int, drift: float, amp: float, phase: float) -> list[DailyBar]:
    bars, price, d = [], 100.0, start
    while len(bars) < n:
        if d.weekday() < 5:
            k = len(bars)
            price *= 1 + drift + amp * math.sin(k / 40 + phase)
            bars.append(DailyBar(d, price, price * 1.01, price * 0.99, price, price, 1_000_000))
        d += timedelta(days=1)
    return bars


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    specs = {
        "SPY": (0.0004, 0.01, 0.0),
        "QQQ": (0.0005, 0.012, 1.0),
        "IEF": (0.0001, 0.002, 2.0),
        "SHY": (0.00005, 0.0, 0.0),
    }
    for sym, (drift, amp, phase) in specs.items():
        write_bars(
            symbol_path(tmp_path, sym), synthetic_bars(date(2010, 1, 4), 2600, drift, amp, phase)
        )
    return tmp_path
