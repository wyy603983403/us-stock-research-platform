"""Daily order intent for a ``sleeve_mix`` study (approved ``vt_plus_defensive``; files only).

The research model is replayed every day from ``--model-start`` on the stored adjusted closes:

- aggressive sleeve: the approved volatility-target rule on SPY (exposure held until the target
  leaves the rebalance band or the trend flips), implemented as SPY / SSO (2x) / BIL;
- defensive sleeve: TLT, IEF, GLD in equal slots, each in its fund while above the 200-day
  average and in BIL otherwise;
- at each month's last trading day both sleeves go back to their target weights (50/50) and the
  slots to equal parts.

The model's weights on the signal day are the target. Orders are written only when the model
trades that day (start, exposure change, slot switch, month-end) or the holdings have drifted
more than ``--drift`` from the target in any fund; otherwise one ``no_change`` line goes to the
audit log.
Same protections as ``usr-lt-intent``: stale data, failed quality checks or the drawdown breaker
switch to reduce-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.calendar import is_trading_day
from us_stock_research.research.leveraged_trend import load_lt_contract
from us_stock_research.research.sleeve_mix import load_mix_contract
from us_stock_research.trading.lt_intent import SIDE_CN, exposure_weights, vol_target_exposure
from us_stock_research.trading.order_intent import (
    BREAKER_DRAWDOWN,
    QUALITY_WINDOW_DAYS,
    Holdings,
    build_orders,
    last_trading_day,
    load_holdings,
    render_review,
    write_outputs,
)

DRIFT_LIMIT = 0.05
MAX_CASH_BUFFER = 0.05


def month_end(day: date) -> bool:
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt.month != day.month


def model_path(
    adj: dict[str, dict[date, float]],
    days: list[date],
    start: date,
    vt_rule: dict[str, Any],
    mix_rule: dict[str, Any],
    slots: list[str],
    *,
    one_x: str = "SPY",
    leveraged: str = "SSO",
    cash: str = "BIL",
) -> list[dict[str, Any]]:
    """Replay the model from ``start``: one row per day with target weights and model events.

    Decisions at a day's close set the weights that earn the following days' returns. Values use
    the funds' adjusted closes (the implementable portfolio), so tracking compares like with like.
    """
    sma = int(mix_rule["sma_days"])
    band = float(vt_rule["rebalance_band"])
    wa = float(mix_rule["aggressive_weight"])
    spy = [adj[one_x][d] for d in days]

    def above(sym: str, i: int) -> bool:
        series = [adj[sym][d] for d in days[i - sma + 1 : i + 1]]
        return series[-1] > sum(series) / sma

    rows: list[dict[str, Any]] = []
    first = next(i for i, d in enumerate(days) if d >= start)
    e_held = vol_target_exposure(spy[: first + 1], vt_rule)
    on = {s: above(s, first) for s in slots}
    v_a, v_slot = wa, {s: (1 - wa) / len(slots) for s in slots}
    events = ["start"]
    for i in range(first, len(days)):
        d = days[i]
        if i > first:
            prev = days[i - 1]

            def ret(sym: str, a: date = prev, b: date = d) -> float:
                return adj[sym][b] / adj[sym][a] - 1

            v_a *= 1 + sum(w * ret(s) for s, w in exposure_weights(
                e_held, one_x, leveraged, cash).items())  # fmt: skip
            for s in slots:
                v_slot[s] *= 1 + ret(s if on[s] else cash)
            events = []
            aim = vol_target_exposure(spy[: i + 1], vt_rule)
            if (aim == 0) != (e_held == 0) or abs(aim - e_held) > band:
                e_held = aim
                events.append("aggressive")
            for s in slots:
                now = above(s, i)
                if now != on[s]:
                    on[s] = now
                    events.append(f"{s} {'on' if now else 'off'}")
        total = v_a + sum(v_slot.values())
        if month_end(d):
            v_a = wa * total
            v_slot = {s: (1 - wa) * total / len(slots) for s in slots}
            events.append("month_end")
        weights: dict[str, float] = {}
        for s, w in exposure_weights(e_held, one_x, leveraged, cash).items():
            weights[s] = weights.get(s, 0.0) + w * v_a / total
        for s in slots:
            key = s if on[s] else cash
            weights[key] = weights.get(key, 0.0) + v_slot[s] / total
        rows.append(
            {
                "day": d,
                "value": total,
                "weights": {s: round(w, 6) for s, w in sorted(weights.items()) if w > 1e-9},
                "events": events,
                "exposure_held": e_held,
                "aggressive_share": v_a / total,
                "slots_on": dict(on),
            }
        )
    return rows


def replica_index(
    path: list[dict[str, Any]], adj: dict[str, dict[date, float]], drift_limit: float = DRIFT_LIMIT
) -> dict[date, float]:
    """Value of a frictionless copy of the live portfolio, for tracking.

    Same timing as the live system: target weights decided at a day's close are traded at the next
    day's close (fractional units, no cost), and only on days the live system would trade (a model
    event or drift beyond ``drift_limit``). Before the first fill the copy is all cash at 1.0.
    """
    out: dict[date, float] = {}
    units: dict[str, float] = {}
    value = 1.0
    pending: dict[str, float] | None = None
    for row in path:
        d = row["day"]
        if units:
            value = sum(n * adj[s][d] for s, n in units.items())
        if pending is not None:  # yesterday's list fills at today's close
            units = {s: w * value / adj[s][d] for s, w in pending.items()}
            pending = None
        out[d] = value
        weights = row["weights"]
        current = {s: n * adj[s][d] / value for s, n in units.items()} if units else {}
        drift = max(
            (abs(weights.get(s, 0.0) - current.get(s, 0.0)) for s in set(weights) | set(current)),
            default=0.0,
        )
        if row["events"] or drift > drift_limit:
            pending = dict(weights)
    return out


def holding_weights(holdings: Holdings, prices: dict[str, float]) -> dict[str, float]:
    value = {s: n * prices.get(s, 0.0) for s, n in holdings.positions.items()}
    nav = holdings.cash_usd + sum(value.values())
    return {s: v / nav for s, v in value.items()} if nav > 0 else {}


def generate(
    contract: dict[str, Any],
    vt_contract: dict[str, Any],
    bars: dict[str, list[Any]],
    holdings: Holdings,
    as_of: date,
    model_start: date,
    *,
    min_trade_usd: float = 100.0,
    quality: Any = None,
    breaker: float = BREAKER_DRAWDOWN,
    drift_limit: float = DRIFT_LIMIT,
    cash_buffer: float = 0.0,
) -> dict[str, Any]:
    if not 0.0 <= cash_buffer <= MAX_CASH_BUFFER:
        raise ValueError(f"cash buffer {cash_buffer} outside 0..{MAX_CASH_BUFFER}")
    day = last_trading_day(as_of)
    slots = [str(s) for s in contract["data"]["defensive_assets"]]
    reasons: list[str] = []
    upto = {s: [b for b in rows if b.day <= day] for s, rows in bars.items()}
    for s, rows in upto.items():
        if not rows or rows[-1].day != day:
            last = rows[-1].day if rows else None
            reasons.append(f"{s} data ends {last}, expected {day}: stale")
    if quality is not None:
        for s, rows in upto.items():
            errors = quality(s, [b for b in rows if b.day >= day - timedelta(QUALITY_WINDOW_DAYS)])
            if errors:
                reasons.append(f"{s} quality: {errors[0]}")
    adj = {s: {b.day: b.adj_close for b in rows if b.adj_close > 0} for s, rows in upto.items()}
    days = sorted(set.intersection(*(set(v) for v in adj.values())))
    path = model_path(adj, days, model_start, vt_contract["rule"], contract["rule"], slots)
    today = path[-1]
    prices = {s: rows[-1].close for s, rows in upto.items() if rows}
    weights = today["weights"]
    current = holding_weights(holdings, prices)
    drift = max(
        (abs(weights.get(s, 0.0) - current.get(s, 0.0)) for s in set(weights) | set(current)),
        default=0.0,
    )
    triggers = list(today["events"])
    if drift > drift_limit:
        triggers.append(f"drift {drift:.1%}")
    nav_now = holdings.cash_usd + sum(n * prices.get(s, 0.0) for s, n in holdings.positions.items())
    if holdings.peak_nav_usd and nav_now < holdings.peak_nav_usd * (1 - breaker):
        reasons.append(
            f"circuit breaker: NAV {nav_now:,.0f} is more than {breaker:.0%} "
            f"below peak {holdings.peak_nav_usd:,.0f}"
        )
    reduce_only = bool(reasons)
    # buys are sized at the signal close but filled a day later: a small cash buffer keeps the
    # manual cash account from coming up short when prices rise (sizing only; drift uses targets)
    sized = {s: w * (1 - cash_buffer) for s, w in weights.items()} if cash_buffer else weights
    orders, nav = build_orders(
        sized, holdings, prices, reduce_only=reduce_only, min_trade_usd=min_trade_usd
    )
    if not triggers:
        orders = []
    approved = contract.get("status") == "promoted" and (contract.get("human_review") or {}).get(
        "approved"
    )
    sma_days = int(vt_contract["rule"]["sma_days"])
    spy = [b.adj_close for b in upto["SPY"]]
    return {
        "study": contract["name"],
        "strategy": "sleeve_mix_v1",
        "mode": "live-candidate" if approved else "rehearsal",
        "rehearsal_reason": None
        if approved
        else f"study status is {contract.get('status')!r} without human approval; "
        "pipeline test only",
        "trading_enabled": False,
        "as_of": as_of.isoformat(),
        "signal_day": day.isoformat(),
        "model_start": model_start.isoformat(),
        "data_last_day": min(rows[-1].day for rows in upto.values() if rows).isoformat(),
        "signal": {
            "symbol": "SPY",
            "close": round(spy[-1], 4),
            "sma": round(sum(spy[-sma_days:]) / sma_days, 4),
            "trend_on": today["exposure_held"] > 0,
            "exposure": {
                "target": round(vol_target_exposure(spy, vt_contract["rule"]), 4),
                "held_by_model": round(today["exposure_held"], 4),
            },
            "aggressive_share": round(today["aggressive_share"], 4),
            "slots_on": today["slots_on"],
            "model_events": today["events"],
            "triggers": triggers,
            "max_drift": round(drift, 4),
        },
        "reduce_only": reduce_only,
        "reduce_only_reasons": reasons,
        "nav_usd": round(nav, 2),
        "target_weights": weights,
        "cash_buffer": cash_buffer,
        "orders": orders,
        "one_way_turnover": round(sum(o["est_value_usd"] for o in orders) / nav, 4) if nav else 0,
        "git_sha": None,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def summary_line(intent: dict[str, Any]) -> str:
    sig = intent["signal"]
    slots = "，".join(f"{s} {'持有' if v else '转国库券'}" for s, v in sig["slots_on"].items())
    orders = intent["orders"]
    return (
        f"{intent['signal_day']}：SPY {sig['close']:.2f} "
        f"{'在均线上方' if sig['trend_on'] else '在均线下方'}（{sig['sma']:.2f}），"
        f"进攻部分敞口 {sig['exposure']['held_by_model']:.2f} 倍、"
        f"占 {sig['aggressive_share']:.0%}；"
        f"{slots}；订单 {len(orders)} 笔"
        + (
            "："
            + "；".join(
                f"{SIDE_CN.get(o['side'], o['side'])} {o['symbol']} {o['shares']} 股"
                f"（参考价 {o['ref_price']:.2f}，约 ${o['est_value_usd']:,.0f}）"
                for o in orders
            )
            if orders
            else ""
        )
        + (f"（触发：{'、'.join(sig['triggers'])}）" if orders else "")
        + (f"，只减仓：{intent['reduce_only_reasons'][0]}" if intent["reduce_only"] else "")
    )


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.quality.ohlcv import audit_bars
    from us_stock_research.storage import open_store
    from us_stock_research.trading.order_intent import _git_sha

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--model-start", type=date.fromisoformat, required=True)
    parser.add_argument("--holdings", type=Path, help="holdings or rehearsal ledger YAML")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--out-dir", type=Path, default=Path("orders"))
    parser.add_argument("--breaker", type=float, default=BREAKER_DRAWDOWN)
    parser.add_argument("--drift", type=float, default=DRIFT_LIMIT)
    parser.add_argument(
        "--cash-buffer", type=float, default=0.0, help="size buys to leave this share in cash"
    )
    args = parser.parse_args(argv)
    contract = load_mix_contract(args.contract)
    vt_contract = load_lt_contract(Path(contract["data"]["aggressive_sleeve"]))
    store = open_store(load_settings())
    holdings = load_holdings(args.holdings, args.capital)
    symbols = {"SPY", "SSO", "BIL", *contract["data"]["defensive_assets"], *holdings.positions}
    bars = {s: store.read_bars(s) for s in sorted(symbols)}
    intent = generate(
        contract,
        vt_contract,
        bars,
        holdings,
        args.as_of,
        args.model_start,
        quality=lambda s, b: [str(e) for e in audit_bars(s, b).errors],
        breaker=args.breaker,
        drift_limit=args.drift,
        cash_buffer=args.cash_buffer,
    )
    intent["git_sha"] = _git_sha()
    if intent["orders"]:
        path = write_outputs(intent, args.out_dir)
        print(render_review(intent))
        print(f"写入 {path}")
    else:
        text = json.dumps(intent, ensure_ascii=False)
        entry = {
            "created_at": intent["created_at"],
            "type": "no_change",
            "study": intent["study"],
            "signal_day": intent["signal_day"],
            "triggers": intent["signal"]["triggers"],
            "reduce_only": intent["reduce_only"],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        folder = args.out_dir / intent["study"]
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "audit_log.jsonl").open("a") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(summary_line(intent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
