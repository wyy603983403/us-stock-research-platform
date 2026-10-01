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


class Fundamentals:
    """Independent quality+value scoring from raw FSDS rows (no code shared with fundamentals.py).

    ``rows``: ``adsh, cik, sic, period, filed, tag, qtrs, uom, value`` of annual reports dated at
    their period end. ``segments``: ticker -> [(start, end, cik)]. ``actual``: the day's actual
    close per ticker. ``splits``: ticker -> [(day, ratio)]. ``known``: tickers with known splits.
    """

    FLOWS = ("net_income", "operating_cash_flow", "shares")

    def __init__(
        self,
        contract: dict[str, Any],
        rows: list[tuple[Any, ...]],
        segments: dict[str, list[tuple[date, date | None, int | None]]],
        actual: dict[str, dict[date, float]],
        splits: dict[str, list[tuple[date, float]]],
        known: set[str],
    ) -> None:
        spec = contract["fundamentals"]
        self.signal = contract["signal"]
        self.max_age = timedelta(days=int(spec["max_age_days"]))
        self.items = {
            k: list(spec[k])
            for k in ("net_income", "operating_cash_flow", "assets", "equity", "shares")
        }
        sic_ranges = list(contract["universe"].get("exclude_sic") or [])
        self.sic_ranges = [(sic_ranges[k], sic_ranges[k + 1]) for k in range(0, len(sic_ranges), 2)]
        # cik -> period -> list of (filed, adsh, sic, {tag: value})
        self.by_cik: dict[int, dict[date, dict[str, tuple[date, Any, dict[str, float]]]]] = {}
        for adsh, cik, sic, period, filed, tag, qtrs, uom, value in rows:
            item = next((k for k, tags in self.items.items() if tag in tags), None)
            if item is None or value is None:
                continue
            if qtrs != (4 if item in self.FLOWS else 0):
                continue
            if uom != ("shares" if item == "shares" else "USD"):
                continue
            filings = self.by_cik.setdefault(int(cik), {}).setdefault(period, {})
            entry = filings.setdefault(adsh, (filed, sic, {}))
            entry[2].setdefault(tag, float(value))
        self.segments = segments
        self.actual = {s: Series(p) for s, p in actual.items() if p}
        self.splits = splits
        self.known = known

    def facts(self, cik: int, day: date) -> dict[str, Any] | None:
        periods = self.by_cik.get(cik, {})
        best = None
        for period, filings in periods.items():
            if any(f[0] <= day for f in filings.values()) and (best is None or period > best):
                best = period
        if best is None or day - best > self.max_age:
            return None
        newest_first = sorted(
            ((f[0], adsh, f[1], f[2]) for adsh, f in periods[best].items() if f[0] <= day),
            reverse=True,
        )
        out: dict[str, Any] = {"sic": newest_first[0][2], "shares_filed": newest_first[0][0]}
        for item, tags in self.items.items():
            out[item] = None
            for filed, _adsh, _sic, values in newest_first:
                found = [values[tag] for tag in tags if tag in values]
                if found:
                    out[item] = found[0]
                    if item == "shares":
                        out["shares_filed"] = filed
                    break
        return out

    def mcap(self, sym: str, cal: list[date], pos: int, facts: dict[str, Any]) -> float | None:
        if sym not in self.known or facts["shares"] is None:
            return None
        hit = price_near(self.actual.get(sym), cal, pos, STALE)
        if hit is None:
            return None
        factor = math.prod(
            r for d, r in self.splits.get(sym, []) if facts["shares_filed"] < d <= cal[pos]
        )
        return float(hit[1] * facts["shares"] * factor)

    def scores(self, candidates: list[str], cal: list[date], pos: int) -> dict[str, float]:
        day = cal[pos]
        table: dict[str, dict[str, float]] = {}
        for sym in candidates:
            cik = next(
                (
                    c
                    for a, b, c in self.segments.get(sym, [])
                    if a <= day and (b is None or day < b)
                ),
                None,
            )
            facts = self.facts(cik, day) if cik is not None else None
            if facts is None:
                continue
            sic = facts["sic"]
            if sic is not None and any(lo <= sic <= hi for lo, hi in self.sic_ranges):
                continue
            mv = self.mcap(sym, cal, pos, facts)
            a, e = facts["assets"], facts["equity"]
            ni, cf = facts["net_income"], facts["operating_cash_flow"]
            row: dict[str, float] = {}
            if a is not None and a > 0:
                if ni is not None:
                    row["roa"] = ni / a
                if cf is not None:
                    row["cfoa"] = cf / a
            if mv is not None and mv > 0:
                if ni is not None:
                    row["earnings_yield"] = ni / mv
                if cf is not None:
                    row["cash_flow_yield"] = cf / mv
                if e is not None and e > 0:
                    row["book_to_market"] = e / mv
            table[sym] = row
        pct: dict[tuple[str, str], float] = {}
        for metric in list(self.signal["quality"]) + list(self.signal["value"]):
            vals = [(row[metric], s) for s, row in table.items() if metric in row]
            n = len(vals)
            for v, s in vals:
                below = sum(1 for w, _ in vals if w < v)
                ties = sum(1 for w, _ in vals if w == v)
                pct[(s, metric)] = 0.5 if n == 1 else (below + (ties - 1) / 2) / (n - 1)
        out = {}
        for s in table:
            qs = [pct[(s, m)] for m in self.signal["quality"] if (s, m) in pct]
            vs = [pct[(s, m)] for m in self.signal["value"] if (s, m) in pct]
            if qs and len(vs) >= 2:
                out[s] = (sum(qs) / len(qs) + sum(vs) / len(vs)) / 2
        return out


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
    fundamentals: Fundamentals | None = None,
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
        candidates = []
        for s in sorted(members - excluded - wrong_company):
            ser = series.get(s)
            if price_near(ser, cal, i, STALE) is None:
                continue
            if price_near(ser, cal, i - need, STALE) is None:
                continue
            candidates.append(s)
        if spec["name"] == "quality_value":
            assert fundamentals is not None, "quality_value needs fundamentals"
            scores = fundamentals.scores(candidates, cal, i)
        else:
            scores = {}
            for s in candidates:
                v = signal(series[s], cal, i, spec)
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


