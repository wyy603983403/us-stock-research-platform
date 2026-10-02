"""Paper fills for rehearsal order lists: carry a simulated portfolio from month to month.

Roadmap stage 1 compares each month's order list with a manual review. Starting every month
from all cash would make every list a full rebuild, so this keeps a *rehearsal ledger*
(``portfolio/rehearsal/<study>.yml``, same format as the holdings file) and, after the fill day
has closed, books the orders as if executed at that day's close:

* fill day = first trading day after the signal day (the contract's one-day lag);
* sells first, then buys; costs in basis points on traded value; a buy that the cash cannot
  cover is cut to the shares it can afford (reported, never borrowed);
* cash dividends of held shares with ex-dates after the previous booking and up to the fill day
  are credited first, so the ledger's NAV includes income;
* ``peak_nav_usd`` is raised to the fill-day NAV when higher (the order tool's breaker uses it).

Nothing here talks to a broker; it writes the ledger and one audit-log line per fill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.calendar import is_trading_day

DEFAULT_COST_BPS = 5.0


def next_trading_day(day: date) -> date:
    d = day + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def load_ledger(path: Path, capital: float) -> dict[str, Any]:
    if not path.exists():
        return {"cash_usd": capital, "positions": {}, "peak_nav_usd": capital, "as_of": None,
                "filled": []}  # fmt: skip
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        "cash_usd": float(raw.get("cash_usd", 0.0)),
        "positions": {str(k).upper(): float(v) for k, v in (raw.get("positions") or {}).items()},
        "peak_nav_usd": float(raw["peak_nav_usd"]) if raw.get("peak_nav_usd") else None,
        "as_of": str(raw["as_of"]) if raw.get("as_of") else None,
        "filled": [str(d) for d in raw.get("filled") or []],
    }


def book(
    ledger: dict[str, Any],
    intent: dict[str, Any],
    fill_day: date,
    closes: dict[str, float],
    dividends: dict[str, dict[date, float]],
    cost_bps: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """New ledger and a fill report. ``closes``: fill-day close of every symbol involved."""
    if intent["signal_day"] in ledger["filled"]:
        raise ValueError(f"order list {intent['signal_day']} is already booked")
    since = date.fromisoformat(ledger["as_of"]) if ledger["as_of"] else None
    if since is not None and fill_day <= since:
        raise ValueError(f"fill day {fill_day} is not after the ledger date {since}")
    cash = float(ledger["cash_usd"])
    positions = dict(ledger["positions"])
    for order in intent["orders"]:
        held = positions.get(order["symbol"], 0.0)
        if abs(float(order.get("current_shares", 0.0)) - held) > 1e-9:
            raise ValueError(
                f"order list {intent['signal_day']} assumed {order['current_shares']:g} "
                f"{order['symbol']}, the ledger holds {held:g}: regenerate it with "
                "--holdings <ledger>"
            )
    income = 0.0
    for symbol, shares in positions.items():
        for ex, amount in (dividends.get(symbol) or {}).items():
            if (since is None or ex > since) and ex <= fill_day:
                income += shares * amount
    cash += income
    rate = cost_bps / 10_000
    fills: list[dict[str, Any]] = []
    costs = 0.0
    for order in sorted(intent["orders"], key=lambda o: o["side"] != "SELL"):
        symbol, side = order["symbol"], order["side"]
        price = closes[symbol]
        shares = float(order["shares"])
        if side == "SELL":
            shares = min(shares, positions.get(symbol, 0.0))
            value = shares * price
            cash += value - value * rate
            positions[symbol] = positions.get(symbol, 0.0) - shares
        else:
            affordable = int(cash // (price * (1 + rate)))
            if shares > affordable:
                fills.append({"symbol": symbol, "note": f"cut {shares:g} -> {affordable} (cash)"})
                shares = float(affordable)
            value = shares * price
            cash -= value + value * rate
            positions[symbol] = positions.get(symbol, 0.0) + shares
        costs += value * rate
        fills.append({"side": side, "symbol": symbol, "shares": shares, "price": round(price, 4)})
    positions = {s: n for s, n in sorted(positions.items()) if n > 0}
    missing = [s for s in positions if s not in closes]
    if missing:
        raise ValueError(f"no fill-day close for held {missing}")
    nav = cash + sum(n * closes[s] for s, n in positions.items())
    peak = max(float(ledger["peak_nav_usd"] or nav), nav)
    new = {
        "cash_usd": round(cash, 2),
        "positions": positions,
        "peak_nav_usd": round(peak, 2),
        "as_of": fill_day.isoformat(),
        "filled": [*ledger["filled"], intent["signal_day"]],
    }
    report = {
        "study": intent["study"],
        "signal_day": intent["signal_day"],
        "fill_day": fill_day.isoformat(),
        "dividends_usd": round(income, 2),
        "costs_usd": round(costs, 2),
        "nav_usd": round(nav, 2),
        "fills": fills,
    }
    return new, report


def dump_ledger(ledger: dict[str, Any]) -> str:
    head = "# 演练账本（模拟成交，不是真实持仓；由 usr-rehearsal-fill 维护）\n"
    return head + str(yaml.safe_dump(ledger, allow_unicode=True, sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.quality.intraday import last_closed_session
    from us_stock_research.storage import open_store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", default="etf_trend_baseline")
    parser.add_argument("--orders-dir", type=Path, default=Path("orders"))
    parser.add_argument("--ledger", type=Path, help="default portfolio/rehearsal/<study>.yml")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument(
        "--since", type=date.fromisoformat, help="ignore order lists with earlier signal days"
    )
    args = parser.parse_args(argv)
    ledger_path = args.ledger or Path("portfolio/rehearsal") / f"{args.study}.yml"
    ledger = load_ledger(ledger_path, args.capital)
    store = open_store(load_settings())
    booked = []
    closed = last_closed_session(datetime.now(UTC))
    for path in sorted((args.orders_dir / args.study).glob("*.json")):
        intent = json.loads(path.read_text())
        if intent["signal_day"] in ledger["filled"]:
            continue
        if args.since and date.fromisoformat(intent["signal_day"]) < args.since:
            continue
        if ledger["as_of"] and intent["signal_day"] < ledger["as_of"]:
            print(f"跳过 {path.name}：早于账本日期 {ledger['as_of']}", file=sys.stderr)
            continue
        fill_day = next_trading_day(date.fromisoformat(intent["signal_day"]))
        if fill_day > closed:  # a bar for a session still trading is not a close
            print(f"{intent['signal_day']}：成交日 {fill_day} 尚未收盘，稍后再记账")
            break
        symbols = {o["symbol"] for o in intent["orders"]} | set(ledger["positions"])
        closes: dict[str, float] = {}
        dividends: dict[str, dict[date, float]] = {}
        for s in symbols:
            bar = next((b for b in store.read_bars(s) if b.day == fill_day), None)
            if bar is not None:
                closes[s] = bar.close
            dividends[s] = store.read_dividends(s) if store.has_dividends(s) else {}
        if len(closes) < len(symbols):
            print(f"{intent['signal_day']}：成交日 {fill_day} 还没有收盘数据，稍后再记账")
            break
        ledger, report = book(ledger, intent, fill_day, closes, dividends, args.cost_bps)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        text = dump_ledger(ledger)
        ledger_path.write_text(text)
        entry = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "type": "rehearsal_fill",
            **{k: report[k] for k in ("study", "signal_day", "fill_day", "nav_usd")},
            "ledger_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        with (args.orders_dir / "audit_log.jsonl").open("a") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        booked.append(report)
    print(json.dumps({"ledger": str(ledger_path), "booked": booked}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
