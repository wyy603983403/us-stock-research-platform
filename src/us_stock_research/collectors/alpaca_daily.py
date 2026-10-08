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


MULTI_URL = "https://data.alpaca.markets/v2/stocks/bars"


def alpaca_keys() -> tuple[str, str] | None:
    """Market-data keys from the environment (.env); the paper keys work for data too."""
    import os

    for k, s in (("ALPACA_KEY_ID", "ALPACA_SECRET_KEY"),
                 ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY")):  # fmt: skip
        key, secret = os.environ.get(k), os.environ.get(s)
        if key and secret:
            return key, secret
    return None


def fetch_many(
    client: httpx.Client,
    symbols: list[str],
    start: date,
    end: date,
    key: str,
    secret: str,
    *,
    adjustment: str = "all",
    batch: int = 100,
) -> dict[str, list[DailyBar]]:
    """Daily bars for many symbols (multi-symbol endpoint, paged).

    ``adjustment="all"`` returns split- and dividend-adjusted prices (used for screening returns);
    ``"raw"`` returns traded prices (used to value a ledger). Symbols use Alpaca notation
    (``BRK.B``); the result is keyed by the symbols as given.
    """
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    out: dict[str, list[DailyBar]] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        to_alpaca = {s.replace("-", "."): s for s in chunk}
        rows: dict[str, list[dict[str, Any]]] = {}
        for feed in ("sip", "iex"):
            rows, token, refused = {}, None, False
            while True:
                params: dict[str, str | int] = {
                    "symbols": ",".join(to_alpaca),
                    "timeframe": "1Day",
                    "start": f"{start.isoformat()}T00:00:00Z",
                    "end": f"{end.isoformat()}T23:59:59Z",
                    "adjustment": adjustment,
                    "feed": feed,
                    "limit": 10000,
                }
                if token:
                    params["page_token"] = token
                response = client.get(MULTI_URL, params=params, headers=headers)
                if response.status_code in (401, 403, 422) and feed == "sip":
                    refused = True
                    break
                response.raise_for_status()
                body = response.json()
                for sym, bars in (body.get("bars") or {}).items():
                    rows.setdefault(sym, []).extend(bars)
                token = body.get("next_page_token")
                if not token:
                    break
            if not refused:
                break
        for sym, bars in rows.items():
            out[to_alpaca.get(sym, sym)] = parse_daily(bars)
    return out
