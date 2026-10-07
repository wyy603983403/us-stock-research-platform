"""Trend-signal vote for the volatility-target S&P 500 strategy (``usr-trend-ensemble``).

Same data, execution, costs and financing as ``sp500_trend_voltarget``; only the on/off switch
becomes a vote: one point each for the total-return close above its 50-, 100- and 200-day
averages and for the 252-day return above T-bills over the same days. Target exposure =
points / 4 x min(cap, vol target / 20-day volatility). Compared with the approved single-switch
engine on the same months (paired block bootstrap of the Sharpe difference).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.research import leveraged_trend as lt

TRADING_DAYS = 252


def load_te_contract(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if raw.get("kind") != "trend_ensemble":
        raise ValueError(f"{path} is not a trend_ensemble contract")
    for key in ("name", "data", "rule", "benchmark", "inference", "risk"):
        if key not in raw:
            raise ValueError(f"{path} lacks {key}")
    for key in ("start", "end"):
        v = raw["data"][key]
        raw["data"][key] = v if isinstance(v, date) else date.fromisoformat(str(v))
    return raw


def vote_fraction(
    prices: list[float],
    yields_pct: list[float | None],
    i: int,
    sma_days: list[int],
    momentum_days: int | None,
) -> float | None:
    """Share of trend signals that are on at index ``i`` (None while history is too short)."""
    if i < max(sma_days) - 1 or (momentum_days and i < momentum_days):
        return None  # an n-day average needs n closes; the momentum vote needs a price n days ago
    votes = [prices[i] > sum(prices[i - n + 1 : i + 1]) / n for n in sma_days]
    if momentum_days:
        bill = 1.0
        for j in range(i - momentum_days + 1, i + 1):
            y = yields_pct[j - 1]
            if y is None:
                return None
            bill *= 1 + y / 100 / TRADING_DAYS
        votes.append(prices[i] / prices[i - momentum_days] > bill)
    return sum(votes) / len(votes)


def ensemble_targets(
    prices: list[float],
    yields_pct: list[float | None],
    *,
    sma_days: list[int],
    momentum_days: int | None,
    vol_window: int,
    vol_target: float,
    max_leverage: float,
) -> list[float | None]:
    n = len(prices)
    rets = [0.0] + [prices[i] / prices[i - 1] - 1 for i in range(1, n)]
    out: list[float | None] = [None] * n
    for i in range(max(vol_window, 1), n):
        frac = vote_fraction(prices, yields_pct, i, sma_days, momentum_days)
        if frac is None:
            continue
        if frac == 0:
            out[i] = 0.0
            continue
        window = rets[i - vol_window + 1 : i + 1]
        mean = sum(window) / vol_window
        vol = math.sqrt(sum((x - mean) ** 2 for x in window) / (vol_window - 1) * TRADING_DAYS)
        out[i] = frac * (max_leverage if vol <= 0 else min(max_leverage, vol_target / vol))
    return out


def simulate_targets(
    days: list[date],
    prices: list[float],
    yields_pct: list[float | None],
    target: list[float | None],
    *,
    band: float,
    cost_bps: float,
) -> list[tuple[date, float, float, float]]:
    """Execution identical to ``leveraged_trend.simulate_vol_target`` for any target series."""
    n = len(prices)
    rets = [0.0] + [prices[i] / prices[i - 1] - 1 for i in range(1, n)]
    out: list[tuple[date, float, float, float]] = []
    held: float | None = None
    for k in range(2, n):
        t, y = target[k - 2], yields_pct[k - 1]
        if t is None or y is None:
            continue
        cost = 0.0
        if held is None or (t == 0.0) != (held == 0.0) or abs(t - held) > band:
            cost = abs(t - (held or 0.0)) * cost_bps / 10_000 if held is not None else 0.0
            held = t
        rate = y / 100
        e, r = held, rets[k]
        if e <= 1:
            ret = e * r + (1 - e) * rate / TRADING_DAYS
        else:
            ret = e * r - (e - 1) * (rate + lt.FINANCING_SPREAD + lt.LEVERAGED_FEE) / TRADING_DAYS
        if cost:
            ret = (1 + ret) * (1 - cost) - 1
        out.append((days[k], ret, r, e))
    return out


def run_ensemble(
    rule: dict[str, Any],
    days: list[date],
    prices: list[float],
    yields: list[float | None],
    *,
    momentum: bool = True,
) -> list[tuple[date, float, float, float]]:
    sig = rule["signals"]
    targets = ensemble_targets(
        prices,
        yields,
        sma_days=[int(x) for x in sig["sma_days"]],
        momentum_days=int(sig["momentum_days"]) if momentum else None,
        vol_window=int(rule["vol_window_days"]),
        vol_target=float(rule["vol_target"]),
        max_leverage=float(rule["max_leverage"]),
    )
    return simulate_targets(days, prices, yields, targets, band=float(rule["rebalance_band"]),
                            cost_bps=float(rule["trading_cost_bps"]))  # fmt: skip


def window(rows: list[tuple[date, float, float, float]], a: date, b: date) -> dict[str, Any]:
    sim = [r for r in rows if a <= r[0] <= b]
    strat = lt.monthly([(d, r) for d, r, _, _ in sim])
    switches = sum(1 for x, y in zip(sim, sim[1:], strict=False) if x[3] != y[3])
    return {
        "months": [m for m, _ in strat],
        "strategy": [r for _, r in strat],
        "benchmark": [r for _, r in lt.monthly([(d, a_) for d, _, a_, _ in sim])],
        "average_exposure": sum(r[3] for r in sim) / len(sim),
        "switches_per_year": switches / (len(sim) / TRADING_DAYS),
    }


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research import verify_ensemble
    from us_stock_research.research.sleeve_mix import paired_sharpe_bootstrap
    from us_stock_research.research.trials import record_and_assess
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("research/trials.jsonl"))
    args = parser.parse_args(argv)
    c = load_te_contract(args.contract)
    rule, inf, data = c["rule"], c["inference"], c["data"]
    a, b = data["start"], data["end"]
    settings = load_settings()
    base = lt.load_lt_contract(Path(data["baseline_contract"]))
    days, prices, yields, _ = lt.load_inputs(TableStore.from_settings(settings),
                                            open_store(settings), base)  # fmt: skip
    ens_rows = run_ensemble(rule, days, prices, yields)
    ens = window(ens_rows, a, b)
    approved = lt.run(base, days, prices, yields, start=a, end=b)
    if approved["months"] != ens["months"]:
        raise SystemExit("baseline and ensemble cover different months")
    # 200-day vote only must reproduce the approved engine day by day
    single = simulate_targets(
        days, prices, yields,
        ensemble_targets(prices, yields, sma_days=[int(base["rule"]["sma_days"])],
                         momentum_days=None, vol_window=int(rule["vol_window_days"]),
                         vol_target=float(rule["vol_target"]),
                         max_leverage=float(rule["max_leverage"])),
        band=float(rule["rebalance_band"]), cost_bps=float(rule["trading_cost_bps"]),
    )  # fmt: skip
    ref = lt.simulate_vol_target(
        days, prices, yields, sma_days=int(base["rule"]["sma_days"]),
        vol_window=int(base["rule"]["vol_window_days"]),
        vol_target=float(base["rule"]["vol_target"]),
        max_leverage=float(base["rule"]["max_leverage"]),
        band=float(base["rule"]["rebalance_band"]),
        cost_bps=float(base["rule"]["trading_cost_bps"]),
    )  # fmt: skip
    same_engine = len(single) == len(ref) and max(
        abs(x[1] - y[1]) for x, y in zip(single, ref, strict=True)
    ) < 1e-12  # fmt: skip
    independent = verify_ensemble.monthly_returns(c, days, prices, yields)
    keys = ens["months"]
    worst = max(abs(independent[m] - r) for m, r in zip(keys, ens["strategy"], strict=True))
    verified = worst < 1e-12 and set(independent) >= set(keys)
    test = paired_sharpe_bootstrap(ens["strategy"], approved["strategy"], int(inf["resamples"]),
                                   int(inf["block_size_months"]), float(inf["confidence_level"]),
                                   int(inf["random_seed"]))  # fmt: skip
    digest = hashlib.sha256()
    for row in zip(days, prices, yields, strict=True):
        digest.update(repr(row).encode())
    artifact: dict[str, Any] = {
        "study": c["name"], "strategy": "trend_ensemble_v1",
        "parameters": {"rule": rule, "data": {k: str(v) for k, v in data.items()}},
        "snapshot_id": "lt-data-v1:sha256:" + digest.hexdigest(),
        "trading_enabled": False, "strategy_monthly_returns": ens["strategy"],
    }  # fmt: skip
    mt = record_and_assess(args.registry, artifact, ["^GSPC"])
    m_ens, m_app = lt.metrics(ens["strategy"]), lt.metrics(approved["strategy"])
    fails = []
    if not test.get("interval") or test["interval"][0] <= 0:
        fails.append("夏普差 95% 置信区间下限不为正")
    if m_ens["worst_rolling_12m_return"] < -float(c["risk"]["max_worst_12m_loss"]):
        fails.append(f"最差滚动 12 个月 {m_ens['worst_rolling_12m_return']:.1%} 超过上限")
    if mt["deflated_sharpe"] is None or mt["deflated_sharpe"] < 0.95:
        fails.append(f"Deflated Sharpe {mt['deflated_sharpe']} < 0.95")
    if not (verified and same_engine):
        fails.append("独立实现或基准引擎核对不一致")
    sub = {}
    for lo, hi in c.get("reporting", {}).get("subperiods", []):
        x, y = date.fromisoformat(str(lo)), date.fromisoformat(str(hi))
        e2, b2 = window(ens_rows, x, y), lt.run(base, days, prices, yields, start=x, end=y)
        sub[f"{x.year}-{y.year}"] = {
            "ensemble": lt.metrics(e2["strategy"]), "approved": lt.metrics(b2["strategy"]),
            "sharpe_diff": paired_sharpe_bootstrap(e2["strategy"], b2["strategy"], 2000,
                                                   int(inf["block_size_months"]), 0.95,
                                                   int(inf["random_seed"]))["sharpe_difference"],
        }  # fmt: skip
    sma_only = window(run_ensemble(rule, days, prices, yields, momentum=False), a, b)
    artifact.update(
        metrics={"ensemble": m_ens, "approved": m_app,
                 "sp500_total_return": lt.metrics(approved["benchmark"])},
        activity={"ensemble": {"avg_exposure": ens["average_exposure"],
                               "adjustments_per_year": ens["switches_per_year"]},
                  "approved": {"avg_exposure": approved["average_exposure"],
                               "adjustments_per_year": approved["switches_per_year"]}},
        sharpe_test=test, subperiods=sub,
        variants={"sma_only": lt.metrics(sma_only["strategy"])},
        verification={"independent_max_abs_diff": worst, "independent_match": verified,
                      "single_vote_equals_approved_engine": same_engine},
        multiple_testing=mt, failed_criteria=fails,
    )  # fmt: skip
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n")
    print(json.dumps({k: v for k, v in artifact.items() if k != "strategy_monthly_returns"},
                     indent=2, ensure_ascii=False, default=str))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
