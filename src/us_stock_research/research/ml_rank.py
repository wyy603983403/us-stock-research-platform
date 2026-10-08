"""Stage 2 of study sp500_ml_rank (``usr-ml-rank``): walk-forward LightGBM ranking and portfolios.

Reads the stage-1 panel (``usr-ml-panel``), so it needs no price store. Each January of the test
period a model is trained on every earlier month whose holding period had ended by that signal day
(expanding window from ``ml.train_start``); features are monthly cross-sectional percentiles (the
market-state inputs stay raw); the label is the percentile of the next holding-period return. The
test months then hold the top ``top_n`` predictions equal-weight with the same monthly cost and
drift accounting as ``cross_section.run``. Also built: the pre-registered linear composite, an ML
variant without macro inputs and three single-group variants (reporting only).

The independent check (``usr-verify-ml``) recomputes the features from the raw data and replays the
portfolios through ``cross_section.run``; its report is passed back with ``--verification``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

from us_stock_research.research import cross_section as xs
from us_stock_research.research.fundamentals import percentile_ranks
from us_stock_research.research.ml_features import (
    FLOWS,
    FUND,
    MARKET_STATE,
    STOCK_FEATURES,
    TECH,
)
from us_stock_research.research.stats import block_bootstrap

Month = dict[str, Any]
Row = dict[str, Any]
FEATURE_SETS = {
    "ml": (STOCK_FEATURES, MARKET_STATE),
    "ml_no_macro": (TECH + FLOWS + FUND, ()),
    "technical_only": (TECH, ()),
    "flows_only": (FLOWS, ()),
    "fundamental_only": (FUND, ()),
}


def load_panel(path: Path) -> tuple[list[Month], dict[str, list[Row]]]:
    months: list[Month] = []
    rows: dict[str, list[Row]] = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r["type"] == "month":
                months.append(r)
                rows[r["date"]] = []
            else:
                rows[r["date"]].append(r)
    return months, rows


def design(
    rows: list[Row], month: Month, stock_feats: tuple[str, ...], state: tuple[str, ...]
) -> list[list[float]]:
    """Feature matrix of one month: stock features as percentiles (NaN = missing), state raw."""
    ranks = {}
    for k in stock_feats:
        have = {r["symbol"]: r["f"][k] for r in rows if r["f"].get(k) is not None}
        ranks[k] = percentile_ranks(have) if have else {}
    ms = month["market_state"]
    out = []
    for r in rows:
        x = [ranks[k].get(r["symbol"], math.nan) for k in stock_feats]
        x += [math.nan if ms.get(k) is None else float(ms[k]) for k in state]
        out.append(x)
    return out


def label(rows: list[Row]) -> list[float]:
    ranks = percentile_ranks({r["symbol"]: r["ret"] for r in rows})
    return [ranks[r["symbol"]] for r in rows]


def fit(params: dict[str, Any], x: list[list[float]], y: list[float]) -> Any:
    import lightgbm as lgb

    model = lgb.LGBMRegressor(**params)
    model.fit(x, y)
    return model


def linear_scores(rows: list[Row], dirs: dict[str, dict[str, str]]) -> dict[str, float]:
    """Pre-registered composite: signed percentiles, mean within group, mean of groups present."""
    group_ranks: dict[str, dict[str, dict[str, float]]] = {}
    for group, feats in dirs.items():
        group_ranks[group] = {}
        for k, sign in feats.items():
            have = {r["symbol"]: r["f"][k] for r in rows if r["f"].get(k) is not None}
            pr = percentile_ranks(have) if have else {}
            group_ranks[group][k] = {s: v if sign == "+" else 1 - v for s, v in pr.items()}
    out = {}
    for r in rows:
        s = r["symbol"]
        means = []
        for feats in group_ranks.values():
            vals = [ranks[s] for ranks in feats.values() if s in ranks]
            if vals:
                means.append(sum(vals) / len(vals))
        if means:
            out[s] = sum(means) / len(means)
    return out


def contract_dirs(c: dict[str, Any]) -> dict[str, dict[str, str]]:
    f = c["features"]
    return {
        g: {k: str(v["dir"]) for k, v in f[g].items()}
        for g in ("technical", "flows", "fundamental")
    }


def portfolio(
    test: list[Month],
    rows: dict[str, list[Row]],
    scores: dict[str, dict[str, float]],
    top_n: int,
    bps: float,
    ret_key: str = "ret",
) -> list[dict[str, Any]]:
    """Same accounting as cross_section.run: equal weight, cost on traded weight, drift."""
    held: dict[str, float] = {}
    ew_held: dict[str, float] = {}
    out = []
    for m in test:
        rs = rows[m["date"]]
        sc = scores[m["date"]]
        picks = sorted(sc, key=lambda s: (-sc[s], s))[:top_n]
        rets = {r["symbol"]: r[ret_key] for r in rs}
        target = {s: 1 / len(picks) for s in picks} if picks else {}
        ew_target = {s: 1 / len(rets) for s in rets} if rets else {}
        cost, turnover = xs.rebalance_cost(held, target, bps)
        ew_cost, _ = xs.rebalance_cost(ew_held, ew_target, bps)
        strat = sum(w * rets[s] for s, w in target.items()) - cost
        ew = sum(w * rets[s] for s, w in ew_target.items()) - ew_cost
        held, ew_held = xs.drift(target, rets), xs.drift(ew_target, rets)
        out.append(
            {
                "date": m["date"],
                "strategy": strat,
                "equal_weight": ew,
                "benchmark": m["spy"],
                "turnover_one_way": turnover,
                "picks": picks,
                "coverage": m["priced"] / m["members"] if m["members"] else 0.0,
                "fundamentals_coverage": m["with_fundamentals"] / m["eligible"]
                if m["eligible"]
                else 0.0,
            }
        )
    return out


def spearman(a: list[float], b: list[float]) -> float:
    ra = percentile_ranks(dict(enumerate(a)))
    rb = percentile_ranks(dict(enumerate(b)))
    xa, xb = [ra[i] for i in range(len(a))], [rb[i] for i in range(len(b))]
    ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
    cov = sum((p - ma) * (q - mb) for p, q in zip(xa, xb, strict=True))
    va = sum((p - ma) ** 2 for p in xa)
    vb = sum((q - mb) ** 2 for q in xb)
    return cov / math.sqrt(va * vb) if va and vb else 0.0


def walk_forward(
    c: dict[str, Any],
    months: list[Month],
    rows: dict[str, list[Row]],
    feats: tuple[str, ...],
    state: tuple[str, ...],
    test: list[Month],
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Predictions per test month and per-year diagnostics (training size, IC, importance)."""
    ml = c["ml"]
    params = dict(ml["params"])
    train_start = str(ml["train_start"])
    preds: dict[str, dict[str, float]] = {}
    info: dict[str, Any] = {}
    cache: dict[str, tuple[list[list[float]], list[float]]] = {}
    model = None
    year = None
    for m in test:
        y = m["date"][:4]
        if y != year:
            year = y
            xs_, ys_ = [], []
            n_months = 0
            for h in months:
                if h["date"] < train_start or h["exit_date"] > m["date"]:
                    continue
                key = h["date"]
                if key not in cache:
                    rs = rows[key]
                    cache[key] = (design(rs, h, feats, state), label(rs)) if rs else ([], [])
                xs_ += cache[key][0]
                ys_ += cache[key][1]
                n_months += 1
            if len(ys_) < int(ml["min_training_rows"]):
                raise SystemExit(f"{y}: only {len(ys_)} training rows")
            model = fit(params, xs_, ys_)
            names = list(feats) + list(state)
            gains = model.booster_.feature_importance(importance_type="gain")
            info[y] = {
                "training_months": n_months,
                "training_rows": len(ys_),
                "ic": [],
                "importance": dict(zip(names, (float(g) for g in gains), strict=True)),
            }
        rs = rows[m["date"]]
        p = model.predict(design(rs, m, feats, state)) if rs else []  # type: ignore[union-attr]
        preds[m["date"]] = {r["symbol"]: float(v) for r, v in zip(rs, p, strict=True)}
        info[y]["ic"].append(spearman([float(v) for v in p], [r["ret"] for r in rs]))
    for v in info.values():
        ic = v.pop("ic")
        mean = sum(ic) / len(ic)
        sd = math.sqrt(sum((x - mean) ** 2 for x in ic) / (len(ic) - 1)) if len(ic) > 1 else 0.0
        v["ic_mean"], v["ic_months"] = mean, len(ic)
        v["ic_t"] = mean / (sd / math.sqrt(len(ic))) if sd else None
    return preds, info


