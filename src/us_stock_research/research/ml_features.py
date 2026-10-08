"""Stage 1 of study sp500_ml_rank (``usr-ml-panel``): the point-in-time feature panel.

For every month end from ``ml.train_start`` to ``universe.end`` and every eligible S&P 500 member
(same membership, price, quarantine and identity rules as ``cross_section.run``) it writes the raw
stock-level features, the month's market-state inputs, and the next holding period's return
(base and with the delisting haircut) -- everything stage 2 (``usr-ml-rank``, model and
portfolios) needs, so stage 2 runs without the price store. Rows are JSON lines, gzip.

Feature definitions are the contract's (``research/sp500-ml-rank/study.yml``); prices may be up to
``MAX_STALE_DAYS`` stale as in the engine; macro inputs use values up to the day *before* the
signal.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from us_stock_research.research import cross_section as xs
from us_stock_research.research import fundamentals as fu

TECH = (
    "mom_12_1",
    "mom_6_1",
    "rev_1m",
    "rev_1w",
    "vol_60",
    "beta_252",
    "idio_vol_60",
    "max_ret_21",
    "dist_sma_200",
    "dist_high_252",
    "industry_mom",
)
FLOWS = ("dollar_vol_60", "volume_surge", "amihud_60", "insider_buyers_182", "insider_sellers_182")
FUND = (
    "log_market_value",
    "roa",
    "cfoa",
    "accruals",
    "earnings_yield",
    "book_to_market",
    "cash_flow_yield",
)
MACRO_BETAS = ("beta_rate", "beta_dollar", "beta_oil", "beta_credit")
MARKET_STATE = ("vix", "curve_10y2y", "credit_spread", "rate_chg_63", "market_mom_252")
STOCK_FEATURES = TECH + FLOWS + FUND + MACRO_BETAS
REQUIRED = ("mom_12_1", "vol_60", "dollar_vol_60")
MACRO_SERIES = {
    "DGS10": "DGS10",
    "DTWEXBGS": "DTWEXBGS",
    "DCOILWTICO": "DCOILWTICO",
    "BAA10Y": "BAA10Y",
    "VIXCLS": "VIXCLS",
    "T10Y2Y": "T10Y2Y",
}
STALE = xs.MAX_STALE_DAYS


def as_array(series: list[float | None]) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in series], dtype=float)


def last_valid(a: np.ndarray, i: int, stale: int = STALE) -> float | None:
    for j in range(i, max(i - stale, 0) - 1, -1):
        if not math.isnan(a[j]):
            return float(a[j])
    return None


def window_returns(p: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Daily returns r_i = p[i]/p[i-1]-1 for i in [lo, hi] (NaN where either price is missing)."""
    lo = max(lo, 1)
    if hi < lo:
        return np.array([])
    return p[lo : hi + 1] / p[lo - 1 : hi] - 1


def ols_coefs(y: np.ndarray, x: np.ndarray) -> np.ndarray | None:
    """Coefficients of y on [1, x] (rows with any NaN dropped); None with < 200 rows."""
    ok = ~np.isnan(y) & ~np.isnan(x).any(axis=1)
    if ok.sum() < 200:
        return None
    design = np.column_stack([np.ones(ok.sum()), x[ok]])
    coef, *_ = np.linalg.lstsq(design, y[ok], rcond=None)
    return coef


def ffill_on(days: list[date], points: dict[date, float]) -> np.ndarray:
    keys = sorted(points)
    out = np.full(len(days), np.nan)
    j, last = 0, math.nan
    for i, d in enumerate(days):
        while j < len(keys) and keys[j] <= d:
            last = points[keys[j]]
            j += 1
        out[i] = last
    return out


@dataclass
class Inputs:
    days: list[date]
    adj: dict[str, np.ndarray]
    dollar: dict[str, np.ndarray]
    macro: dict[str, np.ndarray]
    buys: dict[int, list[tuple[date, int, float]]]
    sells: dict[int, list[tuple[date, int, float]]]
    segments: dict[str, list[tuple[date, date | None, int | None]]]
    fundamentals: Any  # fu.QualityValue: reports, tags, market value


