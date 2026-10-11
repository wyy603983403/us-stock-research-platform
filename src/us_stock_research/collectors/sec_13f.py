"""SEC Form 13F holdings and the CUSIP -> ticker map they need (``usr-collect-13f``).

* **Fails-to-deliver** (``cnsfailsYYYYMM[a|b].zip``, half-monthly, SEC):
  https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data. Each row carries
  settlement date, CUSIP and the ticker used that day. ``meta/ftd_cusip`` keeps, for every
  (CUSIP, ticker) pair, the last settlement date it appeared in each calendar month
  (``cusip, symbol, seen``). New files are merged in with the same rule, so re-processing a file
  changes nothing; each run re-reads the files from the month before the newest stored date on.
* **Form 13F data sets** (quarterly zips, SEC):
  https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets. Kept: original 13F-HR
  filings, ``SSHPRNAMTTYPE = SH`` without PUT/CALL, CUSIPs that ever traded under an S&P 500
  member's ticker (``meta/ftd_cusip`` symbol in ``meta/sp500_history``), summed per (accession,
  CUSIP). Stored as ``sec13f/<zip name without _form13f>`` with ``accession, cik, form,
  filing_date, period, cusip, value`` (VALUE as filed: thousands of dollars before 2023,
  dollars after). Quarters already stored are skipped.

Run FTD first (``--what both`` does), so new CUSIPs reach the 13F filter. SEC asks for a contact
in the User-Agent (``SEC_USER_AGENT`` in .env) and at most 10 requests per second.

    usr-collect-13f --what both --execute
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from typing import Any

import httpx

from us_stock_research.collectors.sec_bulk import _download, parse_date
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

SEC = "https://www.sec.gov"
F13_PAGE = SEC + "/data-research/sec-markets-data/form-13f-data-sets"
FTD_PAGE = SEC + "/data-research/sec-markets-data/fails-deliver-data"
F13_KIND = "sec13f"
F13_SCHEMA = {
    "accession": "VARCHAR",
    "cik": "BIGINT",
    "form": "VARCHAR",
    "filing_date": "DATE",
    "period": "DATE",
    "cusip": "VARCHAR",
    "value": "DOUBLE",
}
FTD_SCHEMA = {"cusip": "VARCHAR", "symbol": "VARCHAR", "seen": "DATE"}
FTD_START = "201206"  # 13F data start in 2013Q2; the map looks back 365 days


def links(html: str, pattern: str) -> list[str]:
    """Unique absolute URLs of hrefs matching ``pattern``, in page order."""
    out: list[str] = []
    for href in re.findall(r'href="([^"]+)"', html):
        if re.search(pattern, href):
            url = href if href.startswith("http") else SEC + href
            if url not in out:
                out.append(url)
    return out


def f13_key(url: str) -> str:
    return url.rsplit("/", 1)[-1].removesuffix(".zip").removesuffix("_form13f")


def ftd_period(url: str) -> str:
    """``cnsfails202608b.zip`` -> ``202608b``."""
    m = re.search(r"cnsfails?(\d{6}[ab]?)", url)
    return m.group(1) if m else ""


def parse_ftd(content: bytes) -> list[tuple[str, str, date]]:
    """(cusip, symbol, last settlement date of the pair in each month) of one file."""
    archive = zipfile.ZipFile(io.BytesIO(content))
    text = archive.read(archive.namelist()[0]).decode("latin-1")
    best: dict[tuple[str, str, int, int], date] = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 3:
            continue
        d_txt, cusip, symbol = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if len(d_txt) != 8 or not d_txt.isdigit() or not cusip or not symbol:
            continue
        d = date(int(d_txt[:4]), int(d_txt[4:6]), int(d_txt[6:]))
        key = (cusip, symbol, d.year, d.month)
        if key not in best or d > best[key]:
            best[key] = d
    return [(c, s, d) for (c, s, _, _), d in best.items()]


def merge_ftd(
    old: list[tuple[Any, ...]], new: list[tuple[str, str, date]]
) -> list[tuple[Any, ...]]:
    best: dict[tuple[str, str, int, int], date] = {}
    for c, s, d in [*old, *new]:
        key = (str(c), str(s), d.year, d.month)
        if key not in best or d > best[key]:
            best[key] = d
    return sorted((c, s, d) for (c, s, _, _), d in best.items())


def candidates(tables: TableStore) -> set[str]:
    members = {str(s) for (s,) in tables.read("meta", "sp500_history", "DISTINCT symbol")}
    return {str(c) for c, s in tables.read("meta", "ftd_cusip", "cusip, symbol") if s in members}


def parse_13f(content: bytes, wanted: set[str]) -> list[tuple[Any, ...]]:
    """Rows in F13_SCHEMA order (DuckDB reads the two tab-separated tables of the zip)."""
    import tempfile

    import duckdb

    archive = zipfile.ZipFile(io.BytesIO(content))
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name in ("SUBMISSION.tsv", "INFOTABLE.tsv"):
            member = next(n for n in archive.namelist() if n.upper().endswith(name.upper()))
            target = os.path.join(tmp, name)
            with archive.open(member) as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            paths[name] = target.replace("'", "''")
        con = duckdb.connect()
        try:
            con.execute("CREATE TABLE w (cusip VARCHAR)")
            con.executemany("INSERT INTO w VALUES (?)", [[c] for c in sorted(wanted)])
            opts = "delim='\t', header=true, all_varchar=true, quote='', escape=''"
            raw = con.execute(
                f"""
                WITH s AS (
                  SELECT trim(ACCESSION_NUMBER) acc, CAST(CIK AS BIGINT) cik,
                         FILING_DATE fd, PERIODOFREPORT pr
                  FROM read_csv('{paths["SUBMISSION.tsv"]}', {opts})
                  WHERE trim(SUBMISSIONTYPE) = '13F-HR'
                ), i AS (
                  SELECT trim(ACCESSION_NUMBER) acc, upper(trim(CUSIP)) cusip,
                         TRY_CAST(VALUE AS DOUBLE) v
                  FROM read_csv('{paths["INFOTABLE.tsv"]}', {opts})
                  WHERE trim(SSHPRNAMTTYPE) = 'SH' AND coalesce(trim(PUTCALL), '') = ''
                )
                SELECT i.acc, s.cik, s.fd, s.pr, i.cusip, sum(i.v)
                FROM i JOIN s USING (acc) JOIN w USING (cusip)
                WHERE i.v IS NOT NULL
                GROUP BY i.acc, s.cik, s.fd, s.pr, i.cusip
                """
            ).fetchall()
        finally:
            con.close()
    return [
        (acc, int(cik), "13F-HR", parse_date(fd), parse_date(pr), cusip, float(v))
        for acc, cik, fd, pr, cusip, v in raw
    ]


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912 - one small CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--what", choices=("ftd", "13f", "both"), default="both")
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        print('put SEC_USER_AGENT="Name email" in .env (SEC requires a contact)', file=sys.stderr)
        return 2
    tables = TableStore.from_settings(settings)
    report: dict[str, Any] = {"dry_run": not args.execute, "failed": {}}
    with httpx.Client(timeout=300, headers={"User-Agent": agent}, follow_redirects=True) as client:
        if args.what in ("ftd", "both"):
            urls = links(client.get(FTD_PAGE).text, r"cnsfails?\d{6}[ab]?\.zip$")
            old = (
                tables.read("meta", "ftd_cusip", "cusip, symbol, seen")
                if tables.has("meta", "ftd_cusip")
                else []
            )
            last = max((r[2] for r in old), default=None)
            start = (last - timedelta(days=45)).strftime("%Y%m") if last else FTD_START
            todo = sorted((u for u in urls if ftd_period(u)[:6] >= start), key=ftd_period)
            new: list[tuple[str, str, date]] = []
            for url in todo:
                try:
                    content = _download(client, [url])
                    if content:
                        new += parse_ftd(content)
                except (httpx.HTTPError, zipfile.BadZipFile) as exc:
                    report["failed"][ftd_period(url)] = f"{type(exc).__name__}: {exc}"[:200]
                time.sleep(args.pause)
            merged = merge_ftd(old, new)
            if args.execute and new:
                cols = [[r[i] for r in merged] for i in range(3)]
                tables.write("meta", "ftd_cusip", FTD_SCHEMA, cols, "cusip, symbol, seen")
            report["ftd"] = {"files": [ftd_period(u) for u in todo], "rows_before": len(old),
                             "rows_after": len(merged),
                             "newest": str(max((r[2] for r in merged), default=""))}  # fmt: skip
        if args.what in ("13f", "both"):
            urls = links(client.get(F13_PAGE).text, r"_form13f\.zip$")
            have = set(tables.keys(F13_KIND))
            todo = [u for u in urls if f13_key(u) not in have]
            wanted = candidates(tables)
            done = {}
            for url in todo:
                try:
                    content = _download(client, [url])
                    rows = parse_13f(content, wanted) if content else []
                except (httpx.HTTPError, zipfile.BadZipFile, KeyError) as exc:
                    report["failed"][f13_key(url)] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                if args.execute and rows:
                    cols = [[r[i] for r in rows] for i in range(len(F13_SCHEMA))]
                    tables.write(F13_KIND, f13_key(url), F13_SCHEMA, cols, "accession, cusip")
                done[f13_key(url)] = len(rows)
                time.sleep(args.pause)
            report["13f"] = {"listed": len(urls), "stored_before": len(have), "new": done,
                             "candidate_cusips": len(wanted)}  # fmt: skip
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
