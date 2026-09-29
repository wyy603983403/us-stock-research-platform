"""Daily OHLCV + adjusted close from Yahoo Finance's public chart endpoint.

Unofficial endpoint: good enough for research prototyping, not a system of record. Every
study must cross-check critical windows against a second source before promotion.

Defaults to ``--dry-run`` semantics: nothing is written unless ``--execute`` is passed.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

from us_stock_research.bars import DailyBar, merge_bars
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (research; us-stock-research-platform)"


def parse_chart(payload: dict[str, Any]) -> list[DailyBar]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"Yahoo chart error: {chart['error']}")
    results = chart.get("result") or []
    if len(results) != 1:
        raise ValueError("Yahoo chart payload must contain exactly one result")
    result = results[0]
    offset = int(result.get("meta", {}).get("gmtoffset", 0))
    stamps: list[int] = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    if adj is None:
        raise ValueError("payload has no adjclose series; request includeAdjustedClose=true")
    bars: list[DailyBar] = []
    for i, ts in enumerate(stamps):
        values = (quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i], adj[i])
        if any(v is None for v in values):
            continue  # Yahoo emits null rows for halted/partial days; the quality gate flags gaps
        day = datetime.fromtimestamp(ts + offset, tz=UTC).date()
        bars.append(
            DailyBar(
                day=day,
                open=float(values[0]),
                high=float(values[1]),
                low=float(values[2]),
                close=float(values[3]),
                adj_close=float(values[4]),
                volume=int(quote["volume"][i] or 0),
            )
        )
    return bars


def parse_dividends(payload: dict[str, Any]) -> dict[date, float]:
    """Cash dividends per share keyed by ex-date (exchange-local)."""
    result = payload["chart"]["result"][0]
    offset = int(result.get("meta", {}).get("gmtoffset", 0))
    events = (result.get("events") or {}).get("dividends") or {}
    out: dict[date, float] = {}
    for item in events.values():
        day = datetime.fromtimestamp(int(item["date"]) + offset, tz=UTC).date()
        out[day] = out.get(day, 0.0) + float(item["amount"])
    return out


def fetch_symbol(
    client: httpx.Client, symbol: str, start: date, end: date
) -> tuple[list[DailyBar], dict[date, float]]:
    params: dict[str, str | int] = {
        "period1": int(datetime.combine(start, time.min, tzinfo=UTC).timestamp()),
        "period2": int(datetime.combine(end, time.max, tzinfo=UTC).timestamp()),
        "interval": "1d",
        "events": "div,split",
        "includeAdjustedClose": "true",
    }
    response = client.get(CHART_URL.format(symbol=symbol.upper()), params=params)
    response.raise_for_status()
    payload = response.json()
    return parse_chart(payload), parse_dividends(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="tickers, e.g. SPY QQQ IEF")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2005, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    store = open_store(settings)
    summary: list[dict[str, Any]] = []
    with httpx.Client(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        for symbol in args.symbols:
            bars, dividends = fetch_symbol(client, symbol, args.start, args.end)
            item: dict[str, Any] = {
                "symbol": symbol.upper(),
                "rows": len(bars),
                "dividends": len(dividends),
                "first": bars[0].day.isoformat() if bars else None,
                "last": bars[-1].day.isoformat() if bars else None,
                "path": store.location(symbol),
                "written": False,
            }
            if args.execute and bars:
                existing = store.read_bars(symbol) if store.has_bars(symbol) else []
                store.write_bars(symbol, merge_bars(existing, bars))
                old_divs = store.read_dividends(symbol) if store.has_dividends(symbol) else {}
                store.write_dividends(symbol, old_divs | dividends)
                item["written"] = True
            summary.append(item)
    print(json.dumps({"dry_run": not args.execute, "symbols": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
