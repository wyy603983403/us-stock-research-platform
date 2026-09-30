"""SEC bulk data sets: financial statements (2009-) and insider transactions (2006-).

Both include companies that have since been delisted or acquired, which is what a
survivorship-free fundamental study needs.

* **Financial Statement Data Sets** (``<YYYY>q<N>.zip``: ``sub.txt`` + ``num.txt``). Since SEC's
  December 2024 reprocessing they hold only the primary statements, so cover-page share counts are
  gone; use ``WeightedAverageNumberOfDilutedSharesOutstanding`` for market value. Only the
  consolidated (no segment, no co-registrant), standard-taxonomy values of ``TAGS`` are kept,
  joined with the filing facts (CIK, name, SIC industry, form, period, filing date). Stored as
  ``parquet/sec_fsds/<YYYY>q<N>.parquet``. ``filed`` is when the market could first know a
  number: point-in-time use must filter on it.
* **Insider Transactions Data Sets** (``<YYYY>q<N>_form345.zip``: SUBMISSION, REPORTINGOWNER,
  NONDERIV_TRANS). Non-derivative Form 4 trades with issuer CIK *and the ticker used at the
  time*, stored as ``parquet/sec_insider/<YYYY>q<N>.parquet``. The (ticker, CIK, filing date)
  pairs are also the most reliable historical ticker -> CIK map (see ``cik_map``).

Resumable per quarter; the zip is streamed and discarded. SEC asks for a contact in the
User-Agent (``SEC_USER_AGENT`` in .env) and at most 10 requests per second.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

FSDS_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
INSIDER_URLS = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/"
    "{q}_form345.zip",
)
FSDS_KIND, INSIDER_KIND = "sec_fsds", "sec_insider"
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A"}
TAGS = {
    # income statement (alternatives cover banks, insurers and companies without a COGS line)
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "RevenuesNetOfInterestExpense",
    "InterestAndDividendIncomeOperating",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "GrossProfit",
    "SellingGeneralAndAdministrativeExpense",
    "ResearchAndDevelopmentExpense",
    "OperatingIncomeLoss",
    "InterestExpense",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeTaxExpenseBenefit",
    "NetIncomeLoss",
    "ProfitLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "CommonStockDividendsPerShareDeclared",
    # balance sheet
    "Assets",
    "AssetsCurrent",
    "Liabilities",
    "LiabilitiesCurrent",
    "LiabilitiesAndStockholdersEquity",
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "RetainedEarningsAccumulatedDeficit",
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "AccountsReceivableNetCurrent",
    "InventoryNet",
    "PropertyPlantAndEquipmentNet",
    "Goodwill",
    "LongTermDebt",
    "LongTermDebtNoncurrent",
    "LongTermDebtCurrent",
    "DebtCurrent",
    "CommonStockSharesOutstanding",
    "CommonStockSharesIssued",
    "TreasuryStockShares",
    # cash flow
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "ShareBasedCompensation",
    "PaymentsOfDividends",
    "PaymentsOfDividendsCommonStock",
    "PaymentsForRepurchaseOfCommonStock",
    "ProceedsFromIssuanceOfCommonStock",
    # cover page (dei): dropped by SEC's Dec-2024 reprocessing of the data sets, kept in case
    "EntityCommonStockSharesOutstanding",
}
FSDS_SCHEMA = {
    "adsh": "VARCHAR",
    "cik": "BIGINT",
    "name": "VARCHAR",
    "sic": "INTEGER",
    "form": "VARCHAR",
    "period": "DATE",
    "fy": "INTEGER",
    "fp": "VARCHAR",
    "filed": "DATE",
    "tag": "VARCHAR",
    "ddate": "DATE",
    "qtrs": "INTEGER",
    "uom": "VARCHAR",
    "value": "DOUBLE",
}
INSIDER_SCHEMA = {
    "accession": "VARCHAR",
    "filing_date": "DATE",
    "issuer_cik": "BIGINT",
    "issuer_name": "VARCHAR",
    "issuer_symbol": "VARCHAR",
    "owner_cik": "BIGINT",
    "owner_name": "VARCHAR",
    "relationship": "VARCHAR",
    "officer_title": "VARCHAR",
    "trans_date": "DATE",
    "trans_code": "VARCHAR",
    "shares": "DOUBLE",
    "price": "DOUBLE",
    "acquired_disposed": "VARCHAR",
    "shares_after": "DOUBLE",
    "direct_indirect": "VARCHAR",
}
MONTHS = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}


def quarters(first: str, last: str) -> list[str]:
    y, q = int(first[:4]), int(first[5])
    ly, lq = int(last[:4]), int(last[5])
    out = []
    while (y, q) <= (ly, lq):
        out.append(f"{y}q{q}")
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    return out


def latest_quarter(today: date) -> str:
    """The last fully finished quarter (SEC publishes a quarter shortly after it ends)."""
    q = (today.month - 1) // 3  # quarters finished this year
    return f"{today.year}q{q}" if q else f"{today.year - 1}q4"


def parse_date(text: str | None) -> date | None:
    """YYYYMMDD, YYYY-MM-DD or DD-MON-YYYY (the insider data sets)."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        if len(t) == 8 and t.isdigit():
            return date(int(t[:4]), int(t[4:6]), int(t[6:]))
        if len(t) >= 10 and t[4] == "-":
            return date.fromisoformat(t[:10])
        day, mon, year = t.split("-")
        return date(int(year), MONTHS[mon.upper()[:3]], int(day))
    except (ValueError, KeyError):
        return None


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    member = next(n for n in archive.namelist() if n.lower().endswith(name.lower()))
    with archive.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def parse_fsds(content: bytes) -> list[list[Any]]:
    """Rows in FSDS_SCHEMA order: consolidated standard-taxonomy values of TAGS."""
    archive = zipfile.ZipFile(io.BytesIO(content))
    subs: dict[str, dict[str, Any]] = {}
    for r in _rows(archive, "sub.txt"):
        if r.get("form") in FORMS:
            subs[r["adsh"]] = {
                "cik": int(r["cik"]),
                "name": r.get("name", ""),
                "sic": int(r["sic"]) if (r.get("sic") or "").isdigit() else None,
                "form": r["form"],
                "period": parse_date(r.get("period")),
                "fy": int(r["fy"]) if (r.get("fy") or "").isdigit() else None,
                "fp": r.get("fp") or None,
                "filed": parse_date(r.get("filed")),
            }
    out: list[list[Any]] = []
    for r in _rows(archive, "num.txt"):
        sub = subs.get(r.get("adsh", ""))
        if sub is None or r.get("tag") not in TAGS:
            continue
        version = r.get("version", "")
        if not (version.startswith(("us-gaap", "dei", "ifrs"))):
            continue  # company-specific extension tag with a standard name
        if (r.get("coreg") or "").strip() or (r.get("segments") or "").strip():
            continue  # co-registrant or segment breakdown, not the consolidated figure
        value = _num(r.get("value"))
        if value is None:
            continue
        out.append(
            [
                r["adsh"],
                sub["cik"],
                sub["name"],
                sub["sic"],
                sub["form"],
                sub["period"],
                sub["fy"],
                sub["fp"],
                sub["filed"],
                r["tag"],
                parse_date(r.get("ddate")),
                int(r["qtrs"]) if (r.get("qtrs") or "").isdigit() else None,
                r.get("uom"),
                value,
            ]
        )
    return out


