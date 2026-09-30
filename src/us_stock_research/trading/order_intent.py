"""Roadmap stage 1: turn a frozen study into a month-end order list. Files only, never orders.

For one study contract it
1. reads the latest stored daily bars of the contract universe (not the frozen snapshot: the
   snapshot is the research record, this is live use of the same rule);
2. checks freshness (the latest common day must be the latest NYSE trading day on or before
   ``--as-of``) and runs the daily quality gate on the recent window;
3. computes target weights with the exact same ``target_weights`` function the backtest uses,
   on the last trading day of the month;
4. compares them with the holdings file and writes an order list (sells first, whole shares,
   small trades skipped) as JSON + a Markdown review sheet, and appends a line to an append-only
   audit log.

Protections from ``docs/trading-roadmap.md``:
* a study that is not ``promoted`` with human approval yields a *rehearsal* order list, marked as
  such on every line, meant only for testing the pipeline;
* stale data or a failed quality gate switches to **reduce-only** (sell orders only);
* a drawdown of the holdings from ``peak_nav_usd`` beyond the breaker threshold also switches to
  reduce-only;
* ``trading_enabled`` is always false here; nothing talks to a broker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.bars import DailyBar
from us_stock_research.calendar import is_trading_day
from us_stock_research.config import load_settings
from us_stock_research.quality.ohlcv import audit_bars
from us_stock_research.research.backtest import CASH, align, target_weights
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.snapshots import Bundle
from us_stock_research.storage import BarStore, open_store

BREAKER_DRAWDOWN = 0.15  # roadmap default: stop new buys after a 15% fall from the peak
QUALITY_WINDOW_DAYS = 90


@dataclass
class Holdings:
    cash_usd: float
    positions: dict[str, float] = field(default_factory=dict)  # symbol -> shares
    peak_nav_usd: float | None = None
    as_of: str | None = None


def load_holdings(path: Path | None, capital: float) -> Holdings:
    """YAML: ``cash_usd``, ``positions: {SYM: shares}``, optional ``peak_nav_usd``."""
    if path is None or not path.exists():
        return Holdings(cash_usd=capital)
    raw = yaml.safe_load(path.read_text()) or {}
    return Holdings(
        cash_usd=float(raw.get("cash_usd", 0.0)),
        positions={str(k).upper(): float(v) for k, v in (raw.get("positions") or {}).items()},
        peak_nav_usd=float(raw["peak_nav_usd"]) if raw.get("peak_nav_usd") else None,
        as_of=str(raw["as_of"]) if raw.get("as_of") else None,
    )


def last_trading_day(on_or_before: date) -> date:
    day = on_or_before
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def month_end_signal_day(days: list[date], as_of: date) -> int | None:
    """Index of the last trading day of the month containing ``as_of`` if the data reaches it."""
    month_last = date(as_of.year + as_of.month // 12, as_of.month % 12 + 1, 1) - timedelta(days=1)
    target = last_trading_day(month_last)
    if target > as_of:
        return None
    return days.index(target) if target in days else None


def build_orders(
    weights: dict[str, float],
    holdings: Holdings,
    prices: dict[str, float],
    *,
    reduce_only: bool,
    min_trade_usd: float,
) -> tuple[list[dict[str, Any]], float]:
    nav = holdings.cash_usd + sum(
        shares * prices[s] for s, shares in holdings.positions.items() if s in prices
    )
    orders: list[dict[str, Any]] = []
    symbols = sorted(set(weights) | set(holdings.positions))
    for symbol in symbols:
        if symbol == CASH:
            continue
        if symbol not in prices:
            raise ValueError(f"no price for {symbol}: holdings outside the study universe?")
        price = prices[symbol]
        current = holdings.positions.get(symbol, 0.0)
        target_value = weights.get(symbol, 0.0) * nav
        delta_value = target_value - current * price
        shares = math.floor(abs(delta_value) / price)
        if delta_value < 0 and weights.get(symbol, 0.0) == 0.0:
            shares = int(current)  # full exit: sell everything, not a rounded remainder
        if shares == 0 or shares * price < min_trade_usd:
            continue
        side = "SELL" if delta_value < 0 else "BUY"
        if reduce_only and side == "BUY":
            continue
        orders.append(
            {
                "side": side,
                "symbol": symbol,
                "shares": shares,
                "ref_price": round(price, 4),
                "est_value_usd": round(shares * price, 2),
                "current_shares": current,
                "target_weight": round(weights.get(symbol, 0.0), 6),
            }
        )
    orders.sort(key=lambda o: (o["side"] != "SELL", o["symbol"]))  # sells fund the buys
    return orders, nav


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def generate(
    contract: StudyContract,
    store: BarStore,
    holdings: Holdings,
    as_of: date,
    *,
    min_trade_usd: float = 100.0,
    quality: Callable[[str, list[DailyBar]], list[str]] | None = None,
) -> dict[str, Any]:
    universe = [s.upper() for s in contract.data.universe]
    bars = {s: store.read_bars(s) for s in universe}
    dividends = {s: store.read_dividends(s) if store.has_dividends(s) else {} for s in universe}
    panel = align(Bundle(bars=bars, dividends=dividends), contract.data.start, as_of)
    reasons: list[str] = []
    latest = panel.days[-1]
    expected = last_trading_day(as_of)
    if latest != expected:
        reasons.append(f"data ends {latest}, expected {expected}: stale")
    check = quality or (lambda s, b: [str(e) for e in audit_bars(s, b).errors])
    recent_from = as_of - timedelta(days=QUALITY_WINDOW_DAYS)
    for symbol in universe:
        errors = check(symbol, [b for b in bars[symbol] if b.day >= recent_from])
        if errors:
            reasons.append(f"{symbol} quality: {errors[0]}")
    i = month_end_signal_day(panel.days, as_of)
    if i is None:
        raise ValueError(
            f"{as_of} is before this month's last trading day, or the data does not reach it; "
            "order lists are only produced at month end"
        )
    weights = target_weights(contract, panel, i)
    prices = {s: panel.closes[s][-1] for s in universe}
    nav_now = holdings.cash_usd + sum(
        sh * prices.get(s, 0.0) for s, sh in holdings.positions.items()
    )
    if holdings.peak_nav_usd and nav_now < holdings.peak_nav_usd * (1 - BREAKER_DRAWDOWN):
        reasons.append(
            f"circuit breaker: NAV {nav_now:,.0f} is more than {BREAKER_DRAWDOWN:.0%} "
            f"below peak {holdings.peak_nav_usd:,.0f}"
        )
    reduce_only = bool(reasons)
    orders, nav = build_orders(
        weights, holdings, prices, reduce_only=reduce_only, min_trade_usd=min_trade_usd
    )
    approved = contract.status == "promoted" and contract.human_review.approved
    turnover = sum(o["est_value_usd"] for o in orders) / nav if nav else 0.0
    return {
        "study": contract.name,
        "strategy": contract.strategy,
        "mode": "live-candidate" if approved else "rehearsal",
        "rehearsal_reason": None
        if approved
        else f"study status is {contract.status!r} without human approval; pipeline test only",
        "trading_enabled": False,
        "as_of": as_of.isoformat(),
        "signal_day": panel.days[i].isoformat(),
        "data_last_day": latest.isoformat(),
        "reduce_only": reduce_only,
        "reduce_only_reasons": reasons,
        "nav_usd": round(nav, 2),
        "target_weights": {k: round(v, 6) for k, v in sorted(weights.items())},
        "orders": orders,
        "one_way_turnover": round(turnover, 4),
        "git_sha": _git_sha(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def render_review(intent: dict[str, Any]) -> str:
    lines = [
        f"# 订单意向：{intent['study']}（{intent['signal_day']}）",
        "",
        "**只是文件，不会发送给任何券商。**",
        "",
        f"- 模式：{intent['mode']}"
        + (f"（{intent['rehearsal_reason']}）" if intent["rehearsal_reason"] else ""),
        f"- 数据截至：{intent['data_last_day']}；信号日：{intent['signal_day']}",
        f"- 组合净值：${intent['nav_usd']:,.2f}；单边换手：{intent['one_way_turnover']:.1%}",
        f"- 只减仓模式：{'是' if intent['reduce_only'] else '否'}",
    ]
    lines += [f"  - 原因：{r}" for r in intent["reduce_only_reasons"]]
    lines += ["", "## 目标权重", "", "| 标的 | 权重 |", "|---|---:|"]
    lines += [f"| {s} | {w:.2%} |" for s, w in intent["target_weights"].items()]
    lines += ["", "## 订单（先卖后买，整股）", ""]
    if intent["orders"]:
        lines += [
            "| 方向 | 标的 | 股数 | 参考价 | 约金额 | 现持股 |",
            "|---|---|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {o['side']} | {o['symbol']} | {o['shares']} | {o['ref_price']:.2f} "
            f"| ${o['est_value_usd']:,.2f} | {o['current_shares']:g} |"
            for o in intent["orders"]
        ]
    else:
        lines.append("无需调仓。")
    lines += [
        "",
        "## 人工复核（阶段 1 门槛：连续 3 个月与手工计算一致）",
        "",
        "- [ ] 数据截至日正确、无只减仓原因（或原因已理解）",
        "- [ ] 目标权重与手工按规则计算一致",
        "- [ ] 订单股数与现持仓、净值一致",
        "- [ ] 复核人 / 日期：",
        "",
    ]
    return "\n".join(lines)


def write_outputs(intent: dict[str, Any], out_dir: Path) -> Path:
    folder: Path = out_dir / str(intent["study"])
    folder.mkdir(parents=True, exist_ok=True)
    base: Path = folder / str(intent["signal_day"])
    text = json.dumps(intent, indent=2, ensure_ascii=False) + "\n"
    base.with_suffix(".json").write_text(text)
    base.with_suffix(".md").write_text(render_review(intent))
    entry = {
        "created_at": intent["created_at"],
        "study": intent["study"],
        "signal_day": intent["signal_day"],
        "mode": intent["mode"],
        "reduce_only": intent["reduce_only"],
        "orders": len(intent["orders"]),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "git_sha": intent["git_sha"],
    }
    with (out_dir / "audit_log.jsonl").open("a") as log:  # append-only
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return base.with_suffix(".json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--holdings", type=Path, help="YAML holdings file (default: all cash)")
    parser.add_argument("--capital", type=float, default=100_000.0, help="cash if no holdings")
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=1)
    )
    parser.add_argument("--min-trade-usd", type=float, default=100.0)
    parser.add_argument("--out-dir", type=Path, default=Path("orders"))
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    intent = generate(
        contract,
        open_store(load_settings()),
        load_holdings(args.holdings, args.capital),
        args.as_of,
        min_trade_usd=args.min_trade_usd,
    )
    path = write_outputs(intent, args.out_dir)
    print(render_review(intent))
    print(f"写入 {path} 与 {path.with_suffix('.md')}；审计日志 {args.out_dir / 'audit_log.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
