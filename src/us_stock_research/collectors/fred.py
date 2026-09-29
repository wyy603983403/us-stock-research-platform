"""Macro series from FRED's keyless CSV endpoint (rates, spreads, inflation, activity).

``configs/macro_series.yml`` lists the series. Each is stored as ``parquet/macro/<ID>.parquet``
with columns ``date, value``. Missing observations (``.``) are dropped. Series are small, so an
update simply re-downloads the whole thing (FRED revises history for some series, e.g. CPI).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import yaml

from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def parse_fredgraph(text: str) -> tuple[list[date], list[float]]:
    rows = list(csv.reader(io.StringIO(text.strip())))
    if len(rows) < 2 or len(rows[0]) < 2:
        raise ValueError(f"not a FRED CSV: {text[:80]!r}")
    days: list[date] = []
    values: list[float] = []
    for row in rows[1:]:
        try:
            day, value = date.fromisoformat(row[0]), float(row[1])
        except ValueError:
            continue  # "." marks a missing observation
        days.append(day)
        values.append(value)
    if not days:
        raise ValueError("series has no observations")
    return days, values


def load_series(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text())["series"]
    return {str(k): str(v) for k, v in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/macro_series.yml"))
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    series = load_series(args.config)
    store = TableStore.from_settings(load_settings())
    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for sid in series:
            try:
                response = client.get(FRED_URL, params={"id": sid})
                response.raise_for_status()
                days, values = parse_fredgraph(response.text)
                if args.execute:
                    store.write(
                        "macro", sid, {"date": "DATE", "value": "DOUBLE"}, [days, values], "date"
                    )
                done[sid] = {"rows": len(days), "first": str(days[0]), "last": str(days[-1])}
            except (httpx.HTTPError, ValueError) as exc:
                failed[sid] = f"{type(exc).__name__}: {exc}"[:200]
            time.sleep(args.pause)
    summary = {"dry_run": not args.execute, "series": done, "failed": failed}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
