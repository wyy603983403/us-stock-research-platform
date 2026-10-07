"""Fama-French factor returns from Kenneth French's data library (free zipped CSVs).

Used to attribute a strategy's return: if its alpha disappears once market, size, value,
profitability, investment and momentum exposures are removed, the "edge" was a known premium.
Stored as ``parquet/factors/<name>.parquet``; returns are decimals (the files use percent).
Monthly tables are dated on the first day of the month.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
DATASETS = {
    "ff5_daily": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    "mom_daily": "F-F_Momentum_Factor_daily_CSV.zip",
    "ff5_monthly": "F-F_Research_Data_5_Factors_2x3_CSV.zip",
    "mom_monthly": "F-F_Momentum_Factor_CSV.zip",
    # research/factor-sleeve: value-weighted size x momentum and size x profitability portfolios
    "me_prior_daily": "6_Portfolios_ME_Prior_12_2_Daily_CSV.zip",
    "me_op_daily": "6_Portfolios_ME_OP_2x3_daily_CSV.zip",
}
KIND = "factors"


def _column(name: str) -> str:
    return "_".join(name.strip().lower().replace("-", "_").split()) or "unnamed"


def parse_french_csv(text: str) -> tuple[list[str], list[date], list[list[float]]]:
    """First table of a French CSV: header row, then YYYYMMDD / YYYYMM rows until a break.

    Monthly files append an annual table (4-digit years) after a blank line; it is ignored.
    Missing values (-99.99 / -999) become NaN.
    """
    header: list[str] | None = None
    days: list[date] = []
    values: list[list[float]] = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        first = cells[0]
        if header is None:
            if len(cells) > 1 and first == "" and any(cells[1:]):
                header = [_column(c) for c in cells[1:]]
            continue
        if not first.isdigit() or len(first) not in (6, 8):
            if days:
                break
            continue
        if len(first) == 8:
            day = date(int(first[:4]), int(first[4:6]), int(first[6:]))
        else:
            day = date(int(first[:4]), int(first[4:]), 1)
        row = [float(c) for c in cells[1 : 1 + len(header)]]
        days.append(day)
        values.append([v / 100 if v > -99 else float("nan") for v in row])
    if header is None or not days:
        raise ValueError("no factor table found")
    return header, days, values


def read_zip(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = archive.namelist()[0]
        return archive.read(name).decode("latin-1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    store = TableStore.from_settings(load_settings())
    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for name, filename in DATASETS.items():
            try:
                response = client.get(BASE + filename)
                response.raise_for_status()
                header, days, values = parse_french_csv(read_zip(response.content))
            except (httpx.HTTPError, ValueError, zipfile.BadZipFile) as exc:
                failed[name] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            if args.execute:
                schema = {"date": "DATE", **{c: "DOUBLE" for c in header}}
                columns: list[list[Any]] = [days] + [
                    [r[i] for r in values] for i in range(len(header))
                ]
                store.write(KIND, name, schema, columns, "date")
            done[name] = {
                "columns": header,
                "rows": len(days),
                "first": days[0].isoformat(),
                "last": days[-1].isoformat(),
            }
    summary = {"dry_run": not args.execute, "datasets": done, "failed": failed}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
