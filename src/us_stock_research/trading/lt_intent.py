"""Daily order intent for a ``leveraged_trend`` study (rehearsal only; files, never orders).

The study checks its signal every trading day, so unlike the month-end tool this one runs after
each close: SPY above its 200-day average -> hold the leveraged ETF named in ``--risk-on``
(SSO, the 2x S&P 500 ETF the leverage model was validated against), otherwise the T-bill ETF
named in ``--risk-off`` (BIL). An order list is written only when the target differs from the
holdings (about six switches a year), and every run appends one line to the audit log.

Same protections as ``usr-order-intent``: stale data or a failed quality check, and a fall of
more than 15% from ``peak_nav_usd``, switch to reduce-only (sell orders only); a study that is not
promoted with human approval yields a rehearsal file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.research.leveraged_trend import load_lt_contract
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


def trend_on(closes: list[float], sma_days: int) -> bool:
    if len(closes) < sma_days:
        raise ValueError(f"need {sma_days} closes, have {len(closes)}")
    return closes[-1] > sum(closes[-sma_days:]) / sma_days


def vol_target_exposure(closes: list[float], rule: dict[str, Any]) -> float:
    """Target exposure of the volatility-target rule from adjusted closes up to the signal day."""
    if not trend_on(closes, int(rule["sma_days"])):
        return 0.0
    n = int(rule["vol_window_days"])
    if len(closes) < n + 1:
        raise ValueError(f"need {n + 1} closes for volatility")
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    vol = (sum((r - mean) ** 2 for r in rets) / (n - 1) * 252) ** 0.5
    cap = float(rule["max_leverage"])
    return cap if vol <= 0 else min(cap, float(rule["vol_target"]) / vol)


def exposure_weights(exposure: float, one_x: str, leveraged: str, cash: str) -> dict[str, float]:
    """Exposure in [0, 2] with a 1x fund, a 2x fund and T-bills: never both T-bills and 2x."""
    if exposure <= 1:
        w = {one_x: exposure, cash: 1 - exposure}
    else:
        w = {leveraged: exposure - 1, one_x: 2 - exposure}
    return {s: round(v, 6) for s, v in w.items() if v > 1e-9}


def current_exposure(
    holdings: Holdings, prices: dict[str, float], one_x: str, leveraged: str
) -> float:
    value = {s: n * prices.get(s, 0.0) for s, n in holdings.positions.items()}
    nav = holdings.cash_usd + sum(value.values())
    if nav <= 0:
        return 0.0
    return (value.get(one_x, 0.0) + 2 * value.get(leveraged, 0.0)) / nav


def generate(
    contract: dict[str, Any],
    bars: dict[str, list[Any]],
    holdings: Holdings,
    as_of: date,
    *,
    risk_on: str,
    risk_off: str,
    one_x: str = "SPY",
    signal: str | None = None,
    min_trade_usd: float = 100.0,
    quality: Any = None,
    breaker: float = BREAKER_DRAWDOWN,
) -> dict[str, Any]:
    signal_symbol = signal or contract["data"]["signal_and_asset"]
    day = last_trading_day(as_of)
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
    closes = [b.adj_close for b in upto[signal_symbol]]
    rule = contract["rule"]
    prices = {s: rows[-1].close for s, rows in upto.items() if rows}
    exposure: dict[str, float] | None = None
    hold = False
    if "vol_target" in rule:
        aim = vol_target_exposure(closes, rule)
        now = current_exposure(holdings, prices, one_x, risk_on)
        on = aim > 0
        invested = any(n > 0 for n in holdings.positions.values())
        hold = (
            invested
            and (aim == 0) == (now < 1e-6)
            and abs(aim - now) <= float(rule["rebalance_band"])
        )
        exposure = {"target": round(aim, 4), "current": round(now, 4)}
        weights = exposure_weights(aim, one_x, risk_on, risk_off)
    else:
        on = trend_on(closes, int(rule["sma_days"]))
        weights = {risk_on if on else risk_off: 1.0}
    nav_now = holdings.cash_usd + sum(n * prices.get(s, 0.0) for s, n in holdings.positions.items())
    if holdings.peak_nav_usd and nav_now < holdings.peak_nav_usd * (1 - breaker):
        reasons.append(
            f"circuit breaker: NAV {nav_now:,.0f} is more than {breaker:.0%} "
            f"below peak {holdings.peak_nav_usd:,.0f}"
        )
    reduce_only = bool(reasons)
    orders, nav = build_orders(
        weights, holdings, prices, reduce_only=reduce_only, min_trade_usd=min_trade_usd
    )
    if hold:  # within the rebalance band: keep the current mix
        orders = []
    approved = contract.get("status") == "promoted" and (contract.get("human_review") or {}).get(
        "approved"
    )
    sma = sum(closes[-int(contract["rule"]["sma_days"]) :]) / int(contract["rule"]["sma_days"])
    return {
        "study": contract["name"],
        "strategy": "leveraged_trend_v1",
        "mode": "live-candidate" if approved else "rehearsal",
        "rehearsal_reason": None
        if approved
        else f"study status is {contract.get('status')!r} without human approval; "
        "pipeline test only",
        "trading_enabled": False,
        "as_of": as_of.isoformat(),
        "signal_day": day.isoformat(),
        "data_last_day": min(rows[-1].day for rows in upto.values() if rows).isoformat(),
        "signal": {
            "symbol": signal_symbol,
            "close": round(closes[-1], 4),
            "sma": round(sma, 4),
            "trend_on": on,
            **({"exposure": exposure} if exposure else {}),
        },
        "reduce_only": reduce_only,
        "reduce_only_reasons": reasons,
        "nav_usd": round(nav, 2),
        "target_weights": weights,
        "orders": orders,
        "one_way_turnover": round(sum(o["est_value_usd"] for o in orders) / nav, 4) if nav else 0,
        "git_sha": None,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.quality.ohlcv import audit_bars
    from us_stock_research.storage import open_store
    from us_stock_research.trading.order_intent import _git_sha

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--holdings", type=Path, help="holdings or rehearsal ledger YAML")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--risk-on", default="SSO")
    parser.add_argument("--risk-off", default="BIL")
    parser.add_argument("--one-x", default="SPY", help="1x fund (volatility-target rule)")
    parser.add_argument(
        "--signal-symbol",
        help="adjusted closes for the signal (default: the contract's; SPY for an index study)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("orders"))
    parser.add_argument(
        "--breaker",
        type=float,
        default=BREAKER_DRAWDOWN,
        help="drawdown from peak NAV that switches to reduce-only (roadmap default 0.15; "
        "changing it is the user's decision)",
    )
    args = parser.parse_args(argv)
    contract = load_lt_contract(args.contract)
    store = open_store(load_settings())
    signal_symbol = args.signal_symbol or contract["data"]["signal_and_asset"]
    symbols = {signal_symbol, args.risk_on, args.risk_off}
    if "vol_target" in contract["rule"]:
        symbols.add(args.one_x)
    holdings = load_holdings(args.holdings, args.capital)
    symbols |= set(holdings.positions)
    bars = {s: store.read_bars(s) for s in sorted(symbols)}
    intent = generate(
        contract,
        bars,
        holdings,
        args.as_of,
        risk_on=args.risk_on,
        risk_off=args.risk_off,
        one_x=args.one_x,
        signal=signal_symbol,
        quality=lambda s, b: [str(e) for e in audit_bars(s, b).errors],
        breaker=args.breaker,
    )
    intent["git_sha"] = _git_sha()
    sig = intent["signal"]
    state = "在均线上方" if sig["trend_on"] else "在均线下方"
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
            "trend_on": sig["trend_on"],
            "reduce_only": intent["reduce_only"],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        args.out_dir.mkdir(parents=True, exist_ok=True)
        with (args.out_dir / "audit_log.jsonl").open("a") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(
        f"{intent['signal_day']}：{sig['symbol']} {sig['close']:.2f} {state}（{sig['sma']:.2f}），"
        + (
            "目标敞口 {target:.2f} 倍（当前 {current:.2f}），".format(**sig["exposure"])
            if "exposure" in sig
            else ""
        )
        + f"目标 {' '.join(f'{s} {w:.0%}' for s, w in intent['target_weights'].items())}，"
        f"订单 {len(intent['orders'])} 笔"
        + (f"，只减仓：{intent['reduce_only_reasons'][0]}" if intent["reduce_only"] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
