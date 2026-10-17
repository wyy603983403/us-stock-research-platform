"""Pure helpers for the single-stock section (``usr-stocks look | monitor | screen | report``).

Everything here is descriptive: trend and risk figures from daily closes, past earnings dates
from SEC 8-K filings (Item 2.02 "Results of Operations and Financial Condition"), a SPY "shadow"
account for the money paid in, and yearly totals from the fills file. Nothing ranks stocks by
expected return and nothing here places orders.
"""

from __future__ import annotations

import csv
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any

Series = list[tuple[date, float]]  # (day, adjusted close), oldest first


def _ret(closes: list[float], n: int) -> float | None:
    return closes[-1] / closes[-1 - n] - 1 if len(closes) > n and closes[-1 - n] > 0 else None


def _daily(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]


def annual_vol(closes: list[float], n: int = 60) -> float | None:
    rets = _daily(closes[-(n + 1) :])
    if len(rets) < max(2, n // 2):
        return None
    return statistics.stdev(rets) * math.sqrt(252)


def beta(stock: Series, bench: Series, n: int = 252) -> float | None:
    """Slope of the stock's daily returns on the benchmark's over the last ``n`` common days."""
    b = dict(bench)
    days = [d for d, _ in stock if d in b][-(n + 1) :]
    s = dict(stock)
    xs = _daily([b[d] for d in days])
    ys = _daily([s[d] for d in days])
    if len(xs) < 60 or len(xs) != len(ys):
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var


def max_drawdown(closes: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1)
    return worst


def profile(stock: Series, bench: Series | None = None) -> dict[str, Any] | None:
    """Trend and risk figures from adjusted closes; None without any history."""
    if not stock:
        return None
    closes = [c for _, c in stock]
    last = closes[-1]
    out: dict[str, Any] = {
        "day": stock[-1][0],
        "close": last,
        "chg_1d": _ret(closes, 1),
        "ret_1m": _ret(closes, 21),
        "ret_3m": _ret(closes, 63),
        "ret_6m": _ret(closes, 126),
        "ret_12m": _ret(closes, 252),
        "momentum_12_1": (closes[-22] / closes[-253] - 1) if len(closes) >= 253 else None,
        "vol_60": annual_vol(closes, 60),
        "max_dd_1y": max_drawdown(closes[-252:]),
        "from_high": last / max(closes[-252:]) - 1,
        "beta": beta(stock, bench) if bench else None,
    }
    for n in (50, 200):
        if len(closes) >= n:
            sma = sum(closes[-n:]) / n
            out[f"vs_sma{n}"] = last / sma - 1
        else:
            out[f"vs_sma{n}"] = None
    return out


# ---------------------------------------------------------------- earnings (SEC 8-K Item 2.02)


def earnings_dates(submissions: dict[str, Any]) -> list[date]:
    """Filing dates of 8-K (not amendments) carrying Item 2.02, oldest first, one per day."""
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    items = recent.get("items") or [""] * len(forms)
    days = recent.get("filingDate") or []
    out = {
        date.fromisoformat(days[i])
        for i, f in enumerate(forms)
        if f == "8-K" and "2.02" in str(items[i] or "").split(",")
    }
    return sorted(out)


def next_earnings(dates: list[date], today: date) -> dict[str, Any] | None:
    """Next report date estimated as the last one plus the median of the last four gaps
    (gaps outside 45-135 days, e.g. pre-announcements, are ignored)."""
    if not dates:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)][-6:]
    usable = [g for g in gaps if 45 <= g <= 135][-4:]
    gap = int(statistics.median(usable)) if usable else 91
    est = dates[-1] + timedelta(days=gap)
    return {
        "last": dates[-1],
        "recent": dates[-4:],
        "gap_days": gap,
        "estimate": est,
        "days_to": (est - today).days,
        "basis": "间隔推算" if usable else "按 91 天推算",
    }


# ---------------------------------------------------------------- section-level figures


def shadow_spy(
    deposits: list[tuple[date, float]], spy: dict[date, float], day: date
) -> dict[str, float] | None:
    """Value on ``day`` of buying SPY (dividends reinvested: adjusted closes) with every deposit
    on the day it was made (withdrawals sell); None when SPY prices are missing."""
    days = sorted(d for d in spy if d <= day)
    if not days or not deposits:
        return None
    units, paid = 0.0, 0.0
    for d, amount in deposits:
        prior = [x for x in days if x <= d]
        px = spy[prior[-1]] if prior else spy[days[0]]
        units += amount / px
        paid += amount
    value = units * spy[days[-1]]
    return {"value": value, "paid": paid, "return": value / paid - 1 if paid else 0.0}


def deposits_of(ledger: dict[str, Any]) -> list[tuple[date, float]]:
    """Money paid in: the opening cash plus every ``cash --deposit`` entry (negative = out)."""
    adj = ledger.get("cash_adjustments") or []
    later = sum(float(a["amount"]) for a in adj if a.get("deposit"))
    opening = float(ledger["funded_usd"]) - later
    out = [(date.fromisoformat(str(ledger["start_day"])), opening)]
    out += [(date.fromisoformat(str(a["at"])), float(a["amount"])) for a in adj if a.get("deposit")]
    return sorted(out)


