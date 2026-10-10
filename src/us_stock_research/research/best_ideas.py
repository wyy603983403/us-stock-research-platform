"""Managers' "best ideas" from SEC 13F (study sp500_best_ideas_13f).

Inputs (local store): ``parquet/sec13f/*.parquet`` -- 13F-HR holdings already restricted to CUSIPs
that ever traded under an S&P 500 member's ticker (``accession, cik, form, filing_date, period,
cusip, value``); ``meta/ftd_cusip`` -- SEC fails-to-deliver records reduced to the last date each
(CUSIP, symbol) pair appeared in each month (``cusip, symbol, seen``).

On a signal day: each manager's latest original 13F-HR filed by then with a report period at most
200 days old; holdings mapped CUSIP -> the symbol it last traded under within the previous 365
days (if that symbol is a member that day); managers holding 10-150 members; weights within the
manager; consensus = mean weight across managers (0 when not held); a manager's best idea =
largest (weight - consensus), ties by larger weight then symbol. Score = number of managers whose
best idea it is + 0.001 x the sum of their overweights.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

MIN_POS, MAX_POS = 10, 150
MAX_PERIOD_AGE = 200
MAP_WINDOW = 365


def load_ftd(tables: Any) -> dict[str, tuple[list[date], list[str]]]:
    rows = tables.read("meta", "ftd_cusip", "cusip, symbol, seen")
    out: dict[str, list[tuple[date, str]]] = {}
    for c, s, d in rows:
        out.setdefault(str(c), []).append((d, str(s)))
    res = {}
    for c, ev in out.items():
        ev.sort()
        res[c] = ([d for d, _ in ev], [s for _, s in ev])
    return res


def symbol_on(ftd: dict[str, tuple[list[date], list[str]]], cusip: str, day: date) -> str | None:
    ev = ftd.get(cusip)
    if not ev:
        return None
    k = bisect.bisect_right(ev[0], day) - 1
    if k < 0 or (day - ev[0][k]).days > MAP_WINDOW:
        return None
    return ev[1][k]


def best_idea_scores(
    filings: dict[int, dict[str, float]], members: set[str]
) -> tuple[dict[str, float], int]:
    """``filings``: manager -> {symbol: value} (members only). Returns scores and managers used."""
    weights: list[dict[str, float]] = []
    for pos in filings.values():
        held = {s: v for s, v in pos.items() if s in members and v > 0}
        if not MIN_POS <= len(held) <= MAX_POS:
            continue
        total = sum(held.values())
        weights.append({s: v / total for s, v in held.items()})
    n = len(weights)
    if not n:
        return {}, 0
    consensus: dict[str, float] = {}
    for w in weights:
        for s, x in w.items():
            consensus[s] = consensus.get(s, 0.0) + x
    consensus = {s: x / n for s, x in consensus.items()}
    count: dict[str, int] = {}
    tilt_sum: dict[str, float] = {}
    for w in weights:
        best = min(w, key=lambda s: (consensus[s] - w[s], -w[s], s))
        count[best] = count.get(best, 0) + 1
        tilt_sum[best] = tilt_sum.get(best, 0.0) + (w[best] - consensus[best])
    return {s: count[s] + 0.001 * tilt_sum[s] for s in count}, n


@dataclass
class BestIdeas:
    """Callable for ``cross_section.run``: candidates on day index ``t`` -> scores, stats."""

    contract: dict[str, Any]
    days: list[date]
    tables: Any
    ftd: dict[str, tuple[list[date], list[str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import duckdb

        self.ftd = self.ftd or load_ftd(self.tables)
        pattern = str(self.tables.root / "parquet" / "sec13f" / "*.parquet").replace("'", "''")
        self.con = duckdb.connect()
        self.con.execute(
            "CREATE TABLE h AS SELECT cik, accession, filing_date, period, cusip, value "
            f"FROM read_parquet('{pattern}') WHERE form = '13F-HR' AND value > 0"
        )

    def filings_on(self, day: date) -> dict[int, list[tuple[str, float]]]:
        rows = self.con.execute(
            """
            WITH f AS (
              SELECT DISTINCT cik, accession, filing_date, period FROM h
              WHERE filing_date <= ? AND period >= ?
            ), pick AS (
              SELECT cik, arg_max(accession, (period, filing_date, accession)) AS accession FROM f
              GROUP BY cik
            )
            SELECT h.cik, h.cusip, sum(h.value) FROM h JOIN pick USING (cik, accession)
            GROUP BY h.cik, h.cusip
            """,
            [day, day - timedelta(days=MAX_PERIOD_AGE)],
        ).fetchall()
        out: dict[int, list[tuple[str, float]]] = {}
        for cik, cusip, v in rows:
            out.setdefault(int(cik), []).append((str(cusip), float(v)))
        return out

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.days[t]
        members = set(candidates)
        mapped = {symbol_on(self.ftd, c, day) for c in self.ftd}
        filings: dict[int, dict[str, float]] = {}
        for cik, rows in self.filings_on(day).items():
            pos: dict[str, float] = {}
            for cusip, v in rows:
                s = symbol_on(self.ftd, cusip, day)
                if s in members:
                    pos[s] = pos.get(s, 0.0) + v
            filings[cik] = pos
        scores, n = best_idea_scores(filings, members)
        return {s: scores.get(s, 0.0) for s in candidates}, {
            "candidates": len(candidates),
            "mapped": len(members & mapped),
            "managers": n,
            "with_best_idea": sum(1 for s in candidates if scores.get(s, 0) > 0),
        }


def fingerprint(tables: Any) -> str:
    digest = hashlib.sha256()
    for kind in ("sec13f",):
        for key in tables.keys(kind):
            digest.update(tables.path(kind, key).read_bytes())
    digest.update(tables.path("meta", "ftd_cusip").read_bytes())
    return "best-ideas:sha256:" + digest.hexdigest()
