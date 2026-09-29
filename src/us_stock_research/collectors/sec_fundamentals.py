"""Company fundamentals from SEC EDGAR "company facts" (XBRL), stored point-in-time.

Every fact keeps the date it was *filed*, so a study can ask "what did the market know on day
D?" (``point_in_time``) and avoid look-ahead bias from later restatements. Free, keyless, but the
SEC requires a contact in the User-Agent: set ``SEC_USER_AGENT="Your Name your@email"`` in
``.env`` (your own choice; it is only sent to sec.gov). Limit: 10 requests per second.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.collectors.universe import load_universe
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# Core statements: revenue, profit, balance sheet, cash flow, per-share and share count.
TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "GrossProfit",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebt",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "CommonStockSharesOutstanding",
)
SCHEMA = {
    "tag": "VARCHAR",
    "unit": "VARCHAR",
    "period_start": "DATE",
    "period_end": "DATE",
    "value": "DOUBLE",
    "filed": "DATE",
    "form": "VARCHAR",
    "fy": "INTEGER",
    "fp": "VARCHAR",
    "accn": "VARCHAR",
}


def parse_companyfacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    facts = (payload.get("facts") or {}).get("us-gaap") or {}
    for tag in TAGS:
        for unit, items in ((facts.get(tag) or {}).get("units") or {}).items():
            for item in items:
                rows.append(
                    {
                        "tag": tag,
                        "unit": unit,
                        "period_start": date.fromisoformat(item["start"])
                        if item.get("start")
                        else None,
                        "period_end": date.fromisoformat(item["end"]),
                        "value": float(item["val"]),
                        "filed": date.fromisoformat(item["filed"]),
                        "form": item.get("form"),
                        "fy": item.get("fy"),
                        "fp": item.get("fp"),
                        "accn": item.get("accn"),
                    }
                )
    return rows


def point_in_time(rows: list[dict[str, Any]], tag: str, as_of: date) -> dict[str, Any] | None:
    """Latest reported period known on ``as_of`` (using the newest filing about it)."""
    known = [r for r in rows if r["tag"] == tag and r["filed"] <= as_of]
    if not known:
        return None
    latest_period = max(r["period_end"] for r in known)
    return max((r for r in known if r["period_end"] == latest_period), key=lambda r: r["filed"])


def ticker_map(payload: dict[str, Any]) -> dict[str, int]:
    return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in payload.values()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="tickers; default: the mega_caps universe")
    parser.add_argument("--universe", type=Path, default=Path("configs/universes/mega_caps.yml"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()  # also loads .env into the environment
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        print(
            'SEC requires a contact in the User-Agent: put SEC_USER_AGENT="Name email" in .env',
            file=sys.stderr,
        )
        return 2
    symbols = [s.upper() for s in args.symbols] or load_universe(args.universe)
    store = TableStore.from_settings(settings)
    done: dict[str, int] = {}
    skipped: list[str] = []
    failed: dict[str, str] = {}
    with httpx.Client(timeout=60, headers={"User-Agent": agent}, follow_redirects=True) as client:
        response = client.get(TICKERS_URL)
        response.raise_for_status()
        ciks = ticker_map(response.json())
        for symbol in symbols:
            if store.has("fundamentals", symbol) and not args.refresh:
                skipped.append(symbol)
                continue
            cik = ciks.get(symbol) or ciks.get(symbol.replace("-", "."))
            if cik is None:
                failed[symbol] = "no CIK (ETF, index or unknown ticker)"
                continue
            try:
                facts = client.get(FACTS_URL.format(cik=cik))
                facts.raise_for_status()
                rows = parse_companyfacts(facts.json())
                if args.execute and rows:
                    store.write(
                        "fundamentals",
                        symbol,
                        SCHEMA,
                        [[r[name] for r in rows] for name in SCHEMA],
                        "tag, period_end, filed",
                    )
                done[symbol] = len(rows)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                failed[symbol] = f"{type(exc).__name__}: {exc}"[:200]
            time.sleep(args.pause)
    summary = {
        "dry_run": not args.execute,
        "downloaded": done,
        "skipped": skipped,
        "failed": failed,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
