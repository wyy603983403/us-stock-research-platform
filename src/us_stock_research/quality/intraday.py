"""Quality gate for 1-minute bars (Alpaca / Tiingo IEX feeds) and a two-source cross-check.

Timestamps are stored as naive UTC. The regular NYSE session is 09:30-16:00 New York time
(13:00 close on early-close days), so DST is handled by converting the session bounds, not
every row. Checks per symbol:

* trading days inside the data range with no regular-session bar (missing days);
* regular-session bars on NYSE holidays/weekends (extra days: a timestamp or calendar bug);
* OHLC consistency (high >= open/close >= low > 0);
* last regular-session close vs. the daily close already stored from Yahoo. Minute prices are
  unadjusted, so days whose ratio is a clean split factor (2:1, 1:10 ...) are counted separately,
  and so are runs where the ratio is flat for consecutive days (Yahoo back-adjusts "close" for
  spin-offs by a constant factor). Only isolated disagreements count as mismatches;
* minute coverage (bars / session minutes). IEX is one venue, so thin names legitimately miss
  minutes: reported as information, not failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from us_stock_research.calendar import is_trading_day, nyse_holidays, trading_days
from us_stock_research.storage import add_store_args, open_store, settings_from_args
from us_stock_research.tables import TableStore

ET = ZoneInfo("America/New_York")
SOURCES = {"alpaca": "intraday_1min_alpaca", "tiingo": "intraday_1min"}
COLUMNS = "ts, open, high, low, close, volume"
CLOSE_TOLERANCE = 0.02  # IEX last trade vs consolidated close
MAX_MISSING_FRACTION = 0.02
ADJUSTMENT_STABILITY = 0.005  # day-to-day drift of a corporate-action adjustment ratio
MAX_CLOSE_MISMATCH_FRACTION = 0.01

Row = tuple[Any, ...]  # (ts naive UTC, open, high, low, close, volume)


def early_close(day: date) -> bool:
    """13:00 close: Jul 3 and Dec 24 when they fall Mon-Thu, and the day after Thanksgiving."""
    if not is_trading_day(day):
        return False
    if (day.month, day.day) in ((7, 3), (12, 24)) and day.weekday() < 4:
        return True
    thanksgiving = next(
        d for d in nyse_holidays(day.year) if d.month == 11 and d.weekday() == 3 and d.day > 21
    )
    return day == thanksgiving + timedelta(days=1)


@cache
def session_utc(day: date) -> tuple[datetime, datetime]:
    """Regular session [open, close) of an NYSE day as naive UTC datetimes."""
    close_at = time(13, 0) if early_close(day) else time(16, 0)

    def to_utc(t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=ET).astimezone(UTC).replace(tzinfo=None)

    return to_utc(time(9, 30)), to_utc(close_at)


def last_closed_session(now_utc: datetime) -> date:
    """Latest NYSE trading day whose regular session has closed by ``now_utc`` (aware or naive UTC).

    Downloaders use it as the default end date so a run during (or before) the US session never
    stores a half-finished day, and a run after the close still picks that day up.
    """
    now = now_utc.astimezone(UTC).replace(tzinfo=None) if now_utc.tzinfo else now_utc
    day = now.date()
    while True:
        if is_trading_day(day) and session_utc(day)[1] <= now:
            return day
        day -= timedelta(days=1)


def session_minutes(day: date) -> int:
    start, stop = session_utc(day)
    return int((stop - start).total_seconds() // 60)


def load_intraday_exceptions(
    path: Path, source: str, symbol: str | None = None
) -> tuple[set[date], date | None]:
    """Known-bad days for a source and, if given, the first valid day of ``symbol``."""
    if not path.exists():
        return set(), None
    raw = (yaml.safe_load(path.read_text()) or {}).get(source) or {}
    bad = {date.fromisoformat(str(d)) for d in raw.get("bad_days") or []}
    start = (raw.get("symbol_start") or {}).get(symbol) if symbol else None
    return bad, date.fromisoformat(str(start)) if start else None


def regular_session(
    rows: Iterable[Row], exclude: set[date] | None = None, since: date | None = None
) -> list[Row]:
    """Keep only bars inside the regular session of a trading day (for research use).

    ``exclude``/``since`` come from ``load_intraday_exceptions`` (vendor outages, re-used tickers).
    """
    out: list[Row] = []
    for row in rows:
        day = row[0].date()
        if (exclude and day in exclude) or (since and day < since):
            continue
        if is_trading_day(day):
            start, stop = session_utc(day)
            if start <= row[0] < stop:
                out.append(row)
    return out


def _split_like(ratio: float) -> bool:
    """True when raw/split-adjusted looks like a (cumulative) split factor: 2, 3:2, 16, 1/10 ..."""
    x = max(ratio, 1 / ratio)
    if x < 1.4:
        return False
    for denominator in (1, 2, 3):
        scaled = x * denominator
        nearest = round(scaled)
        if nearest >= 2 and abs(scaled / nearest - 1) <= CLOSE_TOLERANCE:
            return True
    return False


def audit_intraday(
    symbol: str, rows: list[Row], daily_close: dict[date, float] | None = None
) -> dict[str, Any]:
    per_day: dict[date, dict[str, Any]] = {}
    outside = 0
    ohlc_errors: list[str] = []
    for ts, o, h, low, c, _v in rows:
        day = ts.date()
        start, stop = session_utc(day)
        if not start <= ts < stop:
            outside += 1
            continue
        info = per_day.setdefault(day, {"n": 0, "bad": 0, "close": c, "last": ts})
        info["n"] += 1
        if ts >= info["last"]:
            info["last"], info["close"] = ts, c
        if min(o, h, low, c) <= 0 or h < max(o, c) - 1e-9 or low > min(o, c) + 1e-9:
            info["bad"] += 1
            if len(ohlc_errors) < 10:
                ohlc_errors.append(ts.isoformat())
    bad_bars = sum(v["bad"] for v in per_day.values())
    days = sorted(per_day)
    result: dict[str, Any] = {"symbol": symbol, "bars": len(rows), "outside_session": outside}
    if not days:
        return {**result, "days": 0, "passed": False, "reasons": ["no regular-session bars"]}
    expected = trading_days(days[0], days[-1])
    # Data stops an hour or more before the close: fine for a thin name, but when many symbols
    # share the same truncated day it is a vendor outage (see market_bad_days in main()).
    truncated = [
        d
        for d in days
        if is_trading_day(d) and session_utc(d)[1] - per_day[d]["last"] > timedelta(minutes=60)
    ]
    missing = sorted(set(expected) - set(days))
    extra = [d for d in days if not is_trading_day(d)]
    coverage = [per_day[d]["n"] / session_minutes(d) for d in days if is_trading_day(d)]
    mismatches: list[tuple[date, float]] = []
    split_days = 0
    compared = 0
    ratios: list[tuple[date, float]] = []
    if daily_close:
        ratios = [(d, per_day[d]["close"] / daily_close[d]) for d in days if daily_close.get(d)]
        compared = len(ratios)
    adjusted_days = 0
    levels: set[float] = set()
    for i, (d, ratio) in enumerate(ratios):
        if abs(ratio - 1) <= CLOSE_TOLERANCE:
            continue
        if _split_like(ratio):
            split_days += 1
            continue
        # Yahoo back-adjusts "close" for spin-offs by a constant factor: the raw/adjusted ratio
        # then stays flat for months. A bad print is isolated, so compare with neighbours.
        neighbours = [ratios[j][1] for j in (i - 1, i + 1) if 0 <= j < len(ratios)]
        if any(abs(ratio / n - 1) <= ADJUSTMENT_STABILITY for n in neighbours):
            adjusted_days += 1
            levels.add(round(ratio, 3))
        else:
            mismatches.append((d, ratio - 1))
    mismatches.sort(key=lambda x: -abs(x[1]))
    missing_fraction = len(missing) / len(expected)
    mismatch_fraction = len(mismatches) / compared if compared else 0.0
    reasons: list[str] = []
    if bad_bars:
        reasons.append(f"{bad_bars} bars violate OHLC ordering")
    if extra:
        reasons.append(f"{len(extra)} regular-session days on NYSE holidays/weekends")
    if missing_fraction > MAX_MISSING_FRACTION:
        reasons.append(f"{len(missing)} trading days missing ({missing_fraction:.1%})")
    if mismatch_fraction > MAX_CLOSE_MISMATCH_FRACTION:
        reasons.append(
            f"{len(mismatches)} days last close differs from daily close by >"
            f"{CLOSE_TOLERANCE:.0%} ({mismatch_fraction:.1%})"
        )
    return {
        **result,
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "days": len(days),
        "missing_days": len(missing),
        "missing_sample": [d.isoformat() for d in missing[:10]],
        "missing_all": [d.isoformat() for d in missing],
        "extra_days": [d.isoformat() for d in extra[:10]],
        "early_close_days": sum(1 for d in days if early_close(d)),
        "mean_coverage": round(sum(coverage) / len(coverage), 4) if coverage else 0.0,
        "median_coverage": round(median(coverage), 4) if coverage else 0.0,
        "low_coverage_days": sum(1 for x in coverage if x < 0.5),
        "truncated_days": [d.isoformat() for d in truncated],
        "ohlc_error_bars": bad_bars,
        "ohlc_error_sample": ohlc_errors,
        "close_compared_days": compared,
        "close_mismatch_days": len(mismatches),
        "close_split_factor_days": split_days,
        "close_adjustment_days": adjusted_days,
        "close_adjustment_levels": sorted(levels)[:10],
        "close_worst": [{"date": d.isoformat(), "diff": round(x, 4)} for d, x in mismatches[:5]],
        "passed": not reasons,
        "reasons": reasons,
    }


def compare_sources(a: list[Row], b: list[Row], tolerance: float = 0.002) -> dict[str, Any]:
    """Minute-by-minute close agreement on the overlapping regular session.

    Also tries +-1 minute shifts: if a shifted alignment agrees better, the vendors label bars
    differently (bar start vs. bar end) and every intraday signal would be off by a minute.
    """
    ra = {r[0]: r[4] for r in regular_session(a)}
    rb = {r[0]: r[4] for r in regular_session(b)}
    if not ra or not rb:
        return {"common_minutes": 0, "passed": False, "reasons": ["one side has no data"]}
    lo = max(min(ra), min(rb))
    hi = min(max(ra), max(rb))
    ra = {t: v for t, v in ra.items() if lo <= t <= hi}
    rb = {t: v for t, v in rb.items() if lo <= t <= hi}

    def agreement(shift: int) -> tuple[int, int, list[float]]:
        diffs = [
            abs(rb[t + timedelta(minutes=shift)] / v - 1)
            for t, v in ra.items()
            if t + timedelta(minutes=shift) in rb
        ]
        return len(diffs), sum(1 for x in diffs if x > tolerance), diffs

    by_shift = {s: agreement(s) for s in (-1, 0, 1)}
    common, bad, diffs = by_shift[0]
    rate = {s: (n - k) / n if n else 0.0 for s, (n, k, _d) in by_shift.items()}
    best = max(rate, key=lambda s: rate[s])
    reasons: list[str] = []
    if not common:
        reasons.append("no common minutes")
    elif bad / common > 0.01:
        reasons.append(f"{bad / common:.1%} of common minutes differ by >{tolerance:.1%}")
    if best != 0 and rate[best] > rate[0] + 0.05:
        reasons.append(f"bars align better shifted by {best:+d} minute: label convention differs")
    return {
        "overlap": [lo.isoformat(), hi.isoformat()],
        "minutes_a": len(ra),
        "minutes_b": len(rb),
        "common_minutes": common,
        "mismatch_minutes": bad,
        "median_abs_diff_bps": round(median(diffs) * 1e4, 3) if diffs else None,
        "agreement_by_shift": {str(s): round(r, 4) for s, r in rate.items()},
        "passed": not reasons,
        "reasons": reasons,
    }


def market_bad_days(audits: dict[str, Any], share: float = 0.2) -> dict[str, str]:
    """Days on which more than ``share`` of the symbols trading then are missing or truncated."""
    live: dict[str, int] = {}
    hit: dict[str, int] = {}
    for a in audits.values():
        if not a.get("days"):
            continue
        for d in trading_days(date.fromisoformat(a["first"]), date.fromisoformat(a["last"])):
            live[d.isoformat()] = live.get(d.isoformat(), 0) + 1
        for d in set(a.get("missing_all", [])) | set(a.get("truncated_days", [])):
            hit[d] = hit.get(d, 0) + 1
    return {
        d: f"{n}/{live[d]}"
        for d, n in sorted(hit.items())
        if live.get(d) and n / live[d] > share and live[d] >= 5
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit stored 1-minute bars.")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--all", action="store_true", help="every symbol stored for --source")
    parser.add_argument("--source", choices=sorted(SOURCES), default="alpaca")
    parser.add_argument(
        "--crosscheck", action="store_true", help="also compare Alpaca vs Tiingo where both exist"
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/quality/intraday.json"))
    add_store_args(parser)
    args = parser.parse_args(argv)
    settings = settings_from_args(args)
    tables = TableStore.from_settings(settings)
    daily = open_store(settings)
    kind = SOURCES[args.source]
    symbols = [s.upper() for s in args.symbols]
    if args.all:
        symbols += tables.keys(kind)
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        print("give symbols or --all", file=sys.stderr)
        return 2
    audits: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}", file=sys.stderr, flush=True)
        if not tables.has(kind, symbol):
            audits[symbol] = {"passed": False, "reasons": [f"no {args.source} minute data"]}
            continue
        rows = tables.read(kind, symbol, COLUMNS)
        closes = (
            {b.day: b.close for b in daily.read_bars(symbol)} if daily.has_bars(symbol) else None
        )
        audits[symbol] = audit_intraday(symbol, rows, closes)
        if args.crosscheck:
            other = SOURCES["tiingo" if args.source == "alpaca" else "alpaca"]
            if tables.has(other, symbol):
                checks[symbol] = compare_sources(rows, tables.read(other, symbol, COLUMNS))
    failed = sorted(s for s, a in audits.items() if not a["passed"])
    bad_days = market_bad_days(audits)
    report = {
        "source": args.source,
        "market_bad_days": bad_days,
        "symbols": len(audits),
        "passed": len(audits) - len(failed),
        "failed": {s: audits[s]["reasons"] for s in failed},
        "crosscheck_failed": {s: c["reasons"] for s, c in checks.items() if not c["passed"]},
        "audits": audits,
        "crosscheck": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if len(audits) <= 5:
        print(json.dumps({"audits": audits, "crosscheck": checks}, indent=2, ensure_ascii=False))
    brief = {
        k: report[k]
        for k in ("source", "symbols", "passed", "failed", "crosscheck_failed", "market_bad_days")
    }
    brief["report"] = str(args.output)
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    return 0 if not failed and not report["crosscheck_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
