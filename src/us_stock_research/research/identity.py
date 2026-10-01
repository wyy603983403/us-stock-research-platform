"""Which membership intervals' stored prices belong to a *different* company (ticker re-use).

A ticker's stored price history is that of the company using it last (Yahoo and Tiingo key
history by ticker). When S&P history lists the same ticker for an earlier, different company
(TT was American Standard/Trane in 2002-08, today's TT is the former Ingersoll-Rand), those
prices would be silently attributed to the wrong firm. Rules, all conservative:

1. **Different CIK** -- an earlier interval of a symbol whose SEC CIK (from ``meta/ticker_cik``)
   differs from the CIK of the symbol's latest interval is blocked, unless a human listed it
   in ``configs/identity_reviewed.yml`` as the same company (re-domicile, bankruptcy emergence).
2. **History starts too late** -- any interval (or CIK segment of an interval) that begins more
   than 30 days before the stored series does is blocked: the series is a later company's.
   Applied with the effective start ``max(start, first stored day of the data set)``.

Blocked intervals stay in the universe as *unpriced members* so coverage reports them honestly.
Output: ``parquet/meta/price_identity.parquet`` (symbol, start_date, end_date, reason).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

DATA_START = date(2000, 1, 3)  # first day of the daily store
GRACE = timedelta(days=30)
SCHEMA = {"symbol": "VARCHAR", "start_date": "DATE", "end_date": "DATE", "reason": "VARCHAR"}

Interval = tuple[str, date, date | None]
Segment = tuple[date, date | None, int | None, str | None]  # start, end, cik, issuer name


def build(
    history: list[Interval],
    segments: dict[str, list[Segment]],
    first_day: dict[str, date],
    same_company: set[str],
    spliced: set[str],
) -> list[tuple[str, date, date | None, str]]:
    """``same_company``: "SYM@start" keys a human confirmed; ``spliced``: symbols whose early
    history is borrowed from a successor ticker (dated aliases) and is therefore correct."""
    by_symbol: dict[str, list[tuple[date, date | None]]] = {}
    for sym, start, end in sorted(history, key=lambda h: (h[0], h[1])):
        by_symbol.setdefault(sym, []).append((start, end))
    out: list[tuple[str, date, date | None, str]] = []
    for sym, intervals in by_symbol.items():
        segs = sorted(segments.get(sym, []), key=lambda s: s[0])
        latest_cik = next((s[2] for s in reversed(segs) if s[2] is not None), None)
        first = first_day.get(sym)
        for start, end in intervals:
            key = f"{sym}@{start.isoformat()}"
            if key in same_company or sym in spliced:
                continue
            inside = [s for s in segs if s[0] >= start and (end is None or s[0] < end)]
            ciks = {s[2] for s in inside if s[2] is not None}
            is_latest = end is None or (start, end) == intervals[-1]
            if not is_latest and latest_cik is not None and ciks and latest_cik not in ciks:
                out.append((sym, start, end, f"cik {sorted(ciks)} != latest {latest_cik}"))
                continue
            if first is None:
                continue  # no prices at all: already unpriced
            eff = max(start, DATA_START)
            if first > eff + GRACE and (end is None or first < end):
                # series begins inside the interval: block the part before it trades plus,
                # when another company held the ticker then, the whole interval
                other = latest_cik is not None and any(
                    s[2] not in (None, latest_cik) for s in inside
                )
                stop = end if other else first
                out.append((sym, start, stop, f"stored prices start {first}"))
            # within-interval CIK change whose part the series cannot cover (IR in 2020)
            for seg_start, seg_end, cik, _name in inside:
                if cik not in (None, latest_cik) and first > seg_start + GRACE:
                    out.append((sym, seg_start, seg_end, f"segment cik {cik} before series"))
    return sorted(set(out), key=lambda r: (r[0], r[1]))


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.research.cross_section import load_aliases

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed", type=Path, default=Path("configs/identity_reviewed.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    tables = TableStore.from_settings(load_settings())
    history = [
        tuple(r) for r in tables.read("meta", "sp500_history", "symbol, start_date, end_date")
    ]
    segments: dict[str, list[Segment]] = {}
    if tables.has("meta", "ticker_cik"):
        for sym, a, b, cik, name in tables.read(
            "meta", "ticker_cik", "symbol, start_date, end_date, cik, issuer_name"
        ):
            segments.setdefault(sym, []).append((a, b, cik, name))
    first_day: dict[str, date] = {}
    for kind in ("daily_delisted", "daily"):  # Yahoo wins, as in load_prices
        if not tables.keys(kind):
            continue
        pattern = str(tables.root / "parquet" / kind / "*.parquet").replace("'", "''")
        con = tables._duckdb().connect()  # noqa: SLF001
        try:
            rows = con.execute(
                "SELECT regexp_extract(filename, '([^/]+)\\.parquet$', 1), min(date) "
                f"FROM read_parquet('{pattern}', filename=true) WHERE adj_close > 0 GROUP BY 1"
            ).fetchall()
        finally:
            con.close()
        first_day.update({s: d for s, d in rows})
    aliases = load_aliases(args.aliases)
    spliced = {k.split("@")[0] for k in aliases if "@" in k}
    for old, new in aliases.items():
        if "@" not in old and new in first_day:
            first_day.setdefault(old, first_day[new])
    same: set[str] = set()
    if args.reviewed.exists():
        same = set((yaml.safe_load(args.reviewed.read_text()) or {}).get("same_company") or [])
    rows_out = build(history, segments, first_day, same, spliced)  # type: ignore[arg-type]
    tables.write(
        "meta",
        "price_identity",
        SCHEMA,
        [[r[k] for r in rows_out] for k in range(4)],
        "symbol, start_date",
    )
    summary: dict[str, Any] = {
        "blocked": len(rows_out),
        "rows": [f"{s} {a}..{b or '-'}: {why}" for s, a, b, why in rows_out],
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