def weighted(rows: list[dict[str, Any]], key: str) -> float | None:
    """Value-weighted average of ``key`` over holdings that have it (cash counts as zero)."""
    total = sum(r["value"] for r in rows)
    have = [r for r in rows if r.get(key) is not None]
    if total <= 0 or not have:
        return None
    return float(sum(r["value"] * r[key] for r in have)) / total


def sector_weights(rows: list[dict[str, Any]], nav: float) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    for r in rows:
        sec = r.get("sector") or "其他/未知"
        out[sec] = out.get(sec, 0.0) + r["value"]
    return sorted(((s, v / nav) for s, v in out.items()), key=lambda x: -x[1]) if nav > 0 else []


def yearly_summary(fills: Path, ledger: dict[str, Any]) -> dict[int, dict[str, float]]:
    """Per calendar year: bought, sold, fees, realized P&L (average cost, as recorded), dividends
    and other cash entries (``cash`` without --deposit), deposits."""
    years: dict[int, dict[str, float]] = {}

    def y(day: str) -> dict[str, float]:
        return years.setdefault(
            int(day[:4]),
            {"bought": 0.0, "sold": 0.0, "fees": 0.0, "realized": 0.0, "other_cash": 0.0,
             "deposits": 0.0, "trades": 0.0},
        )  # fmt: skip

    if fills.exists():
        with fills.open(newline="") as fh:
            for r in csv.DictReader(fh):
                row = y(r["day"])
                amount = float(r["qty"]) * float(r["price"])
                row["bought" if r["side"] == "BUY" else "sold"] += amount
                row["fees"] += float(r.get("fee") or 0)
                row["realized"] += float(r.get("realized_usd") or 0)
                row["trades"] += 1
    y(str(ledger["start_day"]))["deposits"] += float(ledger["funded_usd"]) - sum(
        float(a["amount"]) for a in ledger.get("cash_adjustments") or [] if a.get("deposit")
    )
    for a in ledger.get("cash_adjustments") or []:
        y(str(a["at"]))["deposits" if a.get("deposit") else "other_cash"] += float(a["amount"])
    return dict(sorted(years.items()))


# ---------------------------------------------------------------- weekly screen comparison


def screen_changes(current: list[str], previous: list[str] | None) -> dict[str, list[str]] | None:
    if previous is None:
        return None
    cur, prev = set(current), set(previous)
    return {
        "new": [s for s in current if s not in prev],
        "dropped": [s for s in previous if s not in cur],
    }


def holding_checks(
    held: list[str], rows: dict[str, dict[str, Any]], picked: list[str], vol_cut: float | None
) -> list[dict[str, Any]]:
    """Where each holding stands under the screen rules this week."""
    out = []
    for s in sorted(held):
        r = rows.get(s)
        if r is None:
            out.append({"symbol": s, "evaluated": False})
            continue
        out.append(
            {
                "symbol": s,
                "evaluated": True,
                "mom_pct": r.get("mom_pct"),
                "above_sma": r["above_sma"],
                "vol_ok": vol_cut is None or r["vol"] <= vol_cut,
                "rank": picked.index(s) + 1 if s in picked else None,
            }
        )
    return out


# ---------------------------------------------------------------- watchlist


def load_watch(path: Path) -> dict[str, dict[str, Any]]:
    """``{symbol: {added, note, above_sma, in_screen, earn_notified}}`` (empty when missing)."""
    import yaml

    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return {str(s): dict(v or {}) for s, v in (raw.get("symbols") or {}).items()}


def save_watch(path: Path, watch: dict[str, dict[str, Any]]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"symbols": {s: watch[s] for s in sorted(watch)}}
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False))


def watch_events(
    watch: dict[str, dict[str, Any]],
    figures: dict[str, dict[str, Any]],
    candidates: set[str] | None,
    within_days: int | None,
) -> list[str]:
    """Reminders for watched symbols, each only once per change (``watch`` is updated):
    a close crossing the 200-day line, entering or leaving the latest weekly candidates, and an
    estimated report date coming within ``within_days``. A symbol's first check sets the state
    without a reminder."""
    notes = []
    for s in sorted(watch):
        st, fig = watch[s], figures.get(s) or {}
        vs = fig.get("vs_sma200")
        if vs is not None:
            above = vs >= 0
            if st.get("above_sma") is not None and above != st["above_sma"]:
                notes.append(f"观察 {s} {'站上' if above else '跌破'} 200 日线（{vs:+.0%}）")
            st["above_sma"] = above
        if candidates is not None:
            inside = s in candidates
            if st.get("in_screen") is not None and inside != st["in_screen"]:
                notes.append(f"观察 {s} {'进入' if inside else '掉出'}每周候选清单")
            st["in_screen"] = inside
        earn = fig.get("earnings")
        if within_days is not None and earn and 0 <= earn["days_to"] <= within_days:
            key = earn["estimate"].isoformat()
            if st.get("earn_notified") != key:
                notes.append(
                    f"观察 {s} 预计 {earn['estimate']:%m-%d} 前后发财报"
                    f"（推算，{earn['days_to']} 天后）"
                )
                st["earn_notified"] = key
    return notes
