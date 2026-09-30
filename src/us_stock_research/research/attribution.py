"""Fama-French factor attribution of a backtest (read-only, no pandas/numpy needed).

Regresses the strategy's monthly excess return on market, size, value, profitability,
investment (FF5) and momentum:

    r - rf = alpha + b1 MKT + b2 SMB + b3 HML + b4 RMW + b5 CMA + b6 MOM + e

with Newey-West (lag 3) standard errors, because overlapping signals make monthly residuals
autocorrelated. A "defensive" strategy whose alpha is insignificant and whose return is explained
by a low market beta plus momentum has no edge beyond known premia; that is the question this
answers. The benchmark is run through the same regression as a sanity check (beta near 1,
alpha near 0).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

from us_stock_research.config import load_settings
from us_stock_research.storage import open_store
from us_stock_research.tables import TableStore

FACTORS = ("mkt_rf", "smb", "hml", "rmw", "cma", "mom")
NW_LAGS = 3


def monthly_returns(dates: list[str], values: list[float]) -> dict[tuple[int, int], float]:
    """Month-over-month returns from a daily equity curve (last value of each month)."""
    month_end: dict[tuple[int, int], float] = {}
    for d, v in zip(dates, values, strict=True):
        day = date.fromisoformat(d[:10])
        month_end[(day.year, day.month)] = v
    keys = sorted(month_end)
    return {k: month_end[k] / month_end[p] - 1 for p, k in zip(keys, keys[1:], strict=False)}


def _inverse(m: list[list[float]]) -> list[list[float]]:
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-14:
            raise ValueError("singular design matrix")
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [x / p for x in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [x - f * y for x, y in zip(a[r], a[col], strict=True)]
    return [row[n:] for row in a]


def ols_newey_west(y: list[float], x: list[list[float]], lags: int = NW_LAGS) -> dict[str, Any]:
    """OLS with an intercept already in ``x``; returns coefficients, NW t-stats and R^2."""
    n, k = len(y), len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yi for r, yi in zip(x, y, strict=True)) for i in range(k)]
    inv = _inverse(xtx)
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    resid = [
        yi - sum(b * v for b, v in zip(beta, r, strict=True)) for r, yi in zip(x, y, strict=True)
    ]
    s = [[0.0] * k for _ in range(k)]
    for lag in range(lags + 1):
        w = 1.0 if lag == 0 else 1 - lag / (lags + 1)
        for t in range(lag, n):
            e = resid[t] * resid[t - lag]
            for i in range(k):
                for j in range(k):
                    term = x[t][i] * x[t - lag][j]
                    if lag:
                        term += x[t - lag][i] * x[t][j]
                    s[i][j] += w * e * term
    cov = [
        [sum(inv[i][a] * s[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)]
        for i in range(k)
    ]
    mean_y = sum(y) / n
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    ss_res = sum(e * e for e in resid)
    t_stats = [
        b / math.sqrt(cov[i][i]) if cov[i][i] > 0 else float("nan") for i, b in enumerate(beta)
    ]
    return {
        "coef": beta,
        "t": t_stats,
        "r2": 1 - ss_res / ss_tot if ss_tot else float("nan"),
        "resid_vol_annual": math.sqrt(ss_res / (n - k) * 12),
        "n": n,
    }


def attribute(
    returns: dict[tuple[int, int], float],
    factors: dict[tuple[int, int], dict[str, float]],
    names: tuple[str, ...] = FACTORS,
) -> dict[str, Any]:
    months = sorted(m for m in returns if m in factors and all(n in factors[m] for n in names))
    if len(months) < 36:
        raise ValueError(f"only {len(months)} overlapping months; need at least 36")
    y = [returns[m] - factors[m]["rf"] for m in months]
    x = [[1.0] + [factors[m][f] for f in names] for m in months]
    fit = ols_newey_west(y, x)
    names = ("alpha", *names)
    return {
        "months": len(months),
        "first": f"{months[0][0]}-{months[0][1]:02d}",
        "last": f"{months[-1][0]}-{months[-1][1]:02d}",
        "alpha_annual": fit["coef"][0] * 12,
        "alpha_t": fit["t"][0],
        "loadings": {n: round(c, 4) for n, c in zip(names[1:], fit["coef"][1:], strict=True)},
        "t_stats": {n: round(t, 2) for n, t in zip(names, fit["t"], strict=True)},
        "r2": fit["r2"],
        "resid_vol_annual": fit["resid_vol_annual"],
    }


def load_factors(tables: TableStore) -> dict[tuple[int, int], dict[str, float]]:
    out: dict[tuple[int, int], dict[str, float]] = {}
    for d, *vals in tables.read("factors", "ff5_monthly", "date, mkt_rf, smb, hml, rmw, cma, rf"):
        out[(d.year, d.month)] = dict(
            zip(("mkt_rf", "smb", "hml", "rmw", "cma", "rf"), vals, strict=True)
        )
    for d, mom in tables.read("factors", "mom_monthly", "date, mom"):
        if (d.year, d.month) in out and mom == mom:  # skip NaN
            out[(d.year, d.month)]["mom"] = mom
    return {k: v for k, v in out.items() if "mom" in v}


LABELS = {
    "mkt_rf": "市场",
    "smb": "规模",
    "hml": "价值",
    "rmw": "盈利",
    "cma": "投资",
    "mom": "动量",
    "ief": "债券(IEF)",
    "gld": "黄金(GLD)",
}


def render(results: dict[str, dict[str, Any]]) -> str:
    columns: list[str] = []
    for r in results.values():
        columns += [c for c in r["loadings"] if c not in columns]
    header = [
        "序列",
        "月数",
        "年化 alpha",
        "alpha t 值",
        *(LABELS.get(c, c) for c in columns),
        "R²",
    ]
    lines = ["| " + " | ".join(header) + " |", "|---|" + "---:|" * (len(header) - 1)]
    for name, r in results.items():
        ld, ts = r["loadings"], r["t_stats"]
        cells = [f"{ld[c]:+.2f} ({ts[c]:+.1f})" if c in ld else "—" for c in columns]
        lines.append(
            f"| {name} | {r['months']} | {r['alpha_annual']:+.2%} | {r['alpha_t']:+.2f} | "
            + " | ".join(cells)
            + f" | {r['r2']:.2f} |"
        )
    lines.append("")
    lines.append("括号内为 Newey-West t 值；|t| < 2 视为与 0 无显著差异。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backtests", nargs="+", type=Path, help="backtest.json files")
    parser.add_argument("--output", type=Path, help="write JSON results here")
    parser.add_argument(
        "--extra",
        nargs="*",
        default=["IEF", "GLD"],
        help="non-equity asset ETFs whose excess returns join the regression",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    factors = load_factors(TableStore.from_settings(settings))
    daily = open_store(settings)
    extra: list[str] = []
    for symbol in args.extra:
        if not daily.has_bars(symbol):
            continue
        bars = daily.read_bars(symbol)
        rets = monthly_returns([b.day.isoformat() for b in bars], [b.adj_close for b in bars])
        name = symbol.lower()
        extra.append(name)
        for month, row in factors.items():
            if month in rets:
                row[name] = rets[month] - row["rf"]
    results: dict[str, dict[str, Any]] = {}
    for path in args.backtests:
        bt = json.loads(path.read_text())
        curve = bt["equity_curve"]
        strategy = monthly_returns(curve["dates"], curve["strategy"])
        results[bt["study"]] = attribute(strategy, factors)
        if extra:
            label = " + ".join(LABELS.get(e, e) for e in extra)
            results[f"{bt['study']} + {label}"] = attribute(strategy, factors, (*FACTORS, *extra))
        bench = f"{bt['benchmark']['symbol']}（基准）"
        results[bench] = attribute(monthly_returns(curve["dates"], curve["benchmark"]), factors)
    text = render(results)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
