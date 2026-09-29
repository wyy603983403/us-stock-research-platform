"""Stock split history from Yahoo's chart endpoint (``events=split``).

Minute bars are stored unadjusted while Yahoo's daily ``close`` is split-adjusted, so the split
table is what links the two, and a backtest needs it to turn target weights into share counts.
Stored as ``parquet/splits/<SYMBOL>.parquet`` with ``date, numerator, denominator`` (a 4-for-1
split is 4/1). Symbols without splits get no file; the report lists them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.collectors.alpaca_intraday import minute_symbols
from us_stock_research.collectors.yahoo_daily import CHART_URL, USER_AGENT
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store
from us_stock_research.tables import TableStore

KIND = "splits"
SCHEMA = {"date": "DATE", "numerator": "DOUBLE", "denominator": "DOUBLE"}


def parse_splits(payload: dict[str, Any]) -> list[tuple[date, float, float]]:
    result = payload["chart"]["result"][0]
    offset = int(result.get("meta", {}).get("gmtoffset", 0))
    events = (result.get("events") or {}).get("splits") or {}
    out: list[tuple[date, float, float]] = []
    for item in events.values():
        day = datetime.fromtimestamp(int(item["date"]) + offset, tz=UTC).date()
        out.append((day, float(item["numerator"]), float(item["denominator"])))
    return sorted(out)


def fetch_splits(client: httpx.Client, symbol: str) -> list[tuple[date, float, float]]:
    params: dict[str, str | int] = {
        "period1": 946684800,  # 2000-01-01
        "period2": int(time.time()),
        "interval": "3mo",  # splits come with any interval; coarse bars keep the payload tiny
        "events": "split",
    }
    response = client.get(CHART_URL.format(symbol=symbol.upper()), params=params)
    response.raise_for_status()
    return parse_splits(response.json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="default: every stored exchange-traded symbol")
    parser.add_argument("--pause", type=float, default=0.3)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    symbols = [s.upper() for s in args.symbols] or minute_symbols(open_store(settings).symbols())
    store = TableStore.from_settings(settings)
    with_splits: dict[str, list[str]] = {}
    none: list[str] = []
    failed: dict[str, str] = {}
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        for i, symbol in enumerate(symbols, 1):
            print(f"[{i}/{len(symbols)}] {symbol}", file=sys.stderr, flush=True)
            try:
                splits = fetch_splits(client, symbol)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                failed[symbol] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            finally:
                time.sleep(args.pause)
            if not splits:
                none.append(symbol)
                continue
            with_splits[symbol] = [f"{d} {n:g}:{m:g}" for d, n, m in splits]
            if args.execute:
                store.write(
                    KIND, symbol, SCHEMA, [list(c) for c in zip(*splits, strict=True)], "date"
                )
    summary = {
        "dry_run": not args.execute,
        "symbols": len(symbols),
        "with_splits": len(with_splits),
        "without_splits": len(none),
        "failed": failed,
        "splits": with_splits,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "splits"}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
