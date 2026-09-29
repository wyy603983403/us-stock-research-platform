"""Alpaca free-tier (IEX feed) 1-minute bars: the primary minute-bar source.

Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env (a free paper-trading account is enough;
the keys are used for market data only and no orders are ever placed).
Stored under kind ``intraday_1min_alpaca`` so it never overwrites the Tiingo series.
History on the free IEX feed starts 2020-07-27.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
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
from us_stock_research.storage import open_store
from us_stock_research.tables import TableStore

KIND = "intraday_1min_alpaca"
BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
RETRIES = 5


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


def to_alpaca(symbol: str) -> str:
    """Our keys follow Yahoo (BRK-B); Alpaca uses a dot for share classes (BRK.B)."""
    return symbol.upper().replace("-", ".")


def minute_symbols(symbols: list[str]) -> list[str]:
    """Daily symbols that trade on an exchange (drops ^GSPC, GC=F, DX-Y.NYB)."""
    return [s for s in symbols if not (s.startswith("^") or "=" in s or s.endswith(".NYB"))]


def _get(
    client: httpx.Client,
    url: str,
    params: dict[str, str | int],
    headers: dict[str, str],
    sleep: Callable[[float], None],
) -> httpx.Response:
    """Waits out per-minute throttling and brief network drops instead of giving up overnight."""
    for attempt in range(RETRIES):
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError:
            if attempt == RETRIES - 1:
                raise
            sleep(30)
            continue
        if response.status_code == 429:
            if attempt == RETRIES - 1:
                raise RateLimited(response.text[:200])
            sleep(60)
            continue
        return response
    raise AssertionError("unreachable")


def fetch_chunk(
    client: httpx.Client,
    symbol: str,
    start: date,
    end: date,
    key: str,
    secret: str,
    sleep: Callable[[float], None] = time.sleep,
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
    url = BARS_URL.format(symbol=to_alpaca(symbol))
    out: list[dict[str, Any]] = []
    while True:
        response = _get(client, url, params, headers, sleep)
        response.raise_for_status()
        payload = response.json()
        out.extend(parse_alpaca(payload.get("bars") or []))
        token = payload.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token


def collect_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Alpaca IEX minute bars (resumable).")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument(
        "--all-stored", action="store_true", help="every symbol that has daily bars stored"
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 7, 27))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--pause", type=float, default=0.3)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("put ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env first", file=sys.stderr)
        return 2
    symbols = [s.upper() for s in args.symbols]
    if args.all_stored:
        symbols += minute_symbols(open_store(settings).symbols())
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        print("give symbols or --all-stored", file=sys.stderr)
        return 2
    store = TableStore.from_settings(settings)
    with httpx.Client(timeout=120) as client:
        summary = download_intraday(
            symbols,
            store,
            lambda s, a, b: fetch_chunk(client, s, a, b, key, secret),
            args.start,
            args.end,
            execute=args.execute,
            pause=args.pause,
            kind=KIND,
            chunks=year_chunks,  # Alpaca pages, so no row cap per window
            log=lambda m: print(m, file=sys.stderr, flush=True),
        )
    empty = sorted(s for s, v in summary["done"].items() if not v["total_bars"])
    summary = {
        "dry_run": not args.execute,
        "symbols": len(symbols),
        "completed": len(summary["done"]),
        "no_data": empty,
        **summary,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    if len(symbols) > 20:  # keep the terminal readable for batch runs
        print(json.dumps({k: v for k, v in summary.items() if k != "done"}, indent=2))
    else:
        print(text)
    return 1 if summary["failed"] or summary["stopped_by_rate_limit"] else 0


if __name__ == "__main__":
    raise SystemExit(collect_main())
