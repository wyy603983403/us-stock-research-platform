"""Incremental daily update of every symbol already in the store.

Adjusted close is a *total-return* series: when a dividend or split happens, Yahoo restates the
whole history. Appending new rows to old ones would leave a seam (old rows on the old
adjustment, new rows on the new one) and create a fake return of the dividend's size. So each
update re-fetches a short overlap window and compares it with what is stored:

* identical overlap  -> append only the new days;
* restated overlap   -> re-download and replace the full history (the adjustment changed);
* no overlap / gap   -> full re-download.

Frozen snapshots are content-addressed copies, so updating the store never changes a frozen
study; new data only enters a study through a new snapshot and a new study version.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from us_stock_research.bars import DailyBar, merge_bars
from us_stock_research.collectors.alpaca_daily import CROSSCHECK_TOLERANCE, crosscheck, fetch_daily
from us_stock_research.collectors.yahoo_daily import USER_AGENT, fetch_symbol
from us_stock_research.config import load_settings
from us_stock_research.quality.intraday import last_closed_session
from us_stock_research.quality.ohlcv import audit_bars, load_exceptions
from us_stock_research.storage import BarStore, open_store

OVERLAP_DAYS = 10
RESTATEMENT_TOLERANCE = 1e-6
FetchFn = Callable[[str, date, date], tuple[list[DailyBar], dict[date, float]]]
AltFn = Callable[[str, date, date], list[DailyBar]]  # backup source: raw daily bars


def plan(existing: list[DailyBar], tail: list[DailyBar]) -> tuple[str, str]:
    """Decide ``append`` or ``refresh`` and why."""
    old = {b.day: b for b in existing}
    overlap = [b for b in tail if b.day in old]
    if not overlap:
        return "refresh", "no overlap between stored data and download"
    for b in overlap:
        o = old[b.day]
        if abs(b.adj_close / o.adj_close - 1) > RESTATEMENT_TOLERANCE:
            return "refresh", f"adjusted close restated on {b.day} (dividend or split)"
        if abs(b.close / o.close - 1) > RESTATEMENT_TOLERANCE:
            return "refresh", f"close restated on {b.day} (split or spin-off)"
    return "append", "overlap identical"


def update_symbol(
    symbol: str,
    store: BarStore,
    fetch: FetchFn,
    today: date,
    full_start: date,
    *,
    execute: bool,
) -> dict[str, Any]:
    existing = store.read_bars(symbol)
    last = existing[-1].day
    tail, tail_divs = fetch(symbol, last - timedelta(days=OVERLAP_DAYS), today)
    action, reason = plan(existing, tail)
    if action == "refresh":
        bars, divs = fetch(symbol, full_start, today)
        merged, all_divs = merge_bars([], bars), divs  # sorted, one bar per day
    else:
        merged = merge_bars(existing, tail)
        all_divs = (store.read_dividends(symbol) if store.has_dividends(symbol) else {}) | tail_divs
    new_days = len(merged) - len(existing)
    if execute and merged:
        store.write_bars(symbol, merged)
        store.write_dividends(symbol, all_divs)
    return {
        "symbol": symbol,
        "action": action,
        "reason": reason,
        "new_days": new_days,
        "last": merged[-1].day.isoformat() if merged else last.isoformat(),
    }


def _use_alt(
    symbol: str,
    store: BarStore,
    alt: AltFn,
    today: date,
    execute: bool,
    failed: dict[str, str],
    results: list[dict[str, Any]],
    fallback: dict[str, int],
    checks: dict[str, dict[str, float]],
    alt_errors: dict[str, str],
) -> None:
    """Backup source: fill the days Yahoo did not deliver, or cross-check its latest close."""
    existing = store.read_bars(symbol)
    if not existing:
        return
    last = existing[-1]
    try:
        if symbol in failed:
            new = [b for b in alt(symbol, last.day + timedelta(days=1), today) if b.day > last.day]
            if new and execute:
                store.write_bars(symbol, existing + new)
            fallback[symbol] = len(new)
            if new and new[-1].day == today:
                del failed[symbol]  # reported under "fallback" instead
            if new:
                results.append({"symbol": symbol, "action": "append_backup", "reason": "alpaca",
                                "new_days": len(new), "last": new[-1].day.isoformat()})  # fmt: skip
        elif last.day == today:
            rows = [b for b in alt(symbol, today, today) if b.day == today]
            if rows:
                checks[symbol] = {"yahoo": last.close, "alpaca": rows[0].close,
                                  "diff": crosscheck(last.close, rows[0].close)}  # fmt: skip
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        alt_errors[symbol] = f"{type(exc).__name__}: {exc}"[:200]


def _transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


def run_update(
    symbols: list[str],
    store: BarStore,
    fetch: FetchFn,
    today: date,
    full_start: date,
    *,
    execute: bool,
    pause: float,
    sleep: Callable[[float], None] = time.sleep,
    exceptions: Path | None = None,
    retries: int = 3,
    backoff: float = 30.0,
    retry_budget: float = 600.0,
    alt: AltFn | None = None,
) -> dict[str, Any]:
    """``retries``: extra attempts after HTTP 429 / 5xx / network errors, waiting backoff × 3^n;
    total waiting per run is capped by ``retry_budget`` seconds (a blocked source must not stall
    a 600-symbol update)."""
    results: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    retried: dict[str, int] = {}
    fallback: dict[str, int] = {}
    checks: dict[str, dict[str, float]] = {}
    alt_errors: dict[str, str] = {}
    waited = 0.0
    for symbol in symbols:
        attempt = 0
        while True:
            try:
                results.append(
                    update_symbol(symbol, store, fetch, today, full_start, execute=execute)
                )
                break
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                wait = backoff * 3**attempt
                if _transient(exc) and attempt < retries and waited + wait <= retry_budget:
                    sleep(wait)
                    waited += wait
                    attempt += 1
                    retried[symbol] = attempt
                    continue
                failed[symbol] = f"{type(exc).__name__}: {exc}"[:200]
                break
        if alt is not None:
            _use_alt(symbol, store, alt, today, execute, failed, results, fallback, checks,
                     alt_errors)  # fmt: skip
        sleep(pause)
    accepted, quarantine = load_exceptions(exceptions) if exceptions else ({}, {})
    audited = {
        r["symbol"]: audit_bars(
            r["symbol"], store.read_bars(r["symbol"]), accepted.get(r["symbol"])
        )
        for r in results
        if r["symbol"] not in quarantine  # already known bad: reported separately
    }
    return {
        "quarantined": sorted(s for s in quarantine if any(r["symbol"] == s for r in results)),
        "updated": len([r for r in results if r["new_days"] > 0]),
        "refreshed": [r for r in results if r["action"] == "refresh"],
        "appended_days": sum(r["new_days"] for r in results if r["action"] == "append"),
        "quality_errors": {s: a.errors[:3] for s, a in audited.items() if a.errors},
        "failed": failed,
        "retried": retried,
        "fallback": fallback,
        "crosscheck": checks,
        "crosscheck_mismatch": sorted(
            s for s, c in checks.items() if abs(c["diff"]) > CROSSCHECK_TOLERANCE
        ),
        "backup_errors": alt_errors,
        "retry_wait_seconds": waited,
        # the source did not deliver the latest session (throttling, stale cache, halted stock)
        "behind": sorted(r["symbol"] for r in results if r["last"] < today.isoformat()),
        "symbols": len(symbols),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="default: every symbol in the store")
    parser.add_argument("--full-start", type=date.fromisoformat, default=date(2000, 1, 1))
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=3, help="extra attempts on 429/5xx")
    parser.add_argument("--retry-budget", type=float, default=600.0, help="max seconds of waiting")
    parser.add_argument(
        "--alpaca-backup",
        action="store_true",
        help="Alpaca daily bars as backup + cross-check (needs ALPACA_KEY_ID/ALPACA_SECRET_KEY)",
    )
    parser.add_argument("--execute", action="store_true", help="write data (default: dry run)")
    parser.add_argument("--report", type=Path, help="write the JSON summary here too")
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    args = parser.parse_args(argv)
    settings = load_settings()
    store = open_store(settings)
    symbols = [s.upper() for s in args.symbols] or store.symbols()
    # never store a half-finished session (a run during US trading hours)
    today = last_closed_session(datetime.now(UTC))
    with httpx.Client(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        summary = run_update(
            symbols,
            store,
            lambda s, a, b: fetch_symbol(client, s, a, b),
            today,
            args.full_start,
            execute=args.execute,
            pause=args.pause,
            exceptions=args.exceptions,
            retries=args.retries,
            retry_budget=args.retry_budget,
            alt=_alpaca_alt(settings) if args.alpaca_backup else None,
        )
    summary = {"dry_run": not args.execute, "date": today.isoformat(), **summary}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(text)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())


def _alpaca_alt(settings: Any) -> AltFn | None:
    import os

    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        key, secret = (
            os.environ.get("ALPACA_PAPER_KEY_ID"),
            os.environ.get("ALPACA_PAPER_SECRET_KEY"),
        )
    if not key or not secret:
        print("备用数据源未启用：.env 里没有 Alpaca 密钥", file=sys.stderr)
        return None
    client = httpx.Client(timeout=settings.http_timeout_seconds)
    return lambda s, a, b: fetch_daily(client, s, a, b, key, secret)
