"""Small-cap factor sleeve vs the market (study small_factor_sleeve, ``usr-small-factor``)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml


def load_sf_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "small_factor":
        raise ValueError(f"{path} is not a small_factor contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk", "success_criteria"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        v = raw["data"][key]
        raw["data"][key] = v if isinstance(v, date) else date.fromisoformat(str(v))
    return raw


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one study flow
    import argparse
    import hashlib
    import json

    from us_stock_research.config import load_settings
    from us_stock_research.research import factor_sleeve as fs
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.research import sleeve_mix as sm
    from us_stock_research.research.trials import record_and_assess
    from us_stock_research.research.verify_factor_sleeve import _port_months
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    c = load_sf_contract(args.contract)
    data, rule, inf = c["data"], c["rule"], c["inference"]
    a, b = data["start"], data["end"]
    settings = load_settings()
    tables, store = TableStore.from_settings(settings), open_store(settings)
    cols = {
        "small_hiprior": ("me_prior_daily", "small_hiprior"),
        "small_hiop": ("me_op_daily", "small_hiop"),
        "big_hiprior": ("me_prior_daily", "big_hiprior"),
        "big_hiop": ("me_op_daily", "big_hiop"),
        "small_loprior": ("me_prior_daily", "small_loprior"),
        "me1_prior2": ("me_prior_daily", "me1_prior2"),
    }
    raw = {
        k: [(d, float(r)) for d, r in sorted(tables.read("factors", t, f"date, {col}")) if r == r]
        for k, (t, col) in cols.items()
    }
    mkt = [
        (d, float(x) + float(r))
        for d, x, r in sorted(tables.read("factors", "ff5_daily", "date, mkt_rf, rf"))
    ]
    drag = float(rule["implementation_drag_annual"])
    parts = [str(p) for p in rule["parts"]]
    net = fs.daily_factor_sleeve(raw, drag)
    sleeve = fs.sleeve_monthly(net, parts, a, b)
    market = dict(lt.monthly([(d, r) for d, r in mkt if a <= d <= b]))
    months = sorted(set(sleeve) & set(market))
    s_r, m_r = [sleeve[m] for m in months], [market[m] for m in months]
    test = sm.paired_sharpe_bootstrap(
        s_r,
        m_r,
        int(inf["resamples"]),
        int(inf["block_size_months"]),
        float(inf["confidence_level"]),
        int(inf["random_seed"]),
    )
    # independent check: per-(year, month) compounding with the drag inside the product
    ind = [_port_months(raw[p], drag, a, b) for p in parts]
    keys = set.intersection(*(set(x) for x in ind))
    indep = {f"{y:04d}-{mo:02d}": sum(x[(y, mo)] for x in ind) / len(ind) for y, mo in keys}
    worst = max(abs(indep[m] - sleeve[m]) for m in months) if set(indep) >= set(months) else 1.0
    verified = worst < 1e-12
    # executability over the last 60 complete months
    ex: dict[str, Any] = {}
    lo, hi = date(2021, 1, 1), date(2025, 12, 31)
    for label, port, sym in (
        ("momentum", "small_hiprior", data["implementation_etfs"]["momentum"]),
        ("quality", "small_hiop", data["implementation_etfs"]["quality"]),
    ):
        bars = [x for x in store.read_bars(sym) if x.adj_close > 0]
        e_daily = [
            (bars[i].day, bars[i].adj_close / bars[i - 1].adj_close - 1)
            for i in range(1, len(bars))
        ]
        e_m = dict(lt.monthly([(d, r) for d, r in e_daily if lo <= d <= hi]))
        f_m = dict(lt.monthly([(d, r) for d, r in net[port] if lo <= d <= hi]))
        ks = sorted(set(e_m) & set(f_m))
        ex[label] = {
            "etf": sym,
            "months": len(ks),
            "correlation": fs.correlation([e_m[k] for k in ks], [f_m[k] for k in ks]),
            "annual_gap": lt.metrics([e_m[k] for k in ks])["cagr"]
            - lt.metrics([f_m[k] for k in ks])["cagr"],
        }
    executable = all(v["correlation"] >= 0.90 for v in ex.values())
    digest = hashlib.sha256(repr([(m, sleeve[m], market[m]) for m in months]).encode())
    artifact: dict[str, Any] = {
        "study": c["name"],
        "strategy": "small_factor_v1",
        "parameters": json.loads(json.dumps({"rule": rule, "data": data}, default=str)),
        "snapshot_id": "sf-data-v1:sha256:" + digest.hexdigest(),
        "trading_enabled": False,
        "strategy_monthly_returns": s_r,
    }
    mt = record_and_assess(args.registry, artifact, ["french_small_2x3"])
    m_s, m_m = lt.metrics(s_r), lt.metrics(m_r)
    big = fs.sleeve_monthly(net, ["big_hiprior", "big_hiop"], a, b)
    small_all = fs.sleeve_monthly(raw, ["small_loprior", "me1_prior2", "small_hiprior"], a, b)
    table = {
        "small_factor": m_s,
        "market": m_m,
        "big_factor": lt.metrics([big[m] for m in months]),
        "small_all": lt.metrics([small_all[m] for m in months]),
    }
    spy_bars = [x for x in store.read_bars("SPY") if x.adj_close > 0]
    spy_d = [
        (spy_bars[i].day, spy_bars[i].adj_close / spy_bars[i - 1].adj_close - 1)
        for i in range(1, len(spy_bars))
    ]
    spy_m = dict(lt.monthly([(d, r) for d, r in spy_d if date(1994, 1, 1) <= d <= b]))
    sk = [m for m in months if m in spy_m]
    vs_spy = {
        "months": len(sk),
        "small_factor": lt.metrics([sleeve[m] for m in sk]),
        "spy": lt.metrics([spy_m[m] for m in sk]),
    }
    sub = {}
    for x, y in c.get("reporting", {}).get("subperiods", []):
        ks = [m for m in months if str(x)[:7] <= m <= str(y)[:7]]
        sub[f"{str(x)[:4]}-{str(y)[:4]}"] = {
            "small_factor": lt.metrics([sleeve[m] for m in ks]),
            "market": lt.metrics([market[m] for m in ks]),
            "sharpe_diff": sm.sharpe([sleeve[m] for m in ks]) - sm.sharpe([market[m] for m in ks]),
        }
    variants = {
        k: lt.metrics([fs.sleeve_monthly(net, [p], a, b)[m] for m in months])
        for k, p in c["reporting"]["variants"].items()
    }
    drag_sens = {}
    for dg in c["reporting"]["drag_sensitivity"]:
        alt = fs.sleeve_monthly(fs.daily_factor_sleeve(raw, float(dg)), parts, a, b)
        drag_sens[str(dg)] = {
            **lt.metrics([alt[m] for m in months]),
            "sharpe_diff": sm.sharpe([alt[m] for m in months]) - sm.sharpe(m_r),
        }
    fails = sm.evaluate(c, test, m_s, mt["deflated_sharpe"], verified)
    if not executable:
        fails.append(
            "可执行性：ETF 与 French 组合月相关未全部 ≥ 0.90 "
            + str({k: round(v["correlation"], 3) for k, v in ex.items()})
        )
    artifact.update(
        metrics=table,
        sharpe_test=test,
        vs_spy=vs_spy,
        subperiods=sub,
        variants=variants,
        drag_sensitivity=drag_sens,
        executability=ex,
        verification={"months": len(months), "max_abs_diff": worst, "match": verified},
        multiple_testing=mt,
        failed_criteria=fails,
        months=months,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    brief = {
        k: v
        for k, v in artifact.items()
        if k not in ("strategy_monthly_returns", "months", "parameters")
    }
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
