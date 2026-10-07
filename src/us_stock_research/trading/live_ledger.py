"""Manual real-money ledger (Schwab, executed by the user in the app) -- ``usr-live``.

The system never reaches the brokerage account. The user funds the account, initializes this
ledger once, places the orders from ``orders/live/<study>/<day>.md`` in the app, and records each
fill here, so holdings, slippage against the close and tracking against the model stay measurable.

    usr-live init --cash 10000                       # once, after funding
    usr-live fill --day 2026-10-30 --symbol SSO --side BUY --qty 69 --price 71.80 [--fee 0]
    usr-live cash --amount 12.34 --note "BIL dividend (net of withholding)"
    usr-live show

Ledger: ``portfolio/live/schwab.yml`` (same shape as the rehearsal ledger, so ``usr-mix-intent``
and ``usr-status`` read it unchanged); fills: ``portfolio/live/fills.csv``.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

LEDGER = Path("portfolio/live/schwab.yml")
FILLS = Path("portfolio/live/fills.csv")
FILL_COLUMNS = ["recorded_at", "day", "symbol", "side", "qty", "price", "fee", "close",
                "slippage_bps", "note"]  # fmt: skip


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"还没有实盘账本 {path}：入金后先运行 usr-live init --cash 金额")
    return dict(yaml.safe_load(path.read_text()) or {})


def save(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger["positions"] = {s: q for s, q in sorted(ledger.get("positions", {}).items()) if q}
    path.write_text(yaml.safe_dump(ledger, allow_unicode=True, sort_keys=False))


def init(path: Path, cash: float, today: date, cap: float | None = None) -> dict[str, Any]:
    if path.exists():
        raise ValueError(f"{path} 已存在；如需重来请先手动改名备份")
    if not 0 < cash <= 1_000_000:
        raise ValueError("金额不合理")
    if cap is not None and cash > cap:
        raise ValueError(
            f"超过 configs/live.yml 的首批上限 ${cap:,.0f}；加大资金需你先改该配置并记录日期"
        )
    ledger = {"account": "schwab-international (manual)", "as_of": today.isoformat(),
              "cash_usd": round(cash, 2), "positions": {}, "peak_nav_usd": round(cash, 2),
              "funded_usd": round(cash, 2)}  # fmt: skip
    save(path, ledger)
    return ledger


def fill(
    path: Path,
    fills: Path,
    *,
    day: date,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    close: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    ledger = load(path)
    side = side.upper()
    if side not in ("BUY", "SELL") or qty <= 0 or price <= 0 or fee < 0:
        raise ValueError("方向须为 BUY/SELL，股数、价格须为正")
    pos = dict(ledger.get("positions") or {})
    have = float(pos.get(symbol, 0.0))
    if side == "SELL" and qty > have + 1e-9:
        raise ValueError(f"卖出 {qty:g} 股 {symbol}，但账本只有 {have:g} 股")
    sign = 1 if side == "BUY" else -1
    pos[symbol] = have + sign * qty
    ledger["positions"] = pos
    ledger["cash_usd"] = round(float(ledger["cash_usd"]) - sign * qty * price - fee, 2)
    ledger["as_of"] = day.isoformat()
    save(path, ledger)
    slip = None
    if close:
        slip = (price / close - 1) * 10_000 * (1 if side == "BUY" else -1)  # positive = worse
    new = not fills.exists()
    fills.parent.mkdir(parents=True, exist_ok=True)
    with fills.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FILL_COLUMNS)
        w.writerow([datetime.now(UTC).isoformat(timespec="seconds"), day.isoformat(), symbol,
                    side, f"{qty:g}", f"{price:.4f}", f"{fee:.2f}",
                    "" if close is None else f"{close:.4f}",
                    "" if slip is None else f"{slip:.1f}", note])  # fmt: skip
    return {"ledger": ledger, "slippage_bps": slip}


def adjust_cash(path: Path, amount: float, note: str) -> dict[str, Any]:
    ledger = load(path)
    ledger["cash_usd"] = round(float(ledger["cash_usd"]) + amount, 2)
    log = list(ledger.get("cash_adjustments") or [])
    log.append({"at": datetime.now(UTC).date().isoformat(), "amount": round(amount, 2),
                "note": note})  # fmt: skip
    ledger["cash_adjustments"] = log[-200:]
    save(path, ledger)
    return ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--cash", type=float, required=True)
    p_fill = sub.add_parser("fill")
    p_fill.add_argument("--day", type=date.fromisoformat, required=True)
    p_fill.add_argument("--symbol", required=True)
    p_fill.add_argument("--side", required=True)
    p_fill.add_argument("--qty", type=float, required=True)
    p_fill.add_argument("--price", type=float, required=True)
    p_fill.add_argument("--fee", type=float, default=0.0)
    p_fill.add_argument("--note", default="")
    p_cash = sub.add_parser("cash")
    p_cash.add_argument("--amount", type=float, required=True)
    p_cash.add_argument("--note", required=True)
    sub.add_parser("show")
    for p in (parser,):
        p.add_argument("--ledger", type=Path, default=LEDGER)
        p.add_argument("--fills", type=Path, default=FILLS)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            conf = Path("configs/live.yml")
            raw = yaml.safe_load(conf.read_text()) if conf.exists() else None
            cap = (raw or {}).get("max_capital_usd")
            init(args.ledger, args.cash, datetime.now(UTC).date(), float(cap) if cap else None)
            print(f"实盘账本已建立：现金 ${args.cash:,.2f}（{args.ledger}）")
        elif args.cmd == "fill":
            close = _close(args.symbol.upper(), args.day)
            out = fill(args.ledger, args.fills, day=args.day, symbol=args.symbol.upper(),
                       side=args.side, qty=args.qty, price=args.price, fee=args.fee,
                       close=close, note=args.note)  # fmt: skip
            slip = out["slippage_bps"]
            sym = args.symbol.upper()
            head = f"已记录：{args.side.upper()} {sym} {args.qty:g} 股 @ {args.price}"
            vs = f"，相对当日收盘 {slip:+.1f} bp（正数=比收盘价差）" if slip is not None else ""
            print(head + vs + f"；现金 ${out['ledger']['cash_usd']:,.2f}")
        elif args.cmd == "cash":
            led = adjust_cash(args.ledger, args.amount, args.note)
            print(f"现金调整 {args.amount:+,.2f}（{args.note}）；现金 ${led['cash_usd']:,.2f}")
        else:
            led = load(args.ledger)
            print(yaml.safe_dump(led, allow_unicode=True, sort_keys=False))
    except ValueError as exc:
        print(f"未执行：{exc}")
        return 2
    return 0


def _close(symbol: str, day: date) -> float | None:
    """The stored close of the fill day (for slippage); None when not stored yet."""
    try:
        from us_stock_research.config import load_settings
        from us_stock_research.storage import open_store

        bars = open_store(load_settings()).read_bars(symbol)
    except Exception:  # noqa: BLE001 - slippage is informative only
        return None
    return next((b.close for b in bars if b.day == day), None)


if __name__ == "__main__":
    sys.exit(main())
