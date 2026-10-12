"""Live-like replay of vt_only at $10k: integer shares, fills at the next day's close.

    python tools/live_cost_replay.py <USR_STORAGE_ROOT> 2008-04-01

Not a study (no trial registered): splits the gap between the model and an account into cash buffer,
slippage, US dividend withholding and an illustrative 20% annual Chinese tax. See
docs/results/vt-only-live-costs.md.
"""

import math
import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from us_stock_research.research.leveraged_trend import load_lt_contract
from us_stock_research.research.sleeve_mix import load_mix_contract
from us_stock_research.trading.mix_intent import (
    DRIFT_LIMIT,
    holding_weights,
    model_path,
    replica_index,
)
from us_stock_research.trading.order_intent import Holdings, build_orders

ROOT = Path(__file__).resolve().parents[1]
STORE = Path(sys.argv[1])
START = date.fromisoformat(sys.argv[2])
SYMS = ["SPY", "SSO", "BIL", "TLT", "IEF", "GLD"]
close, adj = {}, {}
for s in SYMS:
    rows = duckdb.sql(
        f"select date, close, adj_close from read_parquet('{STORE}/parquet/daily/{s}.parquet') where adj_close>0"
    ).fetchall()
    close[s] = {d: float(c) for d, c, a in rows}
    adj[s] = {d: float(a) for d, c, a in rows}
days = sorted(set.intersection(*(set(adj[s]) for s in SYMS)))
mix = load_mix_contract(ROOT / "research/vt-only/study.yml")
vt = load_lt_contract(ROOT / mix["data"]["aggressive_sleeve"])
path = model_path(adj, days, START, vt["rule"], mix["rule"], list(mix["data"]["defensive_assets"]))
rep = replica_index(path, adj)


def run(capital, withholding=0.10, slip_bps=5.0, buffer=0.01):
    h = Holdings(cash_usd=capital)
    nav = {}
    pending = None
    prev = None
    od = on = sf = 0
    div_gross = 0.0
    div_tax = 0.0
    slip_cost = 0.0
    for row in path:
        d = row["day"]
        if prev is not None:
            for s, n in h.positions.items():
                ra = adj[s][d] / adj[s][prev]
                rc = close[s][d] / close[s][prev]
                div = n * close[s][prev] * (ra - rc)
                if div > 0:
                    h.cash_usd += div * (1 - withholding)
                    div_gross += div
                    div_tax += div * withholding
        if pending:
            for o in pending:
                base = close[o["symbol"]][d]
                px = base * (1 + (slip_bps if o["side"] == "BUY" else -slip_bps) / 1e4)
                n = o["shares"]
                if o["side"] == "SELL":
                    n = min(n, h.positions.get(o["symbol"], 0))
                    h.positions[o["symbol"]] -= n
                    h.cash_usd += n * px
                else:
                    if n * px > h.cash_usd:
                        sf += 1
                        n = math.floor(max(h.cash_usd, 0) / px)
                    h.positions[o["symbol"]] = h.positions.get(o["symbol"], 0) + n
                    h.cash_usd -= n * px
                slip_cost += abs(n * (px - base))
            h.positions = {s: n for s, n in h.positions.items() if n}
            pending = None
        prices = {s: close[s][d] for s in SYMS}
        v = h.cash_usd + sum(n * prices[s] for s, n in h.positions.items())
        nav[d] = v
        cur = holding_weights(h, prices)
        w = {s: x * (1 - buffer) for s, x in row["weights"].items()}
        drift = max(
            (abs(row["weights"].get(s, 0) - cur.get(s, 0)) for s in set(row["weights"]) | set(cur)),
            default=0,
        )
        if row["events"] or drift > DRIFT_LIMIT:
            orders, _ = build_orders(w, h, prices, reduce_only=False, min_trade_usd=100.0)
            if orders:
                pending = orders
                od += 1
                on += len(orders)
        prev = d
    return nav, dict(
        order_days=od,
        orders=on,
        shortfalls=sf,
        div_gross=div_gross,
        div_tax=div_tax,
        slip=slip_cost,
    )


def cagr(nav):
    ks = sorted(nav)
    yrs = (ks[-1] - ks[0]).days / 365.25
    return (nav[ks[-1]] / nav[ks[0]]) ** (1 / yrs) - 1, yrs


ref_c, yrs = cagr({d: rep[d] for d in rep if d >= path[0]["day"]})
print(
    "period",
    path[0]["day"],
    path[-1]["day"],
    "years",
    round(yrs, 1),
    "model(replica, zero cost) CAGR",
    round(ref_c, 4),
)
spy = {d: adj["SPY"][d] for d in days if path[0]["day"] <= d <= path[-1]["day"]}
print("SPY total return CAGR", round(cagr(spy)[0], 4))
for label, kw in [
    ("zero cost, no tax, no buffer", dict(withholding=0, slip_bps=0, buffer=0)),
    ("+ 1% cash buffer", dict(withholding=0, slip_bps=0)),
    ("+ 5bp slippage", dict(withholding=0)),
    ("+ 10% withholding (W-8BEN, China treaty)", dict()),
    ("30% withholding (no W-8BEN)", dict(withholding=0.30)),
    ("10bp slippage", dict(slip_bps=10.0)),
]:
    nav, info = run(10_000, **kw)
    c, y = cagr(nav)
    print(
        f"{label:45s} CAGR {c:.4f}  orders/yr {info['orders'] / y:.1f} order-days/yr {info['order_days'] / y:.1f} shortfalls/yr {info['shortfalls'] / y:.1f} div gross/yr ${info['div_gross'] / y:.0f} tax/yr ${info['div_tax'] / y:.0f} slip/yr ${info['slip'] / y:.0f}"
    )

# rough illustration: a China tax resident paying 20% on each calendar year's net gain
# (assumes gains are realised within the year -- high turnover makes that close; US 10% on
# dividends is credited, so it does not add on top; no loss carry-forward between years)
nav, info = run(10_000)
years = sorted({d.year for d in nav})
v = 10_000.0
prev_end = None
for y in years:
    ds = [d for d in nav if d.year == y]
    end = nav[max(ds)]
    start = prev_end if prev_end is not None else nav[min(ds)]
    r = end / start - 1
    v *= 1 + (r * 0.8 if r > 0 else r)
    prev_end = end
c, yy = cagr(nav)
print(
    "pre-tax CAGR", round(c, 4), "after 20% annual tax CAGR", round((v / 10_000) ** (1 / yy) - 1, 4)
)
spy_nav = {d: spy[d] for d in spy}
v = 1.0
prev_end = None
for y in sorted({d.year for d in spy_nav}):
    ds = [d for d in spy_nav if d.year == y]
    end = spy_nav[max(ds)]
    start = prev_end or spy_nav[min(ds)]
    r = end / start - 1
    prev_end = end
    v *= (
        1 + r
    )  # buy-and-hold SPY: gains deferred until sale, dividends taxed ~ (20%-10% credit) on ~1.5%
print(
    "SPY buy&hold pre-tax",
    round(cagr(spy_nav)[0], 4),
    "(dividend tax drag ~0.15-0.3%/yr; gains taxed only when sold)",
)
