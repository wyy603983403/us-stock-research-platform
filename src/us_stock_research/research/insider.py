"""Insider-buying score for the cross-section engine (study sp500_insider_buying_pit).

Input: ``parquet/sec_insider`` (SEC insider transactions data sets: one row per non-derivative
Form 3/4/5 transaction) and ``meta/ticker_cik`` (which filer a ticker was on a day).

On a signal day a member's score is the number of distinct directors or officers who bought
shares (code P, acquired, positive shares and price) in filings dated within the window ending
that day, plus v / (1 + v) with v the dollars bought in millions -- more buyers first, then more
money. Members without such purchases score 0. Only ``filing_date <= signal day`` is used.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from us_stock_research.research.fundamentals import cik_on

Buy = tuple[date, int, float]  # filing date, owner CIK, dollars


def is_insider(relationship: str | None) -> bool:
    text = relationship or ""
    return "Director" in text or "Officer" in text


def load_buys(tables: Any) -> tuple[dict[int, list[Buy]], str]:
    """Qualifying purchases per issuer CIK, sorted by filing date, and a hash of the rows."""
    pattern = str(tables.root / "parquet" / "sec_insider" / "*.parquet").replace("'", "''")
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        rows = con.execute(
            "SELECT issuer_cik, filing_date, owner_cik, shares * price, relationship "
            f"FROM read_parquet('{pattern}') WHERE trans_code = 'P' AND acquired_disposed = 'A' "
            "AND shares > 0 AND price > 0 AND issuer_cik IS NOT NULL AND owner_cik IS NOT NULL "
            "ORDER BY issuer_cik, filing_date, owner_cik, accession, shares, price"
        ).fetchall()
    finally:
        con.close()
    out: dict[int, list[Buy]] = {}
    digest = hashlib.sha256()
    for issuer, filed, owner, dollars, relationship in rows:
        if not is_insider(relationship):
            continue
        out.setdefault(int(issuer), []).append((filed, int(owner), float(dollars)))
        digest.update(repr((issuer, filed, owner, dollars)).encode())
    return out, "insider-buys:sha256:" + digest.hexdigest()


def score(buys: list[Buy], day: date, window_days: int) -> float:
    """Distinct buyers in (day - window, day] + v / (1 + v), v = dollars bought in millions."""
    lo = bisect.bisect_right(buys, (day - timedelta(days=window_days), 1 << 62, 0.0))
    hi = bisect.bisect_right(buys, (day, 1 << 62, float("inf")))
    recent = buys[lo:hi]
    if not recent:
        return 0.0
    v = sum(b[2] for b in recent) / 1e6
    return len({b[1] for b in recent}) + v / (1 + v)


@dataclass
class InsiderBuying:
    """Callable used by ``cross_section.run``: candidates on day index ``t`` -> scores, stats."""

    contract: dict[str, Any]
    days: list[date]
    buys: dict[int, list[Buy]]
    segments: dict[str, list[tuple[date, date | None, int | None]]]

    def __post_init__(self) -> None:
        self.window = int(self.contract["insider"]["window_days"])

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.days[t]
        scores: dict[str, float] = {}
        mapped = 0
        for s in candidates:
            cik = cik_on(self.segments.get(s, []), day)
            mapped += cik is not None
            scores[s] = score(self.buys.get(cik, []), day, self.window) if cik else 0.0
        return scores, {
            "candidates": len(candidates),
            "mapped_cik": mapped,
            "with_buys": sum(v > 0 for v in scores.values()),
        }
