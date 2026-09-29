"""Historical S&P 500 membership (1996-) and daily prices for former members.

Membership comes from github.com/fja05680/sp500 ("S&P 500 Historical Components & Changes
(Updated).csv": one row per change date with the full ticker list). It is stored as intervals in
``parquet/meta/sp500_history.parquet`` (``symbol, start, end``; ``end`` is the first date the
ticker was no longer listed, null while it is a member). Tickers are converted to Yahoo notation.

Former members are mostly delisted, so Yahoo has no history for them. ``--delisted`` tries Yahoo
(for those still trading) and then Tiingo's daily endpoint, storing into
``parquet/daily_delisted/<SYMBOL>.parquet``, and accepts a series only if it covers the membership
window (a re-used ticker that now belongs to another
company fails that test and is reported, not stored). Survivorship bias is reduced, not removed:
whatever Tiingo lacks stays missing and is listed in the coverage table.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.calendar import trading_days
from us_stock_research.collectors.tiingo_intraday import RateLimited
from us_stock_research.collectors.universe import to_yahoo
from us_stock_research.collectors.yahoo_daily import USER_AGENT, fetch_symbol
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store
from us_stock_research.tables import TableStore

HISTORY_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
TIINGO_DAILY = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
DELISTED_KIND = "daily_delisted"
DELISTED_SCHEMA = {
    "date": "DATE",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "adj_close": "DOUBLE",
    "volume": "DOUBLE",
    "div_cash": "DOUBLE",
    "split_factor": "DOUBLE",
}
HISTORY_SCHEMA = {"symbol": "VARCHAR", "start": "DATE", "end": "DATE"}
COVERAGE_SCHEMA = {
    "symbol": "VARCHAR",
    "start": "DATE",
    "end": "DATE",
    "source": "VARCHAR",
    "coverage": "DOUBLE",
    "status": "VARCHAR",
}
MIN_COVERAGE = 0.9  # share of membership trading days a series must have to be accepted

Interval = tuple[str, date, date | None]


def parse_history(text: str) -> list[tuple[date, set[str]]]:
    rows: list[tuple[date, set[str]]] = []
    for row in csv.DictReader(io.StringIO(text)):
        tickers = {to_yahoo(t) for t in row["tickers"].split(",") if t.strip()}
        rows.append((date.fromisoformat(row["date"]), tickers))
    if not rows:
        raise ValueError("empty membership file")
    return sorted(rows, key=lambda r: r[0])


def intervals(rows: list[tuple[date, set[str]]]) -> list[Interval]:
    """Membership runs; a ticker that leaves and rejoins gets two intervals."""
    out: list[Interval] = []
    open_since: dict[str, date] = {}
    for day, members in rows:
        for symbol in sorted(members - open_since.keys()):
            open_since[symbol] = day
        for symbol in sorted(open_since.keys() - members):
            out.append((symbol, open_since.pop(symbol), day))
    out += [(s, start, None) for s, start in sorted(open_since.items())]
    return sorted(out, key=lambda x: (x[0], x[1]))


def members_on(history: list[Interval], day: date) -> set[str]:
    return {s for s, a, b in history if a <= day and (b is None or day < b)}


def parse_tiingo_daily(rows: list[dict[str, Any]]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for r in rows:
        out.append(
            [
                date.fromisoformat(r["date"][:10]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["adjClose"]),
                float(r.get("volume") or 0),
                float(r.get("divCash") or 0),
                float(r.get("splitFactor") or 1),
            ]
        )
    return out


def window_coverage(days: set[date], start: date, end: date | None, today: date) -> float:
    expected = trading_days(start, (end - timedelta(days=1)) if end else today)
    if not expected:
        return 1.0
    return sum(1 for d in expected if d in days) / len(expected)


def fetch_tiingo_daily(client: httpx.Client, symbol: str, token: str) -> list[list[Any]]:
    response = client.get(
        TIINGO_DAILY.format(symbol=symbol.lower()),
        params={"startDate": "1996-01-01", "format": "json", "token": token},
    )
    if response.status_code == 429 or (
        response.status_code == 400
        and ("allocation" in response.text.lower() or "limit" in response.text.lower())
    ):
        raise RateLimited(response.text[:200].replace(token, "***"))
    if response.status_code == 404:
        return []  # Tiingo does not carry this ticker
    response.raise_for_status()
    return parse_tiingo_daily(response.json())


def yahoo_rows(client: httpx.Client, symbol: str) -> list[list[Any]]:
    """Former members that still trade (e.g. AAL) are on Yahoo; delisted ones 404."""
    try:
        bars, dividends = fetch_symbol(client, symbol, date(1996, 1, 1), date.today())
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    return [
        [b.day, b.open, b.high, b.low, b.close, b.adj_close, float(b.volume)]
        + [dividends.get(b.day, 0.0), 1.0]
        for b in bars
    ]


Fetcher = tuple[str, Callable[[str], list[list[Any]]]]


def collect_delisted(
    targets: list[Interval],
    store: Any,
    fetchers: list[Fetcher],
    today: date,
    *,
    execute: bool,
    pause: float,
    wait_minutes: float,
    max_waits: int,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _m: None,
    skip: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Tries each source in order and keeps the first series that covers the membership window.

    Resumable: stored tickers are skipped, and so are tickers an earlier run found missing.
    A quota hit waits ``wait_minutes`` and retries the same ticker, at most ``max_waits`` times.
    """
    by_symbol: dict[str, list[Interval]] = {}
    for item in targets:
        by_symbol.setdefault(item[0], []).append(item)
    results: dict[str, dict[str, Any]] = {}
    waits = 0
    stopped: str | None = None
    pending = sorted(by_symbol)
    i = 0
    while i < len(pending):
        symbol = pending[i]
        log(f"[{i + 1}/{len(pending)}] {symbol}")
        if store.has(DELISTED_KIND, symbol):
            days = {r[0] for r in store.read(DELISTED_KIND, symbol, "date")}
            results[symbol] = {"status": "stored", "source": "earlier run", "days": days}
            i += 1
            continue
        if skip and symbol in skip:  # known miss from an earlier run: don't spend quota again
            results[symbol] = {"status": skip[symbol], "source": None, "days": set()}
            i += 1
            continue
        status, source, days = "not_found", None, set()
        try:
            for name, fetch in fetchers:
                rows = fetch(symbol)
                sleep(pause)
                if not rows:
                    continue
                got = {r[0] for r in rows}
                best = max(window_coverage(got, a, b, today) for _s, a, b in by_symbol[symbol])
                if best < MIN_COVERAGE:
                    status = "rejected_window"  # re-used ticker or too short a history
                    continue
                status, source, days = "stored", name, got
                if execute:
                    store.write(
                        DELISTED_KIND,
                        symbol,
                        DELISTED_SCHEMA,
                        [[r[k] for r in rows] for k in range(len(DELISTED_SCHEMA))],
                        "date",
                    )
                break
        except RateLimited as exc:
            if waits >= max_waits:
                stopped = str(exc)
                break
            waits += 1
            log(f"    quota hit, waiting {wait_minutes:.0f} min ({waits}/{max_waits})")
            sleep(wait_minutes * 60)
            continue  # retry the same symbol
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            status = f"error: {type(exc).__name__}: {exc}"[:200]
        results[symbol] = {"status": status, "source": source, "days": days}
        i += 1
    return {"results": results, "waits": waits, "stopped": stopped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="local copy of the membership CSV")
    parser.add_argument("--since", type=date.fromisoformat, default=date(2000, 1, 1))
    parser.add_argument("--delisted", action="store_true", help="fetch former members (Tiingo)")
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--wait-minutes", type=float, default=61)
    parser.add_argument("--max-waits", type=int, default=14)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    tables = TableStore.from_settings(settings)
    daily = open_store(settings)
    today = date.today()
    if args.file:
        text = args.file.read_text()
    else:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get(HISTORY_URL)
            response.raise_for_status()
            text = response.text
    rows = parse_history(text)
    history = intervals(rows)
    if args.execute:
        tables.write(
            "meta",
            "sp500_history",
            HISTORY_SCHEMA,
            [[h[0] for h in history], [h[1] for h in history], [h[2] for h in history]],
            "symbol, start",
        )
    relevant = [h for h in history if h[2] is None or h[2] > args.since]
    have = set(daily.symbols())
    missing = [h for h in relevant if h[0] not in have]
    summary: dict[str, Any] = {
        "dry_run": not args.execute,
        "membership_rows": len(rows),
        "first": rows[0][0].isoformat(),
        "last": rows[-1][0].isoformat(),
        "intervals": len(history),
        f"tickers_since_{args.since}": len({h[0] for h in relevant}),
        "members_last_row": len(rows[-1][1]),
        "with_yahoo_daily": len({h[0] for h in relevant if h[0] in have}),
        "without_daily": len({h[0] for h in missing}),
    }
    coverage_rows: list[list[Any]] = []
    for s, a, b in relevant:
        if s in have:
            days = {bar.day for bar in daily.read_bars(s)}
            cov = window_coverage(days, max(a, args.since), b, today)
            coverage_rows.append(
                [s, a, b, "yahoo", cov, "ok" if cov >= MIN_COVERAGE else "partial"]
            )
    n_current = len(coverage_rows)
    if args.delisted:
        token = os.environ.get("TIINGO_TOKEN")
        if not token:
            print("put TIINGO_TOKEN=<token> in .env first", file=sys.stderr)
            return 2
        targets = [(s, max(a, args.since), b) for s, a, b in missing]
        skip: dict[str, str] = {}
        if tables.has("meta", "sp500_coverage"):
            for sym, status in tables.read("meta", "sp500_coverage", "symbol, status"):
                if status in ("not_found", "rejected_window"):
                    skip[sym] = status
        with (
            httpx.Client(timeout=60) as client,
            httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as yahoo,
        ):
            out = collect_delisted(
                targets,
                tables,
                [
                    ("yahoo", lambda s: yahoo_rows(yahoo, s)),
                    ("tiingo", lambda s: fetch_tiingo_daily(client, s, token)),
                ],
                today,
                execute=args.execute,
                pause=args.pause,
                wait_minutes=args.wait_minutes,
                max_waits=args.max_waits,
                log=lambda m: print(m, file=sys.stderr, flush=True),
                skip=skip,
            )
        results = out["results"]
        for s, a, b in targets:
            res = results.get(s)
            if res is None:
                coverage_rows.append([s, a, b, "none", 0.0, "not_tried"])
                continue
            cov = window_coverage(res["days"], a, b, today) if res["days"] else 0.0
            coverage_rows.append([s, a, b, res["source"] or "none", cov, res["status"]])
        statuses: dict[str, int] = {}
        for r in coverage_rows[n_current:]:
            key = f"{r[5]} ({r[3]})" if r[5] == "stored" else r[5]
            statuses[key] = statuses.get(key, 0) + 1
        summary["former_members"] = {
            "by_status": statuses,
            "quota_waits": out["waits"],
            "stopped_by_quota": out["stopped"],
        }
    else:
        coverage_rows += [[s, a, b, "none", 0.0, "not_tried"] for s, a, b in missing]
    if args.execute:
        tables.write(
            "meta",
            "sp500_coverage",
            COVERAGE_SCHEMA,
            [[r[k] for r in coverage_rows] for k in range(len(COVERAGE_SCHEMA))],
            "symbol, start",
        )
    usable = sum(1 for r in coverage_rows if r[5] in ("ok", "stored"))
    summary["usable_intervals"] = f"{usable}/{len(coverage_rows)}"
    text_out = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text_out + "\n")
    print(text_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
