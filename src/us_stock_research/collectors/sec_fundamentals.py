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


FACTS_KIND = "sec_companyfacts"  # one file per SEC filer (key CIK0000320193), all S&P filers
# Annual-report items of the quality+value study, incl. the cover-page share count (dei) and the
# notes (company facts, unlike the financial statement data sets, are not limited to the face of
# the statements).
WIDE_TAGS = {
    "us-gaap": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "Assets",
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "CommonStockSharesOutstanding",
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "GrossProfit",
        "OperatingIncomeLoss",
        "Liabilities",
        "LongTermDebt",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "EarningsPerShareDiluted",
    ),
    "dei": ("EntityCommonStockSharesOutstanding",),
}
WIDE_SCHEMA = {"taxonomy": "VARCHAR", **SCHEMA}


def parse_wide(payload: dict[str, Any]) -> list[list[Any]]:
    """Rows in ``WIDE_SCHEMA`` order for every ``WIDE_TAGS`` fact, sorted deterministically."""
    out: list[list[Any]] = []
    facts = payload.get("facts") or {}
    for taxonomy, tags in WIDE_TAGS.items():
        for tag in tags:
            for unit, items in (
                ((facts.get(taxonomy) or {}).get(tag) or {}).get("units") or {}
            ).items():
                for item in items:
                    out.append(
                        [
                            taxonomy,
                            tag,
                            unit,
                            date.fromisoformat(item["start"]) if item.get("start") else None,
                            date.fromisoformat(item["end"]),
                            float(item["val"]),
                            date.fromisoformat(item["filed"]),
                            item.get("form"),
                            item.get("fy"),
                            item.get("fp"),
                            item.get("accn"),
                        ]
                    )
    out.sort(key=lambda r: (r[0], r[1], r[2], r[4], r[6], r[10] or "", r[5]))
    return out


def facts_key(cik: int) -> str:
    return f"CIK{cik:010d}"


def collect_all_ciks(
    store: TableStore,
    client: httpx.Client,
    *,
    execute: bool,
    refresh: bool,
    pause: float,
    log: Any = print,
) -> dict[str, Any]:
    """Company facts of every filer in ``meta/ticker_cik`` (current and former S&P members)."""
    ciks = sorted({int(c) for (c,) in store.read("meta", "ticker_cik", "cik") if c is not None})
    done: dict[str, int] = {}
    skipped = 0
    failed: dict[str, str] = {}
    for i, cik in enumerate(ciks, 1):
        key = facts_key(cik)
        if store.has(FACTS_KIND, key) and not refresh:
            skipped += 1
            continue
        log(f"[{i}/{len(ciks)}] {key}")
        for attempt in range(3):
            try:
                response = client.get(FACTS_URL.format(cik=cik))
                if response.status_code == 404:
                    failed[key] = "404 (no XBRL facts)"
                    break
                response.raise_for_status()
                rows = parse_wide(response.json())
                if execute and rows:
                    store.write(
                        FACTS_KIND,
                        key,
                        WIDE_SCHEMA,
                        [list(c) for c in zip(*rows, strict=True)],
                        "taxonomy, tag, period_end, filed",
                    )
                done[key] = len(rows)
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == 2:
                    failed[key] = f"{type(exc).__name__}: {exc}"[:200]
                time.sleep(5 * (attempt + 1))
            except (ValueError, KeyError) as exc:
                failed[key] = f"{type(exc).__name__}: {exc}"[:200]
                break
        time.sleep(pause)
    return {
        "ciks": len(ciks),
        "downloaded": len(done),
        "rows": sum(done.values()),
        "skipped_existing": skipped,
        "failed": failed,
    }


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
    parser.add_argument(
        "--all-ciks",
        action="store_true",
        help=f"every filer in meta/ticker_cik into {FACTS_KIND} (former members included)",
    )
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
    store = TableStore.from_settings(settings)
    if args.all_ciks:
        headers = {"User-Agent": agent}
        with httpx.Client(timeout=120, headers=headers, follow_redirects=True) as client:
            result = collect_all_ciks(
                store,
                client,
                execute=args.execute,
                refresh=args.refresh,
                pause=args.pause,
                log=lambda m: print(m, file=sys.stderr, flush=True),
            )
        result["dry_run"] = not args.execute
        text = json.dumps(result, indent=2, ensure_ascii=False)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text + "\n")
        print(text)
        return 1 if result["failed"] else 0
    symbols = [s.upper() for s in args.symbols] or load_universe(args.universe)
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
