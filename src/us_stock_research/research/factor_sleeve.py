"""Factor sleeve added to the approved two-sleeve mix (``research/factor-sleeve``).

The approved 50/50 mix (``usr-sleeve-mix``) is rebuilt unchanged; a third sleeve holds equal parts
of Kenneth French's value-weighted BIG HiPRIOR and BIG HiOP daily portfolios net of an annual
implementation drag, and the three sleeves are rebalanced to 40/40/20 at each month end.
``usr-factor-sleeve`` writes the artifact, registers the trial and checks MTUM/QUAL tracking.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml


def load_fs_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "factor_sleeve":
        raise ValueError(f"{path} is not a factor_sleeve contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk", "success_criteria"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        value = raw["data"][key]
        raw["data"][key] = value if isinstance(value, date) else date.fromisoformat(str(value))
    weights = raw["rule"]["weights"]
    if abs(sum(float(v) for v in weights.values()) - 1) > 1e-9:
        raise ValueError(f"{path}: sleeve weights must sum to 1")
    return raw


TRADING_DAYS = 252


def daily_factor_sleeve(
    portfolios: dict[str, list[tuple[date, float]]], drag_annual: float
) -> dict[str, list[tuple[date, float]]]:
    """Each portfolio's daily return net of the implementation drag (spread evenly per day)."""
    per_day = drag_annual / TRADING_DAYS
    return {name: [(d, (1 + r) * (1 - per_day) - 1) for d, r in rows]
            for name, rows in portfolios.items()}  # fmt: skip


def sleeve_monthly(
    series: dict[str, list[tuple[date, float]]], parts: list[str], start: date, end: date
) -> dict[str, float]:
    """Equal parts of ``parts``, rebalanced each month end (mean of compounded monthly returns)."""
    from us_stock_research.research import leveraged_trend as lt

    months = [dict(lt.monthly([(d, r) for d, r in series[p] if start <= d <= end])) for p in parts]
    common = sorted(set.intersection(*(set(m) for m in months)))
    return {k: sum(m[k] for m in months) / len(months) for k in common}


def combine_many(
    sleeves: dict[str, dict[str, float]], weights: dict[str, float], cost_bps: float
) -> dict[str, float]:
    """Monthly mix of several sleeves, rebalanced to ``weights`` at each month end.

    Cost = sum over sleeves of |drifted weight - target weight| x ``cost_bps``; with two sleeves
    this equals ``sleeve_mix.combine``.
    """
    names = list(weights)
    keys = sorted(set.intersection(*(set(sleeves[n]) for n in names)))
    out: dict[str, float] = {}
    for m in keys:
        gross = sum(weights[n] * sleeves[n][m] for n in names)
        turnover = sum(
            abs(weights[n] * (1 + sleeves[n][m]) / (1 + gross) - weights[n]) for n in names
        )
        out[m] = (1 + gross) * (1 - turnover * cost_bps / 10_000) - 1
    return out