def stock_features(inp: Inputs, s: str, t: int) -> dict[str, float | None]:
    """Stock-level features except industry_mom (needs the cross-section)."""
    p = inp.adj[s]
    spy = inp.adj["SPY"]
    f: dict[str, float | None] = dict.fromkeys(STOCK_FEATURES)

    def px(k: int) -> float | None:
        return last_valid(p, t - k) if t - k >= 0 else None

    p0, p5, p21, p126, p252 = px(0), px(5), px(21), px(126), px(252)
    if p21 and p252:
        f["mom_12_1"] = p21 / p252 - 1
    if p21 and p126:
        f["mom_6_1"] = p21 / p126 - 1
    if p0 and p21:
        f["rev_1m"] = p0 / p21 - 1
    if p0 and p5:
        f["rev_1w"] = p0 / p5 - 1
    r60 = window_returns(p, t - 59, t)
    v60 = r60[~np.isnan(r60)]
    if len(v60) >= 48:
        f["vol_60"] = float(np.std(v60, ddof=1))
    r21 = window_returns(p, t - 20, t)
    v21 = r21[~np.isnan(r21)]
    if len(v21) >= 17:
        f["max_ret_21"] = float(v21.max())
    r252 = window_returns(p, t - 251, t)
    m252 = window_returns(spy, t - 251, t)
    ok = ~np.isnan(r252) & ~np.isnan(m252)
    if ok.sum() >= 200:
        x, y = m252[ok], r252[ok]
        f["beta_252"] = float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
    m60 = window_returns(spy, t - 59, t)
    ok = ~np.isnan(r60) & ~np.isnan(m60)
    if ok.sum() >= 48:
        x, y = m60[ok], r60[ok]
        b = np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1)
        resid = y - (y.mean() - b * x.mean()) - b * x
        f["idio_vol_60"] = float(np.std(resid, ddof=1))
    closes200 = p[max(t - 199, 0) : t + 1]
    c200 = closes200[~np.isnan(closes200)]
    if p0 and len(c200) >= 160:
        f["dist_sma_200"] = p0 / float(c200.mean()) - 1
    closes252 = p[max(t - 251, 0) : t + 1]
    c252 = closes252[~np.isnan(closes252)]
    if p0 and len(c252) >= 200:
        f["dist_high_252"] = p0 / float(c252.max()) - 1
    # flows
    dv = inp.dollar.get(s)
    if dv is not None:
        w60 = dv[max(t - 59, 0) : t + 1]
        g60 = w60[~np.isnan(w60) & (w60 > 0)]
        if len(g60) >= 48:
            f["dollar_vol_60"] = math.log(float(g60.mean()))
        w21 = dv[max(t - 20, 0) : t + 1]
        g21 = w21[~np.isnan(w21) & (w21 > 0)]
        w252 = dv[max(t - 251, 0) : t + 1]
        g252 = w252[~np.isnan(w252) & (w252 > 0)]
        if len(g21) >= 17 and len(g252) >= 200:
            f["volume_surge"] = float(g21.mean() / g252.mean())
        d60 = dv[max(t - 59, 1) : t + 1]
        ok = ~np.isnan(r60[-len(d60) :]) & ~np.isnan(d60) & (d60 > 0) if len(d60) else None
        if ok is not None and ok.sum() >= 48:
            f["amihud_60"] = float(np.mean(np.abs(r60[-len(d60) :][ok]) / d60[ok]) * 1e9)
    day = inp.days[t]
    cik = fu.cik_on(inp.segments.get(s, []), day)
    if cik is not None:
        f["insider_buyers_182"] = float(distinct_owners(inp.buys.get(cik, []), day, 182))
        f["insider_sellers_182"] = float(distinct_owners(inp.sells.get(cik, []), day, 182))
        qv = inp.fundamentals
        items = fu.resolve(qv.reports.get(cik, []), day, qv.tags, qv.max_age)
        if items is not None:
            mv = qv.market_value(s, t, items)
            m = fu.metrics(items, mv)
            f["roa"], f["cfoa"] = m["roa"], m["cfoa"]
            f["earnings_yield"], f["book_to_market"] = m["earnings_yield"], m["book_to_market"]
            f["cash_flow_yield"] = m["cash_flow_yield"]
            if mv is not None and mv > 0:
                f["log_market_value"] = math.log(mv)
            ni, cfo, assets = (
                items.get("net_income"),
                items.get("operating_cash_flow"),
                items.get("assets"),
            )
            if ni is not None and cfo is not None and assets is not None and assets > 0:
                f["accruals"] = (float(ni) - float(cfo)) / float(assets)
            f["_sic2"] = float(items["sic"] // 100) if items.get("sic") else None
    # macro sensitivities: window ends the day before the signal
    lo, hi = t - 252, t - 1
    y = window_returns(p, lo, hi)
    if len(y):
        mk = inp.macro
        x = np.column_stack(
            [
                window_returns(spy, lo, hi),
                np.diff(mk["DGS10"][max(lo, 1) - 1 : hi + 1]),
                window_returns(mk["DTWEXBGS"], lo, hi),
                oil_returns(mk["DCOILWTICO"], lo, hi),
                np.diff(mk["BAA10Y"][max(lo, 1) - 1 : hi + 1]),
            ]
        )
        coef = ols_coefs(y, x)
        if coef is not None:
            f["beta_rate"], f["beta_dollar"] = float(coef[2]), float(coef[3])
            f["beta_oil"], f["beta_credit"] = float(coef[4]), float(coef[5])
    return f


def oil_returns(o: np.ndarray, lo: int, hi: int) -> np.ndarray:
    r = window_returns(o, lo, hi)
    lo = max(lo, 1)
    bad = (o[lo : hi + 1] <= 0) | (o[lo - 1 : hi] <= 0)
    r[bad] = np.nan
    return r


def distinct_owners(rows: list[tuple[date, int, float]], day: date, window: int) -> int:
    """Distinct owners with a filing dated in (day - window, day]; ``rows`` sorted by date."""
    lo = bisect.bisect_right(rows, (day - timedelta(days=window), 1 << 62, math.inf))
    hi = bisect.bisect_right(rows, (day, 1 << 62, math.inf))
    return len({owner for _, owner, _ in rows[lo:hi]})


def market_state(inp: Inputs, t: int) -> dict[str, float | None]:
    mk, prev = inp.macro, t - 1

    def val(name: str, i: int) -> float | None:
        v = mk[name][i] if i >= 0 else math.nan
        return None if math.isnan(v) else float(v)

    rate_now, rate_then = val("DGS10", prev), val("DGS10", prev - 63)
    spy0, spy252 = last_valid(inp.adj["SPY"], t), last_valid(inp.adj["SPY"], t - 252)
    return {
        "vix": val("VIXCLS", prev),
        "curve_10y2y": val("T10Y2Y", prev),
        "credit_spread": val("BAA10Y", prev),
        "rate_chg_63": rate_now - rate_then
        if rate_now is not None and rate_then is not None
        else None,
        "market_mom_252": spy0 / spy252 - 1 if spy0 and spy252 else None,
    }


def add_industry(rows: dict[str, dict[str, float | None]]) -> None:
    groups: dict[float, list[float]] = {}
    for f in rows.values():
        if f.get("_sic2") is not None and f.get("mom_12_1") is not None:
            groups.setdefault(f["_sic2"], []).append(f["mom_12_1"])  # type: ignore[arg-type]
    for f in rows.values():
        g = groups.get(f.get("_sic2"))  # type: ignore[arg-type]
        f["industry_mom"] = sum(g) / len(g) if g and len(g) >= 3 else None


def eligible_rows(inp: Inputs, candidates: list[str], t: int) -> dict[str, dict[str, float | None]]:
    rows = {}
    for s in candidates:
        if s not in inp.adj:
            continue
        f = stock_features(inp, s, t)
        if all(f.get(k) is not None for k in REQUIRED):
            rows[s] = f
    add_industry(rows)
    return rows


def candidates_on(
    prices: xs.Prices,
    history: list[xs.Interval],
    t: int,
    need: int,
    excluded: set[str],
    blocked: xs.Blocked,
) -> tuple[list[str], int, int]:
    """Same rule as cross_section.run: member, not quarantined/blocked, priced, long enough."""
    day = prices.days[t]
    members = xs.members_on(history, day)
    priced = {
        s
        for s in members
        if s not in excluded and not xs.is_blocked(blocked, s, day) and prices.at(s, t, STALE)
    }
    cands = [s for s in sorted(priced) if prices.at(s, t - need, STALE)]
    return cands, len(members), len(priced)


def load_dollar_volume(tables: Any, days: list[date]) -> dict[str, np.ndarray]:
    index = {d: i for i, d in enumerate(days)}
    out: dict[str, np.ndarray] = {}
    for kind in ("daily_delisted", "daily"):
        if not tables.keys(kind):
            continue
        pattern = str(tables.root / "parquet" / kind / "*.parquet").replace("'", "''")
        con = tables._duckdb().connect()  # noqa: SLF001
        try:
            rows = con.execute(
                "SELECT regexp_extract(filename, '([^/]+)\\.parquet$', 1), date, close * volume "
                f"FROM read_parquet('{pattern}', filename=true, union_by_name=true) "
                f"WHERE date BETWEEN '{days[0]}' AND '{days[-1]}' AND close > 0 AND volume > 0"
            ).fetchall()
        finally:
            con.close()
        fresh: dict[str, np.ndarray] = {}
        for sym, d, v in rows:
            i = index.get(d)
            if i is None:
                continue
            fresh.setdefault(sym, np.full(len(days), np.nan))[i] = float(v)
        out.update(fresh)
    return out


def load_sells(tables: Any) -> tuple[dict[int, list[tuple[date, int, float]]], str]:
    pattern = str(tables.root / "parquet" / "sec_insider" / "*.parquet").replace("'", "''")
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        rows = con.execute(
            "SELECT issuer_cik, filing_date, owner_cik, shares * price, relationship "
            f"FROM read_parquet('{pattern}') WHERE trans_code = 'S' AND acquired_disposed = 'D' "
            "AND shares > 0 AND price > 0 AND issuer_cik IS NOT NULL AND owner_cik IS NOT NULL "
            "ORDER BY issuer_cik, filing_date, owner_cik, accession, shares, price"
        ).fetchall()
    finally:
        con.close()
    from us_stock_research.research.insider import is_insider

    out: dict[int, list[tuple[date, int, float]]] = {}
    digest = hashlib.sha256()
    for issuer, filed, owner, dollars, rel in rows:
        if not is_insider(rel):
            continue
        out.setdefault(int(issuer), []).append((filed, int(owner), float(dollars)))
        digest.update(repr((issuer, filed, owner, dollars)).encode())
    return out, "insider-sells:sha256:" + digest.hexdigest()


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one pipeline
    from us_stock_research.config import load_settings
    from us_stock_research.quality.ohlcv import load_exceptions
    from us_stock_research.research.insider import load_buys
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    args = parser.parse_args(argv)
    c = xs.load_xs_contract(args.contract)
    ml, uni = c["ml"], c["universe"]
    start = (
        ml["train_start"]
        if isinstance(ml["train_start"], date)
        else date.fromisoformat(str(ml["train_start"]))
    )
    tables = TableStore.from_settings(load_settings())
    history = list(tables.read("meta", "sp500_history", "symbol, start_date, end_date"))
    prices = xs.load_prices(tables, start - timedelta(days=500), uni["end"] + timedelta(days=45))
    aliases = xs.load_aliases(args.aliases)
    xs.apply_aliases(prices, aliases)
    excluded = set(load_exceptions(args.exceptions)[1])
    blocked = xs.load_blocked(tables)
    days = prices.days
    dollar = load_dollar_volume(tables, days)
    for key in sorted(aliases):  # renamed tickers borrow volumes like prices
        old, _, cut = key.partition("@")
        new = aliases[key]
        if new in dollar and (old not in dollar or cut):
            if not cut:
                dollar[old] = dollar[new]
            else:
                until = date.fromisoformat(cut)
                own = dollar.get(old, np.full(len(days), np.nan))
                dollar[old] = np.where([d < until for d in days], dollar[new], own)
    macro = {}
    for key in MACRO_SERIES:
        rows = tables.read("macro", key, "date, value")
        macro[key] = ffill_on(days, {d: float(v) for d, v in rows if v is not None and v == v})
    buys, buys_id = load_buys(tables)
    sells, sells_id = load_sells(tables)
    spec = c["fundamentals"]
    reports, fund_id = fu.load_reports(tables, fu.tag_lists(spec), str(spec["source"]))
    actual, splits, known = fu.load_market_inputs(tables, days, aliases)
    qv = fu.QualityValue(c, days, reports, fu.load_segments(tables), actual, splits, known)
    inp = Inputs(
        days,
        {s: as_array(v) for s, v in prices.series.items()},
        dollar,
        macro,
        buys,
        sells,
        fu.load_segments(tables),
        qv,
    )
    need, lag = int(uni["min_history_days"]), int(c["execution_lag_days"])
    haircut = float(c.get("delisting", {}).get("haircut_sensitivity", 0.0))
    ends = xs.month_end_indices(days, start, uni["end"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    with gzip.open(args.output, "wt") as out:
        for t, t_next in zip(ends, ends[1:], strict=False):
            cands, members, priced = candidates_on(prices, history, t, need, excluded, blocked)
            rows = eligible_rows(inp, cands, t)
            entry, exit_ = t + lag, min(t_next + lag, len(days) - 1)
            b0 = prices.at("SPY", entry, STALE)
            b1 = prices.at("SPY", exit_, STALE)
            head = {
                "type": "month",
                "date": days[t].isoformat(),
                "t": t,
                "exit": exit_,
                "exit_date": days[exit_].isoformat(),
                "members": members,
                "priced": priced,
                "candidates": len(cands),
                "eligible": len(rows),
                "with_fundamentals": sum(
                    any(r.get(k) is not None for k in FUND) for r in rows.values()
                ),
                "spy": b1[1] / b0[1] - 1 if b0 and b1 else None,
                "market_state": market_state(inp, t),
            }
            out.write(json.dumps(head) + "\n")
            for s, f in sorted(rows.items()):
                r, early = xs.holding_return(prices, s, entry, exit_, 0.0)
                rh, _ = xs.holding_return(prices, s, entry, exit_, haircut)
                row = {
                    "type": "stock",
                    "date": head["date"],
                    "symbol": s,
                    "ret": r,
                    "ret_haircut": rh,
                    "early": early,
                    "f": {k: f.get(k) for k in STOCK_FEATURES},
                }
                out.write(json.dumps(row) + "\n")
                n_rows += 1
    prices_id = xs.data_fingerprint(tables, ["daily", "daily_delisted", "meta", "splits", "macro"])
    fp = (
        "ml-panel-v1:sha256:"
        + hashlib.sha256(f"{prices_id}|{fund_id}|{buys_id}|{sells_id}".encode()).hexdigest()
    )
    args.output.with_suffix(".meta.json").write_text(
        json.dumps({"snapshot_id": fp, "rows": n_rows, "months": len(ends) - 1}, indent=2) + "\n"
    )
    print(json.dumps({"rows": n_rows, "months": len(ends) - 1, "snapshot_id": fp}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
