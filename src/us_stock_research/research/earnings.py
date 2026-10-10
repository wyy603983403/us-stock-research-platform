"""Post-earnings drift by announcement return (study sp500_earnings_drift).

Events: 8-K filings (not 8-K/A) whose item list contains 2.02 ("Results of Operations"), stored
by ``collectors/sec_8k.py`` in ``sec_8k``. On a signal day each candidate's company is its CIK
that day (``meta/ticker_cik``); its most recent event d with (signal day - d) <= 92 calendar days
and the trading day after d on or before the signal day is used. EAR = the stock's return from
the close of the last trading day before d to the close of the first trading day after d, minus
SPY's over the same closes (prices may come from up to 5 trading days earlier, as in the engine).
If either end lacks a price the event is invalid and the stock is not ranked (no fall back to an
older event). Unranked candidates stay in the equal-weight universe with score -inf.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

RECENCY_DAYS = 92
STALE = 5
UNRANKED = float("-inf")


def is_earnings(form: str, items: str) -> bool:
    return form == "8-K" and "2.02" in {x.strip() for x in str(items).split(",")}


def load_events(tables: Any) -> dict[int, list[date]]:
    out: dict[int, set[date]] = {}
    for key in tables.keys("sec_8k"):
        for cik, form, d, items in tables.read("sec_8k", key, "cik, form, filing_date, items"):
            if is_earnings(str(form), str(items)):
                out.setdefault(int(cik), set()).add(d)
    return {c: sorted(v) for c, v in out.items()}


def window(days: list[date], d: date) -> tuple[int, int] | None:
    """Indices of the last trading day before ``d`` and the first one after it."""
    i_prev = bisect.bisect_left(days, d) - 1
    i_next = bisect.bisect_right(days, d)
    if i_prev < 0 or i_next >= len(days):
        return None
    return i_prev, i_next


@dataclass
class EarningsDrift:
    contract: dict[str, Any]
    prices: Any  # cross_section.Prices
    events: dict[int, list[date]]
    segments: dict[str, list[tuple[date, date | None, int | None]]]
    benchmark: str = "SPY"
    last_ear: dict[str, float] = field(default_factory=dict)

    def ear(self, symbol: str, d: date) -> float | None:
        w = window(self.prices.days, d)
        if w is None:
            return None
        legs = []
        for s in (symbol, self.benchmark):
            a, b = self.prices.at(s, w[0], STALE), self.prices.at(s, w[1], STALE)
            if not a or not b or a[1] <= 0:
                return None
            legs.append(b[1] / a[1] - 1)
        return legs[0] - legs[1]

    def latest_event(self, cik: int, t: int) -> date | None:
        day = self.prices.days[t]
        evs = self.events.get(cik) or []
        k = bisect.bisect_right(evs, day) - 1
        while k >= 0:
            d = evs[k]
            if (day - d).days > RECENCY_DAYS:
                return None
            w = window(self.prices.days, d)
            if w is not None and w[1] <= t:
                return d
            k -= 1  # announced but its reaction window has not closed yet
        return None

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        from us_stock_research.research.fundamentals import cik_on

        day = self.prices.days[t]
        scores: dict[str, float] = {}
        stats = {"candidates": len(candidates), "with_event": 0, "no_cik": 0, "invalid": 0}
        for s in candidates:
            cik = cik_on(self.segments.get(s, []), day)
            if cik is None:
                stats["no_cik"] += 1
                scores[s] = UNRANKED
                continue
            d = self.latest_event(cik, t)
            v = self.ear(s, d) if d is not None else None
            if d is not None and v is None:
                stats["invalid"] += 1
            if v is None:
                scores[s] = UNRANKED
                continue
            scores[s] = v
            stats["with_event"] += 1
        return scores, stats


def fingerprint(tables: Any) -> str:
    digest = hashlib.sha256()
    for key in tables.keys("sec_8k"):
        digest.update(key.encode())
        digest.update(hashlib.sha256(tables.path("sec_8k", key).read_bytes()).digest())
    digest.update(tables.path("meta", "ticker_cik").read_bytes())
    return "earnings-8k:sha256:" + digest.hexdigest()
