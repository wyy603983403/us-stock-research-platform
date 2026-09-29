"""Alpaca free-tier (IEX feed) 1-minute bars: a second source to cross-check Tiingo.

Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env (a free paper-trading account is enough;
these keys are read-only market-data use here and no orders are ever placed).
Stored under kind ``intraday_1min_alpaca`` so it never overwrites the Tiingo series.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.collectors.tiingo_intraday import (
    RateLimited,
    download_intraday,
    year_chunks,
)
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

KIND = "intraday_1min_alpaca"
BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def parse_alpaca(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ts": datetime.fromisoformat(r["t"].replace("Z", "+00:00")),
            "open": float(r["o"]),
            "high": float(r["h"]),
            "low": float(r["l"]),
            "close": float(r["c"]),
            "volume": float(r["v"]),
        }
        for r in rows
    ]


def fetch_chunk(
    client: httpx.Client, symbol: str, start: date, end: date, key: str, secret: str
) -> list[dict[str, Any]]:
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: dict[str, str | int] = {
        "timeframe": "1Min",
        "start": f"{start.isoformat()}T00:00:00Z",
        "end": f"{end.isoformat()}T23:59:59Z",
        "feed": "iex",
        "adjustment": "raw",
        "limit": 10000,
    }
    out: list[dict[str, Any]] = []
    while True:
        response = client.get(
            BARS_URL.format(symbol=symbol.upper()), params=params, headers=headers
        )
        if response.status_code == 429:
            raise RateLimited(response.text[:200])
        response.raise_for_status()
        payload = response.json()
        out.extend(parse_alpaca(payload.get("bars") or []))
        token = payload.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token


def collect_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Alpaca IEX minute bars (resumable).")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2017, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("put ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env first", file=sys.stderr)
        return 2
    store = TableStore.from_settings(settings)
    with httpx.Client(timeout=120) as client:
        summary = download_intraday(
            [s.upper() for s in args.symbols],
            store,
            lambda s, a, b: fetch_chunk(client, s, a, b, key, secret),
            args.start,
            args.end,
            execute=args.execute,
            pause=args.pause,
            kind=KIND,
            chunks=year_chunks,  # Alpaca pages, so no row cap per window
        )
    summary = {"dry_run": not args.execute, **summary}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if summary["failed"] or summary["stopped_by_rate_limit"] else 0


if __name__ == "__main__":
    raise SystemExit(collect_main())
