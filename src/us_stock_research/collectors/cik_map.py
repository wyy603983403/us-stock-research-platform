"""Historical ticker -> SEC CIK map for every S&P 500 membership interval (offline).

Form 4 filings state both the issuer's CIK and the ticker it traded under *at that time*, so the
insider data sets give dated (ticker, CIK) pairs back to 2006 -- including delisted companies and
tickers later re-used by someone else. For each membership interval the CIK whose filings under
that ticker overlap the interval the most is chosen; ties or no overlap are reported, not guessed.
``configs/cik_overrides.yml`` (``SYMBOL: CIK``) wins over the data when a human has checked.

Output: ``parquet/meta/ticker_cik.parquet`` (symbol, start_date, end_date, cik, issuer_name,
source, filings_in_interval).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.collectors.sec_bulk import INSIDER_KIND
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

SCHEMA = {
    "symbol": "VARCHAR",
    "start_date": "DATE",
    "end_date": "DATE",
    "cik": "BIGINT",
    "issuer_name": "VARCHAR",
    "source": "VARCHAR",
    "filings_in_interval": "INTEGER",
}
Filing = tuple[str, int, date, str]  # yahoo symbol, cik, filing date, issuer name


def yahoo(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def choose(
    intervals: list[tuple[str, date, date | None]],
    filings: dict[str, list[tuple[int, date, str]]],
    overrides: dict[str, int],
    since: date,
) -> tuple[list[list[Any]], dict[str, int]]:
    rows: list[list[Any]] = []
    stats = {"override": 0, "insider": 0, "ambiguous": 0, "unmapped": 0, "before_since": 0}
    for symbol, start, end in intervals:
        if end is not None and end <= since:
            stats["before_since"] += 1
            continue
        if symbol in overrides:
            rows.append([symbol, start, end, overrides[symbol], None, "override", None])
            stats["override"] += 1
            continue
        counts: dict[int, int] = {}
        names: dict[int, str] = {}
        for cik, filed, name in filings.get(symbol, []):
            if filed >= start and (end is None or filed < end):
                counts[cik] = counts.get(cik, 0) + 1
                names[cik] = name
        if not counts:
            rows.append([symbol, start, end, None, None, "unmapped", 0])
            stats["unmapped"] += 1
            continue
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        best, n = ranked[0]
        if len(ranked) > 1 and ranked[1][1] >= 0.5 * n:
            # two companies used the ticker inside one interval: needs a human
            rows.append([symbol, start, end, None, names[best], "ambiguous", n])
            stats["ambiguous"] += 1
            continue
        rows.append([symbol, start, end, best, names[best], "insider", n])
        stats["insider"] += 1
    return rows, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=date.fromisoformat, default=date(2009, 1, 1))
    parser.add_argument("--overrides", type=Path, default=Path("configs/cik_overrides.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    tables = TableStore.from_settings(load_settings())
    if not tables.keys(INSIDER_KIND):
        print(
            "no insider data yet: run usr-collect-sec-bulk --what insider --execute",
            file=sys.stderr,
        )
        return 2
    pattern = str(tables.root / "parquet" / INSIDER_KIND / "*.parquet").replace("'", "''")
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        raw = con.execute(
            "SELECT DISTINCT issuer_symbol, issuer_cik, filing_date, issuer_name "
            f"FROM read_parquet('{pattern}') "
            "WHERE issuer_symbol IS NOT NULL AND issuer_cik IS NOT NULL AND filing_date IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    filings: dict[str, list[tuple[int, date, str]]] = {}
    for sym, cik, filed, name in raw:
        for part in str(sym).replace(";", ",").split(","):  # some filings list several classes
            if part.strip():
                filings.setdefault(yahoo(part), []).append((int(cik), filed, name))
    overrides: dict[str, int] = {}
    if args.overrides.exists():
        overrides = {
            str(k): int(v) for k, v in (yaml.safe_load(args.overrides.read_text()) or {}).items()
        }
    history = [
        tuple(r) for r in tables.read("meta", "sp500_history", "symbol, start_date, end_date")
    ]
    rows, stats = choose(history, filings, overrides, args.since)  # type: ignore[arg-type]
    # a renamed ticker (FB -> META) filed under its new symbol after the rename; nothing to do,
    # the interval of each symbol is matched against filings made under that same symbol.
    tables.write("meta", "ticker_cik", SCHEMA, [[r[k] for r in rows] for k in range(7)], "symbol")
    unmapped = sorted({r[0] for r in rows if r[5] in ("unmapped", "ambiguous")})
    summary = {
        "intervals_considered": len(rows),
        "by_source": stats,
        "mapped_share": round(sum(1 for r in rows if r[3] is not None) / len(rows), 4)
        if rows
        else 0.0,
        "unmapped_or_ambiguous": unmapped,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(json.dumps({**summary, "unmapped_or_ambiguous": unmapped[:60]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
