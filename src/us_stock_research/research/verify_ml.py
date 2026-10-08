"""Independent check of study sp500_ml_rank (``usr-verify-ml``; verification only).

1. Features: every stock-month of the stage-1 panel is recomputed from the raw inputs with
   separate code -- plain Python lists and date-keyed dicts instead of numpy windows, normal
   equations solved by Gaussian elimination instead of ``lstsq``, a linear scan for insider
   filings, and the fundamentals of ``verify_xsec.Fundamentals`` (no code shared with
   ``fundamentals.py``). Tolerance: relative 1e-9 (absolute 1e-12 near zero).
2. Portfolios: the stage-2 ML predictions are replayed through ``cross_section.run`` (the verified
   engine) and its monthly strategy / equal-weight returns compared with stage 2's.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.research import cross_section as xs

STALE = 5


def close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(1e-12, 1e-9 * max(abs(a), abs(b)))


def price_back(series: list[float | None], i: int) -> float | None:
    for j in range(i, max(i - STALE, 0) - 1, -1):
        if j >= 0 and series[j] is not None:
            return series[j]
    return None


def daily_returns(series: list[float | None], first: int, last: int) -> dict[int, float]:
    out = {}
    for i in range(max(first, 1), last + 1):
        a, b = series[i - 1], series[i]
        if a is not None and b is not None:
            out[i] = b / a - 1
    return out


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def regress(ys: list[float], xs_: list[list[float]]) -> list[float]:
    k = len(xs_[0]) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for y, x in zip(ys, xs_, strict=True):
        row = [1.0, *x]
        for i in range(k):
            xty[i] += row[i] * y
            for j in range(k):
                xtx[i][j] += row[i] * row[j]
    return solve(xtx, xty)


class Recompute:
    def __init__(
        self,
        days: list[date],
        series: dict[str, list[float | None]],
        dollar: dict[str, list[float | None]],
        macro: dict[str, list[tuple[date, float]]],
        insider: dict[str, dict[int, list[tuple[date, int]]]],
        segments: dict[str, Any],
        fundamentals: Any,
    ) -> None:
        self.days, self.series, self.dollar = days, series, dollar
        self.macro = {k: ([d for d, _ in v], [x for _, x in v]) for k, v in macro.items()}
        self.insider, self.segments, self.fund = insider, segments, fundamentals

    def macro_at(self, name: str, i: int) -> float | None:
        ds, vs = self.macro[name]
        k = bisect.bisect_right(ds, self.days[i]) - 1
        return vs[k] if k >= 0 else None

    def features(self, s: str, t: int) -> dict[str, float | None]:
        p, spy = self.series[s], self.series["SPY"]
        out: dict[str, float | None] = {}

        def ratio(a: float | None, b: float | None) -> float | None:
            return a / b - 1 if a and b else None

        px = {k: price_back(p, t - k) if t - k >= 0 else None for k in (0, 5, 21, 126, 252)}
        out["mom_12_1"] = ratio(px[21], px[252])
        out["mom_6_1"] = ratio(px[21], px[126])
        out["rev_1m"] = ratio(px[0], px[21])
        out["rev_1w"] = ratio(px[0], px[5])
        r60 = daily_returns(p, t - 59, t)
        out["vol_60"] = statistics.stdev(r60.values()) if len(r60) >= 48 else None
        r21 = daily_returns(p, t - 20, t)
        out["max_ret_21"] = max(r21.values()) if len(r21) >= 17 else None
        r252, m252 = daily_returns(p, t - 251, t), daily_returns(spy, t - 251, t)
        both = [i for i in r252 if i in m252]
        if len(both) >= 200:
            xs_, ys = [m252[i] for i in both], [r252[i] for i in both]
            out["beta_252"] = statistics.covariance(xs_, ys) / statistics.variance(xs_)
        else:
            out["beta_252"] = None
        m60 = daily_returns(spy, t - 59, t)
        both = [i for i in r60 if i in m60]
        if len(both) >= 48:
            xs_, ys = [m60[i] for i in both], [r60[i] for i in both]
            b = statistics.covariance(xs_, ys) / statistics.variance(xs_)
            a = statistics.fmean(ys) - b * statistics.fmean(xs_)
            out["idio_vol_60"] = statistics.stdev(
                [y - a - b * x for x, y in zip(xs_, ys, strict=True)]
            )
        else:
            out["idio_vol_60"] = None
        last200 = [v for v in p[max(t - 199, 0) : t + 1] if v is not None]
        out["dist_sma_200"] = (
            px[0] / statistics.fmean(last200) - 1 if px[0] and len(last200) >= 160 else None
        )
        last252 = [v for v in p[max(t - 251, 0) : t + 1] if v is not None]
        out["dist_high_252"] = px[0] / max(last252) - 1 if px[0] and len(last252) >= 200 else None
        dv = self.dollar.get(s)
        out["dollar_vol_60"] = out["volume_surge"] = out["amihud_60"] = None
        if dv is not None:

            def good(lo: int) -> list[float]:
                return [v for v in dv[max(lo, 0) : t + 1] if v is not None and v > 0]

            g60, g21, g252 = good(t - 59), good(t - 20), good(t - 251)
            if len(g60) >= 48:
                out["dollar_vol_60"] = math.log(statistics.fmean(g60))
            if len(g21) >= 17 and len(g252) >= 200:
                out["volume_surge"] = statistics.fmean(g21) / statistics.fmean(g252)
            pairs = [abs(r60[i]) / dv[i] for i in r60 if dv[i] is not None and dv[i] > 0]  # type: ignore[operator]
            if len(pairs) >= 48:
                out["amihud_60"] = statistics.fmean(pairs) * 1e9
        day = self.days[t]
        cik = next(
            (c for a, b, c in self.segments.get(s, []) if a <= day and (b is None or day < b)), None
        )
        for k in (
            "insider_buyers_182",
            "insider_sellers_182",
            "log_market_value",
            "roa",
            "cfoa",
            "accruals",
            "earnings_yield",
            "book_to_market",
            "cash_flow_yield",
        ):
            out[k] = None
        sic2 = None
        if cik is not None:
            lo = day - timedelta(days=182)
            for key, side in (("insider_buyers_182", "buy"), ("insider_sellers_182", "sell")):
                owners = {o for d, o in self.insider[side].get(cik, []) if lo < d <= day}
                out[key] = float(len(owners))
            facts = self.fund.facts(cik, day)
            if facts is not None:
                mv = self.fund.mcap(s, self.days, t, facts)
                a_, e_ = facts["assets"], facts["equity"]
                ni, cf = facts["net_income"], facts["operating_cash_flow"]
                if a_ is not None and a_ > 0:
                    out["roa"] = ni / a_ if ni is not None else None
                    out["cfoa"] = cf / a_ if cf is not None else None
                    if ni is not None and cf is not None:
                        out["accruals"] = (ni - cf) / a_
                if mv is not None and mv > 0:
                    out["log_market_value"] = math.log(mv)
                    out["earnings_yield"] = ni / mv if ni is not None else None
                    out["cash_flow_yield"] = cf / mv if cf is not None else None
                    out["book_to_market"] = e_ / mv if e_ is not None and e_ > 0 else None
                sic2 = facts["sic"] // 100 if facts.get("sic") else None
        out["_sic2"] = sic2
        # macro betas, window ending the day before the signal
        rows_y, rows_x = [], []
        for i in range(max(t - 252, 1), t):
            vals = [p[i], p[i - 1], spy[i], spy[i - 1]]
            mac = [
                (self.macro_at(n, i), self.macro_at(n, i - 1))
                for n in ("DGS10", "DTWEXBGS", "DCOILWTICO", "BAA10Y")
            ]
            if any(v is None for v in vals) or any(a is None or b is None for a, b in mac):
                continue
            (r1, r0), (d1, d0), (o1, o0), (c1, c0) = mac
            if o1 <= 0 or o0 <= 0:  # type: ignore[operator]
                continue
            rows_y.append(p[i] / p[i - 1] - 1)  # type: ignore[operator]
            rows_x.append([spy[i] / spy[i - 1] - 1, r1 - r0, d1 / d0 - 1, o1 / o0 - 1, c1 - c0])  # type: ignore[operator]
        if len(rows_y) >= 200:
            coef = regress(rows_y, rows_x)
            out["beta_rate"], out["beta_dollar"], out["beta_oil"], out["beta_credit"] = coef[2:]
        else:
            out["beta_rate"] = out["beta_dollar"] = out["beta_oil"] = out["beta_credit"] = None
        return out


def check_features(rc: Recompute, panel: Path, index: dict[date, int]) -> dict[str, Any]:
    by_month: dict[str, list[dict[str, Any]]] = {}
    with gzip.open(panel, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r["type"] == "stock":
                by_month.setdefault(r["date"], []).append(r)
    checked = mismatched = 0
    examples: list[str] = []
    for d, rows in sorted(by_month.items()):
        t = index[date.fromisoformat(d)]
        mine = {r["symbol"]: rc.features(r["symbol"], t) for r in rows}
        groups: dict[int, list[float]] = {}
        for f in mine.values():
            if f["_sic2"] is not None and f["mom_12_1"] is not None:
                groups.setdefault(f["_sic2"], []).append(f["mom_12_1"])  # type: ignore[arg-type]
        for f in mine.values():
            g = groups.get(f["_sic2"]) if f["_sic2"] is not None else None  # type: ignore[arg-type]
            f["industry_mom"] = statistics.fmean(g) if g and len(g) >= 3 else None
        for r in rows:
            for k, v in r["f"].items():
                checked += 1
                if not close(v, mine[r["symbol"]][k]):
                    mismatched += 1
                    if len(examples) < 10:
                        examples.append(
                            f"{d} {r['symbol']} {k}: panel {v}, check {mine[r['symbol']][k]}"
                        )
    return {"values_checked": checked, "mismatched": mismatched, "examples": examples}


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one check flow
    from us_stock_research.config import load_settings
    from us_stock_research.quality.ohlcv import load_exceptions
    from us_stock_research.research import fundamentals as fu
    from us_stock_research.research.ml_features import load_dollar_volume
    from us_stock_research.research.verify_xsec import Fundamentals
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True, help="usr-ml-rank artifact")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    args = parser.parse_args(argv)
    c = xs.load_xs_contract(args.contract)
    tables = TableStore.from_settings(load_settings())
    start = date.fromisoformat(str(c["ml"]["train_start"]))
    loaded = xs.load_prices(
        tables, start - timedelta(days=500), c["universe"]["end"] + timedelta(days=45)
    )
    aliases = xs.load_aliases(args.aliases)
    xs.apply_aliases(loaded, aliases)
    days = loaded.days
    dv_np = load_dollar_volume(tables, days)
    dollar: dict[str, list[float | None]] = {
        s: [None if math.isnan(v) else float(v) for v in arr] for s, arr in dv_np.items()
    }
    for key in sorted(aliases):
        old, _, cut = key.partition("@")
        new = aliases[key]
        if new in dollar and (old not in dollar or cut):
            if not cut:
                dollar[old] = dollar[new]
            else:
                until = date.fromisoformat(cut)
                own = dollar.get(old, [None] * len(days))
                dollar[old] = [
                    n if d < until else o for d, n, o in zip(days, dollar[new], own, strict=True)
                ]
    macro = {}
    for name in ("DGS10", "DTWEXBGS", "DCOILWTICO", "BAA10Y"):
        macro[name] = sorted(
            (d, float(v))
            for d, v in tables.read("macro", name, "date, value")
            if v is not None and v == v
        )
    pattern = str(tables.root / "parquet" / "sec_insider" / "*.parquet").replace("'", "''")
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        raw = con.execute(
            "SELECT issuer_cik, filing_date, owner_cik, trans_code, acquired_disposed, "
            "relationship "
            f"FROM read_parquet('{pattern}') WHERE trans_code IN ('P', 'S') AND shares > 0 "
            "AND price > 0 AND issuer_cik IS NOT NULL AND owner_cik IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    insider: dict[str, dict[int, list[tuple[date, int]]]] = {"buy": {}, "sell": {}}
    for issuer, filed, owner, code, ad, rel in raw:
        if not ("Director" in (rel or "") or "Officer" in (rel or "")):
            continue
        if (code, ad) == ("P", "A"):
            insider["buy"].setdefault(int(issuer), []).append((filed, int(owner)))
        elif (code, ad) == ("S", "D"):
            insider["sell"].setdefault(int(issuer), []).append((filed, int(owner)))
    rows = fu.annual_rows(tables, fu.tag_lists(c["fundamentals"]), str(c["fundamentals"]["source"]))
    actual, splits, known = fu.load_market_inputs(tables, days, aliases)
    fund = Fundamentals(
        {**c, "signal": {"quality": [], "value": []}},
        rows,
        fu.load_segments(tables),
        {
            s: {d: v for d, v in zip(days, vals, strict=True) if v is not None}
            for s, vals in actual.items()
        },
        splits,
        known,
    )
    rc = Recompute(days, loaded.series, dollar, macro, insider, fu.load_segments(tables), fund)
    feats = check_features(rc, args.panel, {d: i for i, d in enumerate(days)})
    # portfolios: replay the ML predictions through the engine
    result = json.loads(args.result.read_text())
    preds = json.loads(args.result.with_name("predictions.json").read_text())

    def scorer(candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        p = preds.get(days[t].isoformat(), {})
        return {s: p[s] for s in candidates if s in p}, {"candidates": len(candidates)}

    history = list(tables.read("meta", "sp500_history", "symbol, start_date, end_date"))
    excluded = set(load_exceptions(args.exceptions)[1])
    engine = xs.run(
        c, loaded, history, excluded=excluded, blocked=xs.load_blocked(tables), scorer=scorer
    )["months"]
    mine = {m["date"]: m for m in result["months"]}
    diffs = [
        max(
            abs(m["strategy"] - mine[m["date"]]["strategy"]),
            abs(m["equal_weight"] - mine[m["date"]]["equal_weight"]),
        )
        for m in engine
        if m["date"] in mine
    ]
    eligible_same = all(m["eligible"] == len(preds.get(m["date"], {})) for m in engine)
    port = {
        "months_engine": len(engine),
        "months_stage2": len(mine),
        "max_abs_diff": max(diffs) if diffs else None,
        "eligible_counts_match": eligible_same,
    }
    match = (
        feats["mismatched"] == 0
        and len(engine) == len(mine)
        and eligible_same
        and diffs
        and max(diffs) < 1e-12
    )
    report = {"features": feats, "portfolio_replay": port, "match": bool(match)}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if match else 4


if __name__ == "__main__":
    sys.exit(main())