def correlation(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / (sxx * syy) ** 0.5


def full_months(first_day: date, start: date, end: date) -> tuple[date, date]:
    """First and last day of the complete calendar months from ``first_day`` (inclusive)."""
    y, m = first_day.year, first_day.month
    if first_day.day != 1:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return max(date(y, m, 1), start), end


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one study, one flow
    import argparse
    import hashlib
    import json

    from us_stock_research.config import load_settings
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.research import sleeve_mix as sm
    from us_stock_research.research import verify_factor_sleeve as vfs
    from us_stock_research.research.trials import record_and_assess
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    c = load_fs_contract(args.contract)
    data, rule, inf, rep = c["data"], c["rule"], c["inference"], c.get("reporting", {})
    start, end = data["start"], data["end"]
    base = sm.load_mix_contract(Path(data["base_contract"]))
    settings = load_settings()
    tables, store = TableStore.from_settings(settings), open_store(settings)

    # the approved two-sleeve mix, exactly as in usr-sleeve-mix
    vt_c = lt.load_lt_contract(Path(base["data"]["aggressive_sleeve"]))
    days, prices, yields, _ = lt.load_inputs(tables, store, vt_c)
    vt = lt.run(vt_c, days, prices, yields, start=start, end=end)
    aggressive = dict(zip(vt["months"], vt["strategy"], strict=True))
    spx = dict(zip(vt["months"], vt["benchmark"], strict=True))
    rows = tables.read("macro", base["data"]["cash_rate"], "date, value")
    rate = {d: float(v) for d, v in rows if v is not None and v == v}
    inputs = sm.load_defensive_inputs(store, list(base["data"]["defensive_assets"]), rate)
    sma, bps = int(base["rule"]["sma_days"]), float(rule["trading_cost_bps"])
    defensive = sm.defensive_monthly(sm.defensive_series(inputs, sma, bps), start, end)
    approved = sm.combine(aggressive, defensive, float(base["rule"]["aggressive_weight"]), bps)

    # factor portfolios (value-weighted, total return) net of the implementation drag
    raw = {"big_hiprior": tables.read("factors", "me_prior_daily", "date, big_hiprior"),
           "big_hiop": tables.read("factors", "me_op_daily", "date, big_hiop")}  # fmt: skip
    ports = {k: [(d, float(r)) for d, r in sorted(v) if r == r] for k, v in raw.items()}
    drag = float(rule["implementation_drag_annual"])
    net = daily_factor_sleeve(ports, drag)
    parts = ["big_hiprior", "big_hiop"]
    factor = sleeve_monthly(net, parts, start, end)
    w = {k: float(v) for k, v in rule["weights"].items()}
    sleeves = {"aggressive": aggressive, "defensive": defensive, "factor": factor}
    mix = combine_many(sleeves, w, bps)
    months = sorted(set(mix) & set(approved))
    if len(months) != len(approved) or any(m not in mix for m in approved):
        raise SystemExit("factor data do not cover every month of the approved mix")
    two = combine_many({"aggressive": aggressive, "defensive": defensive},
                       {"aggressive": 0.5, "defensive": 0.5}, bps)  # fmt: skip
    same_base = max(abs(two[m] - approved[m]) for m in months) < 1e-14

    m_mix, m_app = [mix[m] for m in months], [approved[m] for m in months]
    test = sm.paired_sharpe_bootstrap(
        m_mix, m_app, int(inf["resamples"]), int(inf["block_size_months"]),
        float(inf["confidence_level"]), int(inf["random_seed"]),
    )  # fmt: skip
    independent = vfs.mix_monthly(
        vt_monthly=aggressive, inputs=inputs, sma=sma, bps=bps,
        portfolios=ports, drag=drag, weights=w, start=start, end=end,
    )  # fmt: skip
    worst_diff = (max(abs(independent[m] - mix[m]) for m in months)
                  if set(independent) >= set(months) else float("inf"))  # fmt: skip
    verified = worst_diff < 1e-12 and same_base

    # executability: ETF vs French portfolio (net of drag), complete common months
    etf_check: dict[str, Any] = {}
    etfs = {"momentum": ("big_hiprior", data["implementation_etfs"]["momentum"]),
            "quality": ("big_hiop", data["implementation_etfs"]["quality"])}  # fmt: skip
    etf_series = {}
    for label, (port, sym) in etfs.items():
        bars = [b for b in store.read_bars(sym) if b.adj_close > 0]
        etf_series[label] = (port, sym, bars)
    first = max(bars[0].day for _, _, bars in etf_series.values())
    a, b = full_months(first, date(1900, 1, 1), end)
    mkt_rows = tables.read("factors", "ff5_daily", "date, mkt_rf, rf")
    market = [(d, float(x) + float(r)) for d, x, r in sorted(mkt_rows)]
    mkt_m = dict(lt.monthly([(d, r) for d, r in market if a <= d <= b]))
    for label, (port, sym, bars) in etf_series.items():
        e_daily = [(bars[i].day, bars[i].adj_close / bars[i - 1].adj_close - 1)
                   for i in range(1, len(bars))]  # fmt: skip
        e_m = dict(lt.monthly([(d, r) for d, r in e_daily if a <= d <= b]))
        f_m = dict(lt.monthly([(d, r) for d, r in net[port] if a <= d <= b]))
        ks = sorted(set(e_m) & set(f_m) & set(mkt_m))
        ex, fx = [e_m[k] for k in ks], [f_m[k] for k in ks]
        gap = (lt.metrics(ex)["cagr"] - lt.metrics(fx)["cagr"]) if ks else None
        etf_check[label] = {
            "etf": sym, "portfolio": port, "months": len(ks), "first": ks[0], "last": ks[-1],
            "correlation": correlation(ex, fx),
            "active_correlation": correlation([e_m[k] - mkt_m[k] for k in ks],
                                              [f_m[k] - mkt_m[k] for k in ks]),
            "annual_tracking_difference": gap,
        }  # fmt: skip
    executable = all(v["correlation"] >= 0.95 for v in etf_check.values())

    digest = hashlib.sha256()
    for m in months:
        digest.update(repr((m, aggressive[m], defensive[m], factor[m])).encode())
    artifact: dict[str, Any] = {
        "study": c["name"], "strategy": "factor_sleeve_mix_v1",
        "parameters": {"rule": rule, "data": {k: str(v) for k, v in data.items()}},
        "snapshot_id": "fsm-data-v1:sha256:" + digest.hexdigest(),
        "trading_enabled": False, "strategy_monthly_returns": m_mix, "months": months,
    }  # fmt: skip
    symbols = list(base["data"]["defensive_assets"]) + ["^GSPC", "MTUM", "QUAL"]
    mt = record_and_assess(args.registry, artifact, symbols)

    def pick(series: dict[str, float]) -> dict[str, Any]:
        return lt.metrics([series[m] for m in months])

    mkt_all = dict(lt.monthly(market))
    table = {"mix_40_40_20": pick(mix), "approved_50_50": pick(approved),
             "factor_sleeve": pick(factor), "sp500_total_return": pick(spx),
             "french_market": pick(mkt_all)}  # fmt: skip
    variants = {}
    for name, use in (("momentum_only", ["big_hiprior"]), ("quality_only", ["big_hiop"])):
        alt = combine_many({**sleeves, "factor": sleeve_monthly(net, use, start, end)}, w, bps)
        variants[name] = {**pick(alt), "sharpe_minus_approved":
                          sm.sharpe([alt[m] for m in months]) - sm.sharpe(m_app)}  # fmt: skip
    sens = {}
    for wf in rep.get("weights_sensitivity", []):
        wf = float(wf)
        alt_w = {"aggressive": (1 - wf) / 2, "defensive": (1 - wf) / 2, "factor": wf}
        alt = combine_many(sleeves, alt_w, bps)
        gain = sm.sharpe([alt[m] for m in months]) - sm.sharpe(m_app)
        sens[f"factor_{wf:g}"] = {**pick(alt), "sharpe_minus_approved": gain}
    long_a = date(1963, 7, 1)
    long_f = sleeve_monthly(net, parts, long_a, end)
    long_m = dict(lt.monthly([(d, r) for d, r in market if long_a <= d <= end]))
    lk = sorted(set(long_f) & set(long_m))
    long_sample = {"months": len(lk), "first": lk[0], "last": lk[-1],
                   "factor_sleeve": lt.metrics([long_f[k] for k in lk]),
                   "french_market": lt.metrics([long_m[k] for k in lk])}  # fmt: skip
    years = {str(y): {n: sm.calendar_years(s).get(str(y)) for n, s in
                      (("mix", mix), ("approved", approved), ("factor", factor), ("sp500", spx))}
             for y in rep.get("years", [])}  # fmt: skip
    m_mix_stats = table["mix_40_40_20"]
    fails = sm.evaluate(c, test, m_mix_stats, mt["deflated_sharpe"], verified)
    if not executable:
        low = {k: round(v["correlation"], 3) for k, v in etf_check.items()}
        fails.append(f"ETF 与 French 组合月收益相关系数未全部 ≥ 0.95：{low}")
    artifact.update(
        metrics=table, sharpe_test=test, variants=variants, sensitivity=sens,
        long_sample=long_sample, etf_tracking=etf_check, years=years,
        correlation_factor_vs_aggressive=correlation([factor[m] for m in months],
                                                     [aggressive[m] for m in months]),
        verification={"months": len(months), "max_abs_diff": worst_diff,
                      "two_sleeve_equals_approved": same_base, "match": verified},
        multiple_testing=mt, failed_criteria=fails,
    )  # fmt: skip
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    brief = {k: v for k, v in artifact.items() if k not in ("strategy_monthly_returns", "months")}
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