def coverage_by_year(months: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Input coverage only (no returns): per year the lowest fundamentals coverage and the
    smallest number of eligible stocks, so data gaps show before the real run."""
    out: dict[str, dict[str, float]] = {}
    for m in months:
        f = m["fundamentals"]
        base = f["candidates"] - f["financial"]
        cov = f["scored"] / base if base else 0.0
        row = out.setdefault(m["date"][:4], {"min_coverage": 1.0, "min_eligible": 1e9})
        row["min_coverage"] = round(min(row["min_coverage"], cov), 4)
        row["min_eligible"] = min(row["min_eligible"], f["scored"])
        row["min_with_fundamentals"] = min(
            row.get("min_with_fundamentals", 1e9), f["with_fundamentals"]
        )
    return out


def fsds_rows(tables: Any, contract: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Annual-report rows at period end for the contract's tags, read straight from the store."""
    spec = contract["fundamentals"]
    tags = sorted(
        {
            str(t)
            for k in ("net_income", "operating_cash_flow", "assets", "equity", "shares")
            for t in spec[k]
        }
    )
    pattern = str(tables.root / "parquet" / "sec_fsds" / "*.parquet").replace("'", "''")
    in_tags = ",".join("'" + t + "'" for t in tags)
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        return list(
            con.execute(
                "SELECT adsh, cik, sic, period, filed, tag, qtrs, uom, value "
                f"FROM read_parquet('{pattern}') WHERE form LIKE '10-K%' AND ddate = period "
                f"AND tag IN ({in_tags}) ORDER BY adsh, tag, qtrs, uom, value"
            ).fetchall()
        )
    finally:
        con.close()


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
    aliases = xs.load_aliases(args.aliases)
    scorer, _ = xs.build_scorer(contract, tables, loaded, aliases)
    independent_fund = None
    if scorer is not None:
        from us_stock_research.research import fundamentals as fu

        rows = fsds_rows(tables, contract)
        actual, splits, known = fu.load_market_inputs(tables, loaded.days, aliases)
        independent_fund = Fundamentals(
            contract,
            rows,
            fu.load_segments(tables),
            {
                s: {d: v for d, v in zip(loaded.days, vals, strict=True) if v is not None}
                for s, vals in actual.items()
            },
            splits,
            known,
        )
    report = {}
    for label, cut in (("base", 0.0), ("haircut", haircut)):
        engine = xs.run(
            contract,
            loaded,
            history,
            haircut=cut,
            excluded=quarantined,
            blocked=blocked_map,
            scorer=scorer,
        )["months"]
        independent = backtest(
            contract,
            as_dicts,
            history,
            haircut=cut,
            excluded=quarantined,
            blocked=blocked_list,
            fundamentals=independent_fund,
        )
        report[label] = compare(engine, independent)
        if label == "base" and any("fundamentals" in m for m in engine):
            report["fundamentals_coverage"] = coverage_by_year(engine)
    print(json.dumps(report, indent=2))
    return 0 if all(r.get("match", True) for r in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
