"""Batch-download a named universe of symbols into the configured store (CSV or Parquet).

Resumable: symbols already in the store are skipped unless ``--refresh``. Requests are
throttled, retried with backoff, and failures are reported instead of aborting the run.
Dry run by default; nothing is written without ``--execute``.

Universes live in ``configs/universes/<name>.yml``. ``sp500`` is special: today's constituents are
fetched from a public CSV. Current members only -> survivorship bias; never use that list to
estimate historical stock-picking returns.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from us_stock_research.bars import DailyBar, merge_bars
from us_stock_research.collectors.yahoo_daily import USER_AGENT, fetch_symbol
from us_stock_research.config import load_settings
from us_stock_research.storage import BarStore, open_store

SP500_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
FetchFn = Callable[[str, date, date], tuple[list[DailyBar], dict[date, float]]]


def to_yahoo(symbol: str) -> str:
    """Class shares use a dash on Yahoo: BRK.B -> BRK-B."""
    return symbol.strip().upper().replace(".", "-")


def load_universe(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text())
    symbols = [to_yahoo(s) if not s.startswith(("^",)) else s for s in raw["symbols"]]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"duplicate symbols in {path}")
    return symbols


def parse_sp500_csv(text: str) -> list[str]:
    rows = csv.DictReader(io.StringIO(text))
    return sorted({to_yahoo(r["Symbol"]) for r in rows if r.get("Symbol")})


def resolve(names: list[str], universes_dir: Path, client: httpx.Client | None) -> list[str]:
    out: list[str] = []
    for name in names:
        if name == "sp500":
            if client is None:
                raise RuntimeError("sp500 needs a network client")
            response = client.get(SP500_URL)
            response.raise_for_status()
            out.extend(parse_sp500_csv(response.text))
        else:
            out.extend(load_universe(universes_dir / f"{name}.yml"))
    return list(dict.fromkeys(out))


def download(
    symbols: list[str],
    store: BarStore,
    fetch: FetchFn,
    start: date,
    end: date,
    *,
    execute: bool,
    refresh: bool,
    pause: float,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    done: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    for symbol in symbols:
        if store.has_bars(symbol) and not refresh:
            skipped.append(symbol)
            continue
        error = ""
        for attempt in range(retries):
            try:
                bars, dividends = fetch(symbol, start, end)
                error = "" if bars else "no rows returned"
                break
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
                sleep(pause * (2 ** (attempt + 2)))
        if error:
            failed[symbol] = error
            continue
        if execute:
            existing = store.read_bars(symbol) if store.has_bars(symbol) else []
            store.write_bars(symbol, merge_bars(existing, bars))
            old = store.read_dividends(symbol) if store.has_dividends(symbol) else {}
            store.write_dividends(symbol, old | dividends)
        done.append(symbol)
        sleep(pause)
    return {"downloaded": done, "skipped_existing": skipped, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "universes", nargs="+", help="names from configs/universes (e.g. etf_core) or sp500"
    )
    parser.add_argument("--universes-dir", type=Path, default=Path("configs/universes"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2000, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--pause", type=float, default=0.5, help="seconds between requests")
    parser.add_argument("--refresh", action="store_true", help="re-download existing symbols")
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    parser.add_argument("--report", type=Path, help="write the JSON summary here too")
    args = parser.parse_args(argv)
    settings = load_settings()
    store = open_store(settings)
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        symbols = resolve(args.universes, args.universes_dir, client)
        summary = download(
            symbols,
            store,
            lambda s, a, b: fetch_symbol(client, s, a, b),
            args.start,
            args.end,
            execute=args.execute,
            refresh=args.refresh,
            pause=args.pause,
        )
    summary = {"dry_run": not args.execute, "requested": len(symbols), **summary}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