def parse_insider(content: bytes) -> list[list[Any]]:
    """Rows in INSIDER_SCHEMA order: one per non-derivative transaction."""
    archive = zipfile.ZipFile(io.BytesIO(content))
    subs = {r["ACCESSION_NUMBER"]: r for r in _rows(archive, "SUBMISSION.tsv")}
    owners: dict[str, dict[str, str]] = {}
    for r in _rows(archive, "REPORTINGOWNER.tsv"):
        owners.setdefault(r["ACCESSION_NUMBER"], r)  # first reporting owner
    out: list[list[Any]] = []
    for r in _rows(archive, "NONDERIV_TRANS.tsv"):
        acc = r.get("ACCESSION_NUMBER", "")
        sub = subs.get(acc)
        if sub is None:
            continue
        owner = owners.get(acc, {})
        cik = (sub.get("ISSUERCIK") or "").strip()
        owner_cik = (owner.get("RPTOWNERCIK") or "").strip()
        out.append(
            [
                acc,
                parse_date(sub.get("FILING_DATE")),
                int(cik) if cik.isdigit() else None,
                sub.get("ISSUERNAME"),
                (sub.get("ISSUERTRADINGSYMBOL") or "").strip().upper() or None,
                int(owner_cik) if owner_cik.isdigit() else None,
                owner.get("RPTOWNERNAME"),
                owner.get("RPTOWNER_RELATIONSHIP"),
                owner.get("RPTOWNER_TITLE"),
                parse_date(r.get("TRANS_DATE")),
                r.get("TRANS_CODE"),
                _num(r.get("TRANS_SHARES")),
                _num(r.get("TRANS_PRICEPERSHARE")),
                r.get("TRANS_ACQUIRED_DISP_CD"),
                _num(r.get("SHRS_OWND_FOLWNG_TRANS")),
                r.get("DIRECT_INDIRECT_OWNERSHIP"),
            ]
        )
    return out


