"""Independent re-implementation of the cross-section backtest, used only to verify it.

Written separately from ``cross_section.py`` with different data structures (date-keyed dicts
instead of index-aligned lists, bisect lookups, explicit calendar walking) from the written
rules of the pre-registered contract. ``usr-verify-xsec`` runs both on the same data and prints
only the *differences* per month, never the returns themselves, so a verification run cannot
leak the study's result before the real run.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.calendar import is_trading_day

STALE = 5


class Series:
    def __init__(self, points: dict[date, float]) -> None:
        self.dates = sorted(points)
        self.values = [points[d] for d in self.dates]
        self.points = dict(points)


def trading_days_between(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def price_near(
    series: Series | None, cal: list[date], pos: int, stale: int
) -> tuple[date, float] | None:
    """Latest price on cal[pos - stale] .. cal[pos]."""
    if series is None or pos < 0:
        return None
    hi = cal[pos]
    lo = cal[max(pos - stale, 0)]
    k = bisect.bisect_right(series.dates, hi) - 1
    if k >= 0 and series.dates[k] >= lo:
        return series.dates[k], series.values[k]
    return None


def signal(series: Series, cal: list[date], pos: int, spec: dict[str, Any]) -> float | None:
    lookback = int(spec["lookback_days"])
    if spec["name"] == "momentum_12_1":
        recent = price_near(series, cal, pos - int(spec.get("skip_days", 0)), STALE)
        past = price_near(series, cal, pos - lookback, STALE)
        if recent is None or past is None or past[1] <= 0:
            return None
        return recent[1] / past[1] - 1
    if pos - lookback < 0:
        return None
    window = cal[pos - lookback : pos + 1]
    points = series.points
    rets = []
    for a, b in zip(window, window[1:], strict=False):
        pa, pb = points.get(a), points.get(b)
        if pa and pb and pa > 0:
            rets.append(pb / pa - 1)
    if len(rets) < 0.8 * lookback:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def period_return(
    series: Series | None, cal: list[date], entry: int, exit_: int, haircut: float
) -> float:
    start = price_near(series, cal, entry, STALE)
    if start is None:
        return haircut
    end = price_near(series, cal, exit_, STALE)
    if end is not None:
        return end[1] / start[1] - 1
    assert series is not None
    k = bisect.bisect_right(series.dates, cal[exit_]) - 1  # last close since entry
    last = series.values[k] if k >= 0 and series.dates[k] >= start[0] else start[1]
    return (1 + last / start[1] - 1) * (1 + haircut) - 1


def book(
    target: dict[str, float], held: dict[str, float], rets: dict[str, float], bps: float
) -> tuple[float, dict[str, float]]:
    """Net period return of moving from ``held`` to ``target``, and the drifted weights."""
    traded = sum(abs(target.get(s, 0) - held.get(s, 0)) for s in set(target) | set(held))
    gross = sum(w * rets[s] for s, w in target.items())
    value = {s: w * (1 + rets[s]) for s, w in target.items()}
    total = sum(value.values())
    drifted = {s: v / total for s, v in value.items()} if total > 0 else {}
    return gross - traded * bps, drifted


def backtest(
    contract: dict[str, Any],
    prices: dict[str, dict[date, float]],
    history: list[tuple[str, date, date | None]],
    haircut: float = 0.0,
    excluded: set[str] | None = None,
    blocked: list[tuple[str, date, date | None]] | None = None,
) -> list[dict[str, Any]]:
    uni = contract["universe"]
    series = {s: Series(p) for s, p in prices.items() if p}
    first_day = min(min(p) for p in prices.values() if p)
    cal = trading_days_between(first_day, uni["end"] + timedelta(days=45))
    pos = {d: i for i, d in enumerate(cal)}
    rebal = [
        d
        for i, d in enumerate(cal[:-1])
        if uni["start"] <= d <= uni["end"] and cal[i + 1].month != d.month
    ]
    lag = int(contract["execution_lag_days"])
    bps = float(contract.get("transaction_cost_bps", 0.0)) / 10_000
    need = int(uni["min_history_days"])
    spec = contract["signal"]
    top = int(contract["selection"]["top_n"])
    excluded = excluded or set()
    prev_w: dict[str, float] = {}
    prev_ew: dict[str, float] = {}
    out = []
    for d, d_next in zip(rebal, rebal[1:], strict=False):
        i = pos[d]
        members = {s for s, a, b in history if a <= d and (b is None or d < b)}
        wrong_company = {s for s, a, b in blocked or [] if a <= d and (b is None or d < b)}
        scores = {}
        for s in members - excluded - wrong_company:
            ser = series.get(s)
            if price_near(ser, cal, i, STALE) is None:
                continue
            if price_near(ser, cal, i - need, STALE) is None:
                continue
            v = signal(ser, cal, i, spec)  # type: ignore[arg-type]
            if v is not None:
                scores[s] = v
        if spec["name"] == "low_volatility":
            order = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
        else:
            order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        picks = [s for s, _ in order[:top]]
        entry = i + lag
        exit_ = min(pos[d_next] + lag, len(cal) - 1)
        rets = {s: period_return(series.get(s), cal, entry, exit_, haircut) for s in scores}

        strat, prev_w = book({s: 1 / len(picks) for s in picks} if picks else {}, prev_w, rets, bps)
        ew, prev_ew = book(
            {s: 1 / len(scores) for s in scores} if scores else {}, prev_ew, rets, bps
        )
        out.append({"date": d.isoformat(), "strategy": strat, "equal_weight": ew, "picks": picks})
    return out


def compare(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    """Differences only: months, max |diff| of each series, first months that disagree."""
    if [m["date"] for m in a] != [m["date"] for m in b]:
        return {"match": False, "reason": "different rebalance dates"}
    worst = {"strategy": 0.0, "equal_weight": 0.0}
    bad: list[dict[str, Any]] = []
    pick_mismatch = 0
    for x, y in zip(a, b, strict=True):
        for k in worst:
            diff = abs(x[k] - y[k])
            worst[k] = max(worst[k], diff)
            if diff > 1e-9 and len(bad) < 5:
                bad.append({"date": x["date"], "series": k, "abs_diff": diff})
        pick_mismatch += sorted(x["top"] if "top" in x else x["picks"][:10]) != sorted(
            y["top"] if "top" in y else y["picks"][:10]
        )
    return {
        "months": len(a),
        "max_abs_diff": worst,
        "top10_pick_mismatch_months": pick_mismatch,
        "first_disagreements": bad,
        "match": all(v < 1e-9 for v in worst.values()) and pick_mismatch == 0,
    }


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.quality.ohlcv import load_exceptions
    from us_stock_research.research import cross_section as xs
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    args = parser.parse_args(argv)
    contract = xs.load_xs_contract(args.contract)
    tables = TableStore.from_settings(load_settings())
    uni = contract["universe"]
    loaded = xs.load_prices(
        tables, uni["start"] - timedelta(days=500), uni["end"] + timedelta(days=45)
    )
    xs.apply_aliases(loaded, xs.load_aliases(args.aliases))
    history = [
        tuple(r) for r in tables.read("meta", "sp500_history", "symbol, start_date, end_date")
    ]
    quarantined = set(load_exceptions(args.exceptions)[1])
    haircut = float(contract.get("delisting", {}).get("haircut_sensitivity", 0.0))
    as_dicts = {
        s: {d: v for d, v in zip(loaded.days, vals, strict=True) if v is not None}
        for s, vals in loaded.series.items()
    }
    blocked_map = xs.load_blocked(tables)
    blocked_list = [(s, a, b) for s, spans in blocked_map.items() for a, b in spans]
    report = {}
    for label, cut in (("base", 0.0), ("haircut", haircut)):
        engine = xs.run(
            contract, loaded, history, haircut=cut, excluded=quarantined, blocked=blocked_map
        )["months"]
        independent = backtest(
            contract, as_dicts, history, haircut=cut, excluded=quarantined, blocked=blocked_list
        )
        report[label] = compare(engine, independent)
    print(json.dumps(report, indent=2))
    return 0 if all(r.get("match") for r in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
