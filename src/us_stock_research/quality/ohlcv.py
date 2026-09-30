"""Read-only quality gate for daily bars.

Checks: duplicates/ordering, non-positive prices, OHLC consistency, calendar gaps longer than
a long weekend, and extreme single-day adjusted moves (likely bad split/dividend adjustment).
A study may only snapshot data whose report has ``passed: true``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from us_stock_research.bars import DailyBar
from us_stock_research.calendar import trading_days
from us_stock_research.storage import add_store_args, open_store, settings_from_args

MAX_CALENDAR_GAP_DAYS = 5  # e.g. Thu close -> Tue open around a holiday = 5
MAX_ABS_DAILY_RETURN = 0.40
MAX_ADJUSTMENT_MISMATCH = 0.15


@dataclass
class QualityReport:
    symbol: str
    rows: int
    first: str | None
    last: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.rows > 0 and not self.errors

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


MAX_MISSING_TRADING_DAY_FRACTION = 0.01


def _check_calendar(report: QualityReport, bars: list[DailyBar]) -> None:
    """Compare bar dates with the NYSE calendar between the first and last bar."""
    have = {b.day for b in bars}
    expected = trading_days(bars[0].day, bars[-1].day)
    missing = [d for d in expected if d not in have]
    extra = sorted(have.difference(expected))
    if missing:
        text = f"{len(missing)} NYSE trading days missing ({missing[0]} .. {missing[-1]})"
        if len(missing) / len(expected) > MAX_MISSING_TRADING_DAY_FRACTION:
            report.errors.append(text)
        else:
            report.warnings.append(text)
    if extra:
        report.warnings.append(f"{len(extra)} bars on non-trading days (first: {extra[0]})")


def audit_bars(
    symbol: str, bars: list[DailyBar], accepted: dict[str, str] | None = None
) -> QualityReport:
    report = QualityReport(
        symbol=symbol.upper(),
        rows=len(bars),
        first=bars[0].day.isoformat() if bars else None,
        last=bars[-1].day.isoformat() if bars else None,
    )
    if not bars:
        report.errors.append("no rows")
        return report
    # Indices, yields and futures are not investable prices: VIX can double in a day, CL=F went
    # negative in 2020, ^IRX touched zero. Price-level problems are warnings, not errors.
    macro = symbol.startswith("^") or "=" in symbol or symbol.upper().endswith(".NYB")
    price_issues = report.warnings if macro else report.errors
    for prev, cur in zip(bars, bars[1:], strict=False):
        if cur.day <= prev.day:
            report.errors.append(f"{cur.day}: duplicate or out-of-order date")
            continue
        gap = (cur.day - prev.day).days
        if macro and gap > MAX_CALENDAR_GAP_DAYS:
            report.warnings.append(f"{prev.day}->{cur.day}: {gap}-day calendar gap")
        if prev.adj_close <= 0 or prev.close <= 0:
            continue
        ret = cur.adj_close / prev.adj_close - 1.0
        raw = cur.close / prev.close - 1.0
        # Real single-day crashes/spikes happen (earnings, 2008); they are flagged, not rejected.
        if abs(ret) > MAX_ABS_DAILY_RETURN:
            report.warnings.append(f"{cur.day}: large adjusted move {ret:+.1%}")
        # Split-adjusted close and adjusted close may only differ by dividend size. A big
        # difference means a split/spin-off was applied to one series but not the other.
        if abs(ret - raw) > MAX_ADJUSTMENT_MISMATCH:
            price_issues.append(
                f"{cur.day}: adjusted vs raw return differ ({ret:+.1%} vs {raw:+.1%}); "
                "review for a spin-off or special distribution"
            )
    if not macro:
        _check_calendar(report, bars)
    for b in bars:
        if min(b.open, b.high, b.low, b.close, b.adj_close) <= 0:
            price_issues.append(f"{b.day}: non-positive price")
        elif b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            price_issues.append(f"{b.day}: inconsistent OHLC")
        if b.volume < 0:
            report.errors.append(f"{b.day}: negative volume")
    return _apply_reviewed(report, accepted or {})


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _apply_reviewed(report: QualityReport, accepted: dict[str, str]) -> QualityReport:
    """Errors on dates a human reviewed and accepted become warnings that carry the reason."""
    if not accepted:
        return report
    kept: list[str] = []
    for error in report.errors:
        # keyed by the first date: "2011-01-20: ..." or "17 days missing (2017-08-07 .."
        found = ISO_DATE.search(error)
        reason = accepted.get(found.group(0)) if found else None
        if reason:
            report.warnings.append(f"reviewed: {error} [{reason}]")
        else:
            kept.append(error)
    report.errors = kept
    return report


def load_exceptions(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Reviewed exceptions per symbol/date and the quarantine list (symbol -> reason)."""
    if not path.exists():
        return {}, {}
    raw = yaml.safe_load(path.read_text()) or {}
    accepted = {
        symbol: {str(item["date"]): str(item["reason"]) for item in items}
        for symbol, items in (raw.get("accepted") or {}).items()
    }
    return accepted, {k: str(v) for k, v in (raw.get("quarantine") or {}).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    add_store_args(parser)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    args = parser.parse_args(argv)
    accepted, quarantine = load_exceptions(args.exceptions)
    store = open_store(settings_from_args(args))
    reports: list[dict[str, object]] = []
    for symbol in args.symbols:
        bars = store.read_bars(symbol) if store.has_bars(symbol) else []
        report = audit_bars(symbol, bars, accepted.get(symbol.upper())).to_dict()
        if symbol.upper() in quarantine:
            report["quarantined"] = quarantine[symbol.upper()]
            report["passed"] = False
        reports.append(report)
    out = {"passed": all(r["passed"] for r in reports), "reports": reports}
    text = json.dumps(out, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
