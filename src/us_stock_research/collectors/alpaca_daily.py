"""Alpaca daily bars: backup source and cross-check for the server's few ETFs.

Used by ``usr-update --alpaca-backup`` (server pipeline only):

* Yahoo fails for a symbol (after its retries) -> the missing days come from Alpaca. Alpaca has
  raw prices only, so the new rows get ``adj_close = close``: exact unless a dividend went ex on
  those days, and the next successful Yahoo update sees the overlap differ and re-downloads the
  full history, which heals it.
* Yahoo succeeds -> the latest close is compared with Alpaca's; a gap above 0.5% is reported.

Keys: ``ALPACA_KEY_ID`` / ``ALPACA_SECRET_KEY`` (market data; a free account is enough). The SIP
feed is used (the run is hours after the close, inside the free tier's 15-minute delay); IEX if
SIP is refused.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from us_stock_research.bars import DailyBar

BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
CROSSCHECK_TOLERANCE = 0.005


def parse_daily(rows: list[dict[str, Any]]) -> list[DailyBar]:
    out = []
    for r in rows:
        c = float(r["c"])
        out.append(
            DailyBar(
                date.fromisoformat(str(r["t"])[:10]),
                float(r["o"]),
                float(r["h"]),
                float(r["l"]),
                c,
                c,
                int(r.get("v") or 0),
            )  # fmt: skip
        )
    return sorted(out, key=lambda b: b.day)


def fetch_daily(
    client: httpx.Client, symbol: str, start: date, end: date, key: str, secret: str
) -> list[DailyBar]:
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    last_error: Exception | None = None
    for feed in ("sip", "iex"):
        params: dict[str, str | int] = {
            "timeframe": "1Day",
            "start": f"{start.isoformat()}T00:00:00Z",
            "end": f"{end.isoformat()}T23:59:59Z",
            "adjustment": "raw",
            "feed": feed,
            "limit": 10000,
        }
        response = client.get(BARS_URL.format(symbol=symbol), params=params, headers=headers)
        if response.status_code in (401, 403, 422) and feed == "sip":
            last_error = httpx.HTTPStatusError(
                f"sip refused: {response.status_code}", request=response.request, response=response
            )
            continue
        response.raise_for_status()
        return parse_daily(response.json().get("bars") or [])
    raise last_error or RuntimeError("no feed answered")


def crosscheck(yahoo_close: float, alpaca_close: float) -> float:
    return alpaca_close / yahoo_close - 1
