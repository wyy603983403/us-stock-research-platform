"""NYSE trading calendar (regular full-day closures) computed from the exchange's rules.

Valid for 1998 onwards. Half days (day after Thanksgiving, Christmas Eve, July 3) are open
trading days and are not modelled. Verified against real SPY history in the tests' fixtures
and by ``usr-audit-ohlcv``. Special one-off closures are listed explicitly.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

# National days of mourning, terror attacks, storms.
SPECIAL_CLOSURES = frozenset(
    {
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        date(2004, 6, 11),  # Reagan
        date(2007, 1, 2),  # Ford
        date(2012, 10, 29),  # Sandy
        date(2012, 10, 30),
        date(2018, 12, 5),  # Bush
        date(2025, 1, 9),  # Carter
    }
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date | None:
    """Saturday holidays close Friday; Sunday holidays close Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@cache
def nyse_holidays(year: int) -> frozenset[date]:
    days: set[date] = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # Saturday New Year: the previous Friday stays open
        obs = _observed(new_year)
        if obs:
            days.add(obs)
    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        obs = _observed(date(year, 6, 19))  # Juneteenth
        if obs:
            days.add(obs)
    for month, day in ((7, 4), (12, 25)):
        obs = _observed(date(year, month, day))
        if obs:
            days.add(obs)
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    return frozenset(d for d in days if d.year == year) | {
        d for d in SPECIAL_CLOSURES if d.year == year
    }


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def trading_days(start: date, end: date) -> list[date]:
    out: list[date] = []
    day = start
    while day <= end:
        if is_trading_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out
