"""Cross-check the primary source (Yahoo) against an independent second source.

Yahoo is an unofficial endpoint; promotion needs agreement with a second vendor. This compares
dates and *daily returns of the raw close* (split-adjusted on both sides; dividend adjustment is
deliberately not compared because vendors differ). Second-source data comes either from Stooq's
CSV download or from a CSV you supply with ``--file SYMBOL=path.csv`` (columns ``Date,Close`` or
``date,close``), so any vendor can be used.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.bars import DailyBar
from us_stock_research.storage import add_store_args, open_store, settings_from_args

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d&apikey={apikey}"
APIKEY_HELP = (
    "Stooq needs a free API key since 2026-03: open https://stooq.com/q/d/?s=spy.us&get_apikey "
    "in a browser, solve the check, and put STOOQ_APIKEY=<key> in .env"
)
RETURN_TOLERANCE = 0.005  # daily return may differ by 0.5 percentage points
MAX_BAD_RETURN_FRACTION = 0.01
MIN_COVERAGE = 0.98


def parse_close_csv(text: str) -> dict[date, float]:
    """Parse a vendor CSV with date and close columns (case-insensitive)."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise ValueError("second source returned no rows")
    keys = {k.lower(): k for k in rows[0]}
    if "date" not in keys or "close" not in keys:
        raise ValueError(f"second source is not a price CSV: {text[:80]!r}")
    out: dict[date, float] = {}
    for row in rows:
        try:
            out[date.fromisoformat(row[keys["date"]])] = float(row[keys["close"]])
        except ValueError:
            continue
    return out


def compare(
    primary: list[DailyBar], other: dict[date, float], adjusted: bool = False
) -> dict[str, Any]:
    mine = {b.day: (b.adj_close if adjusted else b.close) for b in primary}
    common = sorted(set(mine) & set(other))
    lo, hi = (common[0], common[-1]) if common else (None, None)
    window_mine = {d for d in mine if lo and hi and lo <= d <= hi}
    window_other = {d for d in other if lo and hi and lo <= d <= hi}
    coverage = len(common) / max(min(len(window_mine), len(window_other)), 1) if common else 0.0
    diffs: list[tuple[date, float]] = []
    for a, b in zip(common, common[1:], strict=False):
        ra, rb = mine[b] / mine[a] - 1, other[b] / other[a] - 1
        diffs.append((b, abs(ra - rb)))
    bad = [(d, x) for d, x in diffs if x > RETURN_TOLERANCE]
    worst = max(diffs, key=lambda t: t[1]) if diffs else None
    fraction_bad = len(bad) / max(len(diffs), 1)
    return {
        "common_days": len(common),
        "first": lo.isoformat() if lo else None,
        "last": hi.isoformat() if hi else None,
        "coverage": round(coverage, 4),
        "days_only_in_primary": len(window_mine - set(common)),
        "days_only_in_second": len(window_other - set(common)),
        "return_mismatch_days": len(bad),
        "return_mismatch_fraction": round(fraction_bad, 5),
        "worst_return_diff": {"date": worst[0].isoformat(), "diff": worst[1]} if worst else None,
        "first_mismatches": [d.isoformat() for d, _ in bad[:5]],
        "passed": bool(common)
        and coverage >= MIN_COVERAGE
        and fraction_bad <= MAX_BAD_RETURN_FRACTION,
    }


def fetch_stooq(client: httpx.Client, symbol: str, apikey: str | None) -> dict[date, float]:
    if not apikey:
        raise ValueError(APIKEY_HELP)
    response = client.get(STOOQ_URL.format(symbol=symbol.lower().replace("-", "."), apikey=apikey))
    response.raise_for_status()
    return parse_close_csv(response.text)


TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
TIINGO_HELP = (
    "Tiingo needs a free API token: register at https://www.tiingo.com (email only), copy the "
    "token from the account page and put TIINGO_TOKEN=<token> in .env"
)


def parse_tiingo(rows: list[dict[str, Any]]) -> dict[date, float]:
    """Tiingo daily rows -> {date: adjClose} (split- and dividend-adjusted)."""
    return {date.fromisoformat(r["date"][:10]): float(r["adjClose"]) for r in rows}


def fetch_tiingo(client: httpx.Client, symbol: str, token: str | None) -> dict[date, float]:
    if not token:
        raise ValueError(TIINGO_HELP)
    response = client.get(
        TIINGO_URL.format(symbol=symbol.lower()),
        params={"startDate": "2000-01-01", "format": "json", "token": token},
    )
    response.raise_for_status()
    return parse_tiingo(response.json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    add_store_args(parser)
    parser.add_argument(
        "--file", action="append", default=[], metavar="SYMBOL=PATH", help="second-source CSV"
    )
    parser.add_argument("--source", choices=("tiingo", "stooq"), default="tiingo")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    settings = settings_from_args(args)  # also loads .env into the environment
    files = dict(item.split("=", 1) for item in args.file)
    store = open_store(settings)
    reports: dict[str, Any] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for symbol in args.symbols:
            key = symbol.upper()
            try:
                adjusted = args.source == "tiingo" and key not in files
                if key in files:
                    other = parse_close_csv(Path(files[key]).read_text())
                elif args.source == "tiingo":
                    other = fetch_tiingo(client, symbol, os.environ.get("TIINGO_TOKEN"))
                else:
                    other = fetch_stooq(client, symbol, os.environ.get("STOOQ_APIKEY"))
                reports[key] = compare(store.read_bars(symbol), other, adjusted)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                reports[key] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    out = {"passed": all(r["passed"] for r in reports.values()), "symbols": reports}
    text = json.dumps(out, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
