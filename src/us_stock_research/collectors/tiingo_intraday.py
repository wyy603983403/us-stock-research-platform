"""Tiingo IEX intraday bars (1-minute and coarser): probe tool and resumable downloader.

``usr-probe-intraday`` asks Tiingo for a few short windows of one symbol at different dates and
reports how many bars came back, which columns exist, and any error (permissions, rate limit,
history limit), so you can see what the account's plan really provides before downloading.
Needs ``TIINGO_TOKEN`` in ``.env``. Note: Tiingo's IEX feed only covers trades on the IEX exchange.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

IEX_URL = "https://api.tiingo.com/iex/{symbol}/prices"


def parse_iex(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "ts": datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00")),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
            }
        )
    return out


def probe_window(
    client: httpx.Client, symbol: str, start: date, end: date, freq: str, token: str
) -> dict[str, Any]:
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "resampleFreq": freq,
        "columns": "open,high,low,close,volume",
        "token": token,
    }
    try:
        response = client.get(IEX_URL.format(symbol=symbol.lower()), params=params)
    except httpx.HTTPError as exc:
        return {"window": f"{start}..{end}", "error": f"{type(exc).__name__}"}
    result: dict[str, Any] = {"window": f"{start}..{end}", "status": response.status_code}
    if response.status_code != 200:
        # Error bodies are short plain text or JSON detail; the token is never echoed back.
        result["message"] = response.text[:200].replace(token, "***")
        return result
    try:
        bars = parse_iex(response.json())
    except (ValueError, KeyError) as exc:
        result["error"] = f"unparseable: {type(exc).__name__}"
        return result
    result["bars"] = len(bars)
    if bars:
        result["first"] = bars[0]["ts"].isoformat()
        result["last"] = bars[-1]["ts"].isoformat()
        result["sample_volume"] = bars[len(bars) // 2]["volume"]
    return result


KIND = "intraday_1min"
SCHEMA = {
    "ts": "TIMESTAMP",  # naive UTC
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "DOUBLE",  # IEX-only volume: a small fraction of consolidated volume
}
COLUMNS = ("ts", "open", "high", "low", "close", "volume")


class RateLimited(Exception):
    """Tiingo refused the request because a quota was hit; stop instead of hammering."""


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-year windows covering [start, end] (one request each)."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(date(cursor.year, 12, 31), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def fetch_chunk(
    client: httpx.Client, symbol: str, start: date, end: date, freq: str, token: str
) -> list[dict[str, Any]]:
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "resampleFreq": freq,
        "columns": "open,high,low,close,volume",
        "token": token,
    }
    response = client.get(IEX_URL.format(symbol=symbol.lower()), params=params)
    if response.status_code == 429 or (
        response.status_code == 400 and "limit" in response.text.lower()
    ):
        raise RateLimited(response.text[:200].replace(token, "***"))
    response.raise_for_status()
    return parse_iex(response.json())


def download_intraday(
    symbols: list[str],
    store: Any,
    fetch: Callable[[str, date, date], list[dict[str, Any]]],
    start: date,
    end: date,
    *,
    execute: bool,
    pause: float,
    sleep: Callable[[float], None] = time.sleep,
    kind: str = KIND,
) -> dict[str, Any]:
    """Resumable: continues after the last stored bar. Checkpoints after every yearly chunk."""
    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    stopped: str | None = None
    for symbol in symbols:
        existing: dict[datetime, tuple[float, ...]] = {}
        if store.has(kind, symbol):
            for row in store.read(kind, symbol, ", ".join(COLUMNS)):
                existing[row[0]] = tuple(row[1:])
        first_day = max(start, max(existing).date()) if existing else start
        fetched = 0
        try:
            for a, b in year_chunks(first_day, end):
                bars = fetch(symbol, a, b)
                for bar in bars:
                    ts = bar["ts"].astimezone(UTC).replace(tzinfo=None)
                    existing[ts] = (
                        bar["open"],
                        bar["high"],
                        bar["low"],
                        bar["close"],
                        bar["volume"],
                    )
                fetched += len(bars)
                if execute and bars:
                    ordered = sorted(existing)
                    store.write(
                        kind,
                        symbol,
                        SCHEMA,
                        [ordered] + [[existing[t][i] for t in ordered] for i in range(5)],
                        "ts",
                    )
                sleep(pause)
        except RateLimited as exc:
            stopped = f"{symbol}: {exc}"
            break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            failed[symbol] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        days = {t.date() for t in existing}
        done[symbol] = {
            "new_bars": fetched,
            "total_bars": len(existing),
            "days": len(days),
            "first": min(existing).isoformat() if existing else None,
            "last": max(existing).isoformat() if existing else None,
        }
    return {"done": done, "failed": failed, "stopped_by_rate_limit": stopped}


def collect_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Tiingo IEX minute bars (resumable).")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2017, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--freq", default="1min")
    parser.add_argument("--pause", type=float, default=2.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    token = os.environ.get("TIINGO_TOKEN")
    if not token:
        print("put TIINGO_TOKEN=<token> in .env first", file=sys.stderr)
        return 2
    store = TableStore.from_settings(settings)
    with httpx.Client(timeout=120) as client:
        summary = download_intraday(
            [s.upper() for s in args.symbols],
            store,
            lambda s, a, b: fetch_chunk(client, s, a, b, args.freq, token),
            args.start,
            args.end,
            execute=args.execute,
            pause=args.pause,
        )
    summary = {"dry_run": not args.execute, **summary}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if summary["failed"] or summary["stopped_by_rate_limit"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", nargs="?", default="SPY")
    parser.add_argument("--freq", default="1min", help="1min, 5min, 15min, 1hour")
    args = parser.parse_args(argv)
    load_settings()  # loads .env
    token = os.environ.get("TIINGO_TOKEN")
    if not token:
        print("put TIINGO_TOKEN=<token> in .env first", file=sys.stderr)
        return 2
    windows = [
        (date(2016, 6, 1), date(2016, 6, 3)),
        (date(2018, 6, 4), date(2018, 6, 6)),
        (date(2021, 6, 1), date(2021, 6, 3)),
        (date(2024, 6, 3), date(2024, 6, 5)),
        (date(2026, 9, 21), date(2026, 9, 23)),
    ]
    report: list[dict[str, Any]] = []
    with httpx.Client(timeout=60) as client:
        for start, end in windows:
            report.append(probe_window(client, args.symbol, start, end, args.freq, token))
    print(
        json.dumps({"symbol": args.symbol.upper(), "freq": args.freq, "windows": report}, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
