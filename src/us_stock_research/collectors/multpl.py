"""S&P 500 monthly dividend yield (Shiller series) from multpl.com, for long-history total returns.

The Yahoo ``^GSPC`` index is price only. Studies that need the S&P 500's total return before
SPY existed (1993) add the dividend yield: ``parquet/macro/SP500_DIVYIELD`` (date, percent),
monthly back to the 1870s. Rows the site marks as estimates (a dagger) are skipped.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from typing import Any

import httpx

from us_stock_research.collectors.yahoo_daily import USER_AGENT
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

URL = "https://www.multpl.com/s-p-500-dividend-yield/table/by-month"
KEY = "SP500_DIVYIELD"
SCHEMA = {"date": "DATE", "value": "DOUBLE"}
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


def parse_table(page: str) -> list[tuple[date, float]]:
    out: dict[date, float] = {}
    for row in re.split(r"<tr[^>]*>", page, flags=re.I)[1:]:
        cells = [html.unescape(TAG.sub(" ", c)).strip() for c in CELL.findall(row)]
        if len(cells) < 2:
            continue
        try:
            day = datetime.strptime(" ".join(cells[0].split()), "%b %d, %Y").date()
        except ValueError:
            continue
        value = cells[1]
        if "†" in value or "estimate" in value.lower():
            continue
        match = NUMBER.search(value)
        if match:
            out[day] = float(match.group(1))
    return sorted(out.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(URL)
        response.raise_for_status()
    rows = parse_table(response.text)
    summary: dict[str, Any] = {
        "dry_run": not args.execute,
        "rows": len(rows),
        "first": rows[0][0].isoformat() if rows else None,
        "last": rows[-1][0].isoformat() if rows else None,
    }
    if not rows:
        print(json.dumps(summary, ensure_ascii=False))
        return 1
    if args.execute:
        TableStore.from_settings(settings).write(
            "macro", KEY, SCHEMA, [[d for d, _ in rows], [v for _, v in rows]], "date"
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
