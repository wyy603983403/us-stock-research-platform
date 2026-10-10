"""FINRA semi-monthly short interest files (``usr-collect-short``).

Source: https://cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv (pipe-separated, one
file per settlement date; exchange-listed stocks included from 2017-12-29). Settlement dates are
found by probing every calendar day in the range (missing days return 403/404). Each file is kept
as ``short_interest/YYYYMMDD`` with columns ``settlement, symbol, market, short_qty, adv,
revision``; already stored dates are skipped.

    usr-collect-short --start 2017-12-01 --end 2026-01-31 --execute
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import httpx

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

KIND = "short_interest"
URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.csv"
SCHEMA = {
    "settlement": "DATE",
    "symbol": "VARCHAR",
    "market": "VARCHAR",
    "short_qty": "DOUBLE",
    "adv": "DOUBLE",
    "revision": "VARCHAR",
}


def num(text: str) -> float | None:
    text = (text or "").strip().replace(",", "")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse(text: str, settlement: date) -> list[tuple[Any, ...]]:
    rows = []
    for rec in csv.DictReader(io.StringIO(text), delimiter="|", quoting=csv.QUOTE_NONE):
        sym = (rec.get("symbolCode") or "").strip()
        if not sym:
            continue
        rows.append(
            (
                settlement,
                sym,
                (rec.get("marketClassCode") or "").strip(),
                num(rec.get("currentShortPositionQuantity", "")),
                num(rec.get("averageDailyVolumeQuantity", "")),
                (rec.get("revisionFlag") or "").strip(),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2017, 12, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    store = TableStore.from_settings(load_settings())
    days = [args.start + timedelta(i) for i in range((args.end - args.start).days + 1)]
    todo = [d for d in days if not store.has(KIND, f"{d:%Y%m%d}")]

    def fetch(d: date) -> tuple[date, str | None]:
        with httpx.Client(timeout=60) as client:
            for _ in range(3):
                try:
                    r = client.get(URL.format(d=d))
                except httpx.HTTPError:
                    continue
                if r.status_code in (403, 404):
                    return d, None
                if r.status_code == 200:
                    return d, r.text
            raise RuntimeError(f"{d}: no answer")

    stored, failed = [], []
    with ThreadPoolExecutor(args.workers) as ex:
        for d, text in ex.map(lambda x: _safe(fetch, x), todo):
            if text is None:
                continue
            if text == "":
                failed.append(d.isoformat())
                continue
            rows = parse(text, d)
            if args.execute and rows:
                cols = [[r[i] for r in rows] for i in range(len(SCHEMA))]
                store.write(KIND, f"{d:%Y%m%d}", SCHEMA, cols, "symbol")
            stored.append(d.isoformat())
    print(json.dumps({"probed": len(todo), "files": len(stored), "failed": failed,
                      "first": stored[:1], "last": stored[-1:],
                      "dry_run": not args.execute}, indent=1))  # fmt: skip
    return 0 if not failed else 1


def _safe(fetch: Any, d: date) -> tuple[date, str | None]:
    try:
        result: tuple[date, str | None] = fetch(d)
        return result
    except RuntimeError:
        return d, ""


if __name__ == "__main__":
    sys.exit(main())