def excess_test(a: list[float], b: list[float], inf: dict[str, Any]) -> dict[str, Any]:
    return block_bootstrap(
        [x - y for x, y in zip(a, b, strict=True)],
        int(inf["resamples"]),
        int(inf["block_size_months"]),
        float(inf["confidence_level"]),
        int(inf["random_seed"]),
    )


def evaluate(
    c: dict[str, Any],
    ml_vs_ew: dict[str, Any],
    ml_vs_linear: dict[str, Any],
    metrics: dict[str, Any],
    dsr: float | None,
    months: list[dict[str, Any]],
    verification: dict[str, Any] | None,
) -> list[str]:
    fails = []
    iv = ml_vs_ew.get("interval")
    if not iv or iv[0] <= 0:
        fails.append("相对等权的超额收益 95% 置信区间下限不为正")
    if not ml_vs_linear.get("mean_monthly_excess", 0) > 0:
        fails.append("机器学习未优于线性对照（月均超额 ≤ 0）")
    if dsr is None or dsr < 0.95:
        fails.append(f"Deflated Sharpe {dsr if dsr is None else round(dsr, 3)} < 0.95")
    worst = metrics["worst_rolling_12m_return"]
    cap = float(c["risk"]["max_worst_12m_loss"])
    if worst is not None and worst < -cap:
        fails.append(f"最差滚动 12 个月 {worst:.1%}，超过 {cap:.0%}")
    low = min(m["coverage"] for m in months)
    if low < 0.9:
        fails.append(f"最低价格覆盖率 {low:.1%} < 90%")
    fund = min(m["fundamentals_coverage"] for m in months)
    if fund < 0.85:
        fails.append(f"最低基本面覆盖率 {fund:.1%} < 85%")
    if not verification or not verification.get("match"):
        fails.append("独立核对未完成或不一致")
    return fails


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one study flow
    from us_stock_research.research.trials import record_and_assess

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verification", type=Path, help="usr-verify-ml report")
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    parser.add_argument("--no-register", action="store_true", help="dry run: no trial record")
    args = parser.parse_args(argv)
    import lightgbm

    c = xs.load_xs_contract(args.contract)
    uni, inf, sel = c["universe"], c["inference"], c["selection"]
    months, rows = load_panel(args.panel)
    start = uni["start"].isoformat() if isinstance(uni["start"], date) else str(uni["start"])
    test = [m for m in months if m["date"] >= start]
    top_n, bps = int(sel["top_n"]), float(c["transaction_cost_bps"])
    results: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    predictions: dict[str, dict[str, dict[str, float]]] = {}
    for name, (feats, state) in FEATURE_SETS.items():
        preds, info = walk_forward(c, months, rows, feats, state, test)
        predictions[name] = preds
        diagnostics[name] = info
        results[name] = portfolio(test, rows, preds, top_n, bps)
    lin = {m["date"]: linear_scores(rows[m["date"]], contract_dirs(c)) for m in test}
    results["linear"] = portfolio(test, rows, lin, top_n, bps)
    stressed = portfolio(test, rows, predictions["ml"], top_n, bps, "ret_haircut")
    main_ = results["ml"]
    strat = [m["strategy"] for m in main_]
    ew = [m["equal_weight"] for m in main_]
    lin_r = [m["strategy"] for m in results["linear"]]
    table = {k: xs.monthly_metrics([m["strategy"] for m in v]) for k, v in results.items()}
    table["equal_weight_universe"] = xs.monthly_metrics(ew)
    table["spy"] = xs.monthly_metrics([m["benchmark"] for m in main_])
    ml_vs_ew = excess_test(strat, ew, inf)
    ml_vs_lin = excess_test(strat, lin_r, inf)
    halves = {}
    for lo, hi in (("2015", "2019"), ("2020", "2025")):
        idx = [i for i, m in enumerate(main_) if lo <= m["date"][:4] <= hi]
        halves[f"{lo}-{hi}"] = {
            k: xs.monthly_metrics([results[k][i]["strategy"] for i in idx])["cagr"]
            for k in ("ml", "linear")
        } | {"equal_weight": xs.monthly_metrics([ew[i] for i in idx])["cagr"]}
    meta = (
        json.loads(args.panel.with_suffix(".meta.json").read_text())
        if args.panel.with_suffix(".meta.json").exists()
        else {}
    )
    artifact: dict[str, Any] = {
        "study": c["name"],
        "strategy": "xsec_ml_rank",
        "parameters": json.loads(
            json.dumps(
                {
                    "ml": c["ml"],
                    "features": c["features"],
                    "selection": sel,
                    "execution_lag_days": c["execution_lag_days"],
                    "transaction_cost_bps": bps,
                },
                default=str,
            )
        ),
        "snapshot_id": meta.get("snapshot_id", "unknown"),
        "lightgbm": lightgbm.__version__,
        "trading_enabled": False,
        "strategy_monthly_returns": strat,
    }
    if args.no_register:
        mt = {"deflated_sharpe": None, "note": "dry run"}
    else:
        mt = record_and_assess(args.registry, artifact, ["sp500_point_in_time"])
    verification = json.loads(args.verification.read_text()) if args.verification else None
    artifact.update(
        metrics=table,
        excess_vs_equal_weight=ml_vs_ew,
        excess_vs_linear=ml_vs_lin,
        excess_others_vs_equal_weight={
            k: excess_test([m["strategy"] for m in v], ew, inf)
            for k, v in results.items()
            if k != "ml"
        },
        haircut_sensitivity=xs.monthly_metrics([m["strategy"] for m in stressed]),
        halves=halves,
        diagnostics=diagnostics,
        avg_turnover_one_way={
            k: sum(m["turnover_one_way"] for m in v) / len(v) for k, v in results.items()
        },
        coverage_min=min(m["coverage"] for m in main_),
        fundamentals_coverage_min=min(m["fundamentals_coverage"] for m in main_),
        verification=verification,
        multiple_testing=mt,
        months=[
            {k: m[k] for k in ("date", "strategy", "equal_weight", "benchmark", "picks")}
            for m in main_
        ],
    )
    artifact["failed_criteria"] = evaluate(
        c, ml_vs_ew, ml_vs_lin, table["ml"], mt.get("deflated_sharpe"), main_, verification
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    args.output.with_name("predictions.json").write_text(json.dumps(predictions["ml"]) + "\n")
    brief = {
        "cagr": {k: v["cagr"] for k, v in table.items()},
        "sharpe": {k: v["sharpe_rf0"] for k, v in table.items()},
        "ml_vs_ew": ml_vs_ew,
        "ml_vs_linear": ml_vs_lin,
        "deflated_sharpe": mt.get("deflated_sharpe"),
        "failed_criteria": artifact["failed_criteria"],
    }
    print(json.dumps(brief, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
