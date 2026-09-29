"""Tiingo IEX intraday bars (1-minute and coarser). Probe tool first, downloader later.

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
from datetime import date, datetime
from typing import Any

import httpx

from us_stock_research.config import load_settings

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