def _download(
    client: httpx.Client, urls: list[str], attempts: int = 4, wait: float = 20.0
) -> bytes | None:
    """First URL that exists; dropped connections on large files are retried."""
    for url in urls:
        for attempt in range(attempts):
            try:
                response = client.get(url)
            except (httpx.TransportError, httpx.RemoteProtocolError):
                if attempt == attempts - 1:
                    raise
                time.sleep(wait * (attempt + 1))
                continue
            if response.status_code == 404:
                break
            response.raise_for_status()
            return response.content
    return None


def collect(
    kind: str,
    urls: tuple[str, ...],
    schema: dict[str, str],
    parse: Any,
    qs: list[str],
    store: TableStore,
    client: httpx.Client,
    *,
    execute: bool,
    pause: float,
    refresh: bool,
) -> dict[str, Any]:
    done: dict[str, int] = {}
    missing: list[str] = []
    failed: dict[str, str] = {}
    skipped = 0
    for i, q in enumerate(qs, 1):
        print(f"[{i}/{len(qs)}] {kind} {q}", file=sys.stderr, flush=True)
        if store.has(kind, q) and not refresh:
            skipped += 1
            continue
        try:
            content = _download(client, [u.format(q=q) for u in urls])
            if content is None:
                missing.append(q)
                continue
            rows = parse(content)
        except (httpx.HTTPError, zipfile.BadZipFile, KeyError, StopIteration) as exc:
            failed[q] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        finally:
            time.sleep(pause)
        if execute:
            columns = [[r[k] for r in rows] for k in range(len(schema))]
            store.write(kind, q, schema, columns, next(iter(schema)))
        done[q] = len(rows)
    return {"done": done, "skipped_existing": skipped, "not_published": missing, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--what", choices=("fsds", "insider", "both"), default="both")
    parser.add_argument("--first", help="first quarter, e.g. 2009q1 (default per data set)")
    parser.add_argument("--last", default=latest_quarter(datetime.now(UTC).date()))
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--refresh", action="store_true", help="re-download stored quarters")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        print(
            'put SEC_USER_AGENT="Your Name your@email" in .env (SEC requires it)', file=sys.stderr
        )
        return 2
    store = TableStore.from_settings(settings)
    summary: dict[str, Any] = {"dry_run": not args.execute, "last": args.last}
    with httpx.Client(timeout=300, headers={"User-Agent": agent}, follow_redirects=True) as client:
        if args.what in ("fsds", "both"):
            qs = quarters(args.first or "2009q1", args.last)
            summary["fsds"] = collect(
                FSDS_KIND,
                (FSDS_URL,),
                FSDS_SCHEMA,
                parse_fsds,
                qs,
                store,
                client,
                execute=args.execute,
                pause=args.pause,
                refresh=args.refresh,
            )
        if args.what in ("insider", "both"):
            qs = quarters(args.first or "2006q1", args.last)
            summary["insider"] = collect(
                INSIDER_KIND,
                INSIDER_URLS,
                INSIDER_SCHEMA,
                parse_insider,
                qs,
                store,
                client,
                execute=args.execute,
                pause=args.pause,
                refresh=args.refresh,
            )
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    brief = {
        k: (
            {kk: (len(vv) if isinstance(vv, dict | list) else vv) for kk, vv in v.items()}
            if isinstance(v, dict)
            else v
        )
        for k, v in summary.items()
    }
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    failed = any(summary.get(k, {}).get("failed") for k in ("fsds", "insider"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
