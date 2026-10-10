"""8-K filing index of every filer in ``meta/ticker_cik`` (``usr-collect-8k``).

Source: SEC EDGAR submissions API, https://data.sec.gov/submissions/CIK##########.json (the
``recent`` block plus the older pages listed under ``filings.files``). Kept: every 8-K and 8-K/A
with its filing date, acceptance time and item list, one Parquet file per CIK in ``sec_8k``
(an empty file marks a filer with none, so reruns skip it). SEC asks for a contact in the
User-Agent (``SEC_USER_AGENT`` in .env) and at most 10 requests per second.

    usr-collect-8k --execute [--budget-seconds 160] [--refresh]
    usr-collect-8k --execute --from-raw DIR   # parse saved responses instead of downloading

Stops cleanly when the time budget is used up; run it again to continue. ``--from-raw`` reads
``CIK##########.json.gz`` files holding ``{"top": <submissions JSON>, "pages": {name: JSON}}``
(saved verbatim by a plain downloader where this package cannot be installed).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

KIND = "sec_8k"
BASE = "https://data.sec.gov/submissions/"
SCHEMA = {
    "cik": "BIGINT",
    "accession": "VARCHAR",
    "form": "VARCHAR",
    "filing_date": "DATE",
    "acceptance": "VARCHAR",
    "items": "VARCHAR",
}


def key_of(cik: int) -> str:
    return f"CIK{cik:010d}"


def parse_block(cik: int, block: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Columnar ``filings`` block -> 8-K / 8-K/A rows."""
    forms = block.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A"):
            continue
        out.append(
            (
                cik,
                str(block["accessionNumber"][i]),
                str(form),
                date.fromisoformat(block["filingDate"][i]),
                str((block.get("acceptanceDateTime") or [""] * len(forms))[i] or ""),
                str((block.get("items") or [""] * len(forms))[i] or ""),
            )
        )
    return out


def fetch_cik(client: httpx.Client, cik: int, pause: float) -> list[tuple[Any, ...]]:
    def get(name: str) -> dict[str, Any]:
        for attempt in range(4):
            r = client.get(BASE + name)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 + 3 * attempt)
                continue
            r.raise_for_status()
            time.sleep(pause)
            data: dict[str, Any] = r.json()
            return data
        raise RuntimeError("unreachable")

    top = get(f"{key_of(cik)}.json")
    filings = top.get("filings") or {}
    rows = parse_block(cik, filings.get("recent") or {})
    for page in filings.get("files") or []:
        rows += parse_block(cik, get(str(page["name"])))
    return dedupe(rows)


def rows_from_raw(cik: int, blob: dict[str, Any]) -> list[tuple[Any, ...]]:
    top = blob.get("top") or {}
    filings = top.get("filings") or {}
    rows = parse_block(cik, filings.get("recent") or {})
    for page in filings.get("files") or []:
        rows += parse_block(cik, (blob.get("pages") or {}).get(str(page["name"])) or {})
    return dedupe(rows)


def dedupe(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    seen: set[str] = set()
    unique = []
    for r in rows:
        if r[1] not in seen:
            seen.add(r[1])
            unique.append(r)
    return unique


def write_rows(store: TableStore, cik: int, rows: list[tuple[Any, ...]]) -> None:
    cols = [[r[i] for r in rows] for i in range(len(SCHEMA))]
    store.write(KIND, key_of(cik), SCHEMA, cols, "filing_date, accession")


def from_raw(store: TableStore, folder: Path, execute: bool) -> dict[str, Any]:
    done, missing = 0, []
    ciks = sorted({int(c) for (c,) in store.read("meta", "ticker_cik", "cik") if c is not None})
    for cik in ciks:
        path = folder / f"{key_of(cik)}.json.gz"
        if not path.exists():
            missing.append(key_of(cik))
            continue
        rows = rows_from_raw(cik, json.loads(gzip.decompress(path.read_bytes())))
        if execute:
            write_rows(store, cik, rows)
        done += 1
    return {"ciks": len(ciks), "parsed": done, "missing": missing, "dry_run": not execute}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--budget-seconds", type=float, default=160)
    parser.add_argument("--pause", type=float, default=0.12)
    parser.add_argument("--from-raw", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings()  # also loads .env
    if args.from_raw:
        result = from_raw(TableStore.from_settings(settings), args.from_raw, args.execute)
        print(json.dumps({**result, "missing": result["missing"][:20]}, indent=1))
        return 0 if not result["missing"] else 1
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        print('put SEC_USER_AGENT="Name email" in .env (SEC requires a contact)', file=sys.stderr)
        return 2
    store = TableStore.from_settings(settings)
    ciks = sorted({int(c) for (c,) in store.read("meta", "ticker_cik", "cik") if c is not None})
    start = time.monotonic()
    done, skipped, failed = 0, 0, {}
    todo = [c for c in ciks if args.refresh or not store.has(KIND, key_of(c))]
    skipped = len(ciks) - len(todo)
    with httpx.Client(timeout=60, headers={"User-Agent": agent}, follow_redirects=True) as client:
        for cik in todo:
            if time.monotonic() - start > args.budget_seconds:
                break
            try:
                rows = fetch_cik(client, cik, args.pause)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                failed[key_of(cik)] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            if args.execute:
                write_rows(store, cik, rows)
            done += 1
    remaining = len(todo) - done - len(failed)
    print(json.dumps({"ciks": len(ciks), "skipped": skipped, "done": done, "failed": failed,
                      "remaining": remaining, "dry_run": not args.execute}, indent=1))  # fmt: skip
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
