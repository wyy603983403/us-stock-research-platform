"""Roadmap stage 2: send an approved study's order lists to an Alpaca **paper** account.

The user chose Alpaca paper trading for stage 2 on 2026-10-03. Safeguards:

* the base URL is the paper endpoint and cannot be changed; there is no live code path;
* separate keys ``ALPACA_PAPER_KEY_ID`` / ``ALPACA_PAPER_SECRET_KEY`` (not the market-data keys);
* nothing is submitted unless ``configs/paper_broker.yml`` has ``enabled: true`` -- the user sets it
  once stage 1 (three months of matching reviews) has passed;
* reconciliation first: if the paper positions differ from what the previous submission expected,
  no new orders are sent (roadmap: stop on a reconciliation break);
* orders are market-on-close (``time_in_force: cls``) for the fill day, the backtest's
  "signal day close -> next close" timing; each carries a deterministic ``client_order_id`` so a
  rerun never sends an order twice. Alpaca rejects close orders sent between 15:50 and 19:00 ET,
  so submission is refused in that window.

Commands: ``usr-paper --check`` (read-only account status), ``usr-paper --sync`` (write the paper
holdings file used by ``usr-lt-intent``), ``usr-paper --submit`` (send the latest order list).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

PAPER_URL = "https://paper-api.alpaca.markets"
NEW_YORK = ZoneInfo("America/New_York")
CLOSE_CUTOFF = time(15, 50)
REOPEN = time(19, 0)


class PaperClient:
    def __init__(self, key_id: str, secret: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=PAPER_URL,  # fixed: this module cannot reach a live account
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=30,
            transport=transport,
        )

    def _get(self, path: str, **params: Any) -> Any:
        response = self.http.get(path, params=params or None)
        response.raise_for_status()
        return response.json()

    def account(self) -> dict[str, Any]:
        return dict(self._get("/v2/account"))

    def positions(self) -> dict[str, float]:
        return {str(p["symbol"]): float(p["qty"]) for p in self._get("/v2/positions")}

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        response = self.http.get(
            "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    def submit_close_order(
        self, symbol: str, side: str, qty: int, client_order_id: str
    ) -> dict[str, Any]:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side.lower(),
            "type": "market",
            "time_in_force": "cls",
            "client_order_id": client_order_id,
        }
        response = self.http.post("/v2/orders", json=body)
        response.raise_for_status()
        return dict(response.json())


def load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    return {"enabled": bool((raw or {}).get("enabled", False)), **(raw or {})}


def client_order_id(study: str, signal_day: str, symbol: str, side: str) -> str:
    return f"{study}-{signal_day}-{symbol}-{side}".lower()[:48]


def submission_window_ok(now_utc: datetime) -> bool:
    """Alpaca rejects close orders between 15:50 and 19:00 New York time."""
    local = now_utc.astimezone(NEW_YORK).time()
    return not (CLOSE_CUTOFF <= local < REOPEN)


def reconcile(expected: dict[str, float] | None, actual: dict[str, float]) -> list[str]:
    """Differences between the positions the last submission expected and the paper account."""
    if expected is None:
        return []
    out = []
    for symbol in sorted(set(expected) | set(actual)):
        want, have = expected.get(symbol, 0.0), actual.get(symbol, 0.0)
        if abs(want - have) > 1e-6:
            out.append(f"{symbol}: expected {want:g}, paper account holds {have:g}")
    return out


def holdings_from_account(
    account: dict[str, Any], positions: dict[str, float], previous: dict[str, Any] | None
) -> dict[str, Any]:
    equity = float(account["equity"])
    peak = max(equity, float((previous or {}).get("peak_nav_usd") or 0.0))
    return {
        "as_of": datetime.now(UTC).date().isoformat(),
        "cash_usd": round(float(account["cash"]), 2),
        "positions": {s: q for s, q in sorted(positions.items()) if q},
        "peak_nav_usd": round(peak, 2),
        "equity_usd": round(equity, 2),
        "expected_after_fills": (previous or {}).get("expected_after_fills"),
        "submitted_signal_days": list((previous or {}).get("submitted_signal_days") or []),
    }


def submit(
    client: PaperClient,
    intent: dict[str, Any],
    holdings: dict[str, Any],
    *,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Send the order list (sells first) once; returns what was sent and the expected positions."""
    if intent.get("trading_enabled"):
        raise ValueError("order list claims live trading; this tool only knows paper trading")
    if intent.get("mode") != "live-candidate":
        raise ValueError(f"order list mode is {intent.get('mode')!r}: only approved studies")
    if not submission_window_ok(now_utc):
        raise ValueError("Alpaca rejects close orders between 15:50 and 19:00 New York time")
    expected = dict(holdings["positions"])
    sent = []
    for order in sorted(intent["orders"], key=lambda o: o["side"] != "SELL"):
        side, symbol, qty = order["side"], order["symbol"], int(order["shares"])
        cid = client_order_id(intent["study"], intent["signal_day"], symbol, side)
        existing = client.order_by_client_id(cid)
        result = existing or client.submit_close_order(symbol, side, qty, cid)
        sent.append({"client_order_id": cid, "status": result.get("status"), "resubmitted": not
                     existing, "symbol": symbol, "side": side, "qty": qty})  # fmt: skip
        expected[symbol] = expected.get(symbol, 0.0) + (qty if side == "BUY" else -qty)
    return sent, {s: q for s, q in expected.items() if q}


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read-only account status")
    mode.add_argument("--sync", action="store_true", help="write the paper holdings file")
    mode.add_argument("--submit", action="store_true", help="send the latest order list")
    parser.add_argument("--config", type=Path, default=Path("configs/paper_broker.yml"))
    parser.add_argument("--orders-dir", type=Path, default=Path("orders"))
    args = parser.parse_args(argv)
    load_settings()  # loads .env
    config = load_config(args.config)
    key, secret = os.environ.get("ALPACA_PAPER_KEY_ID"), os.environ.get("ALPACA_PAPER_SECRET_KEY")
    if not key or not secret:
        print("put ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY (paper account keys) in .env")
        return 2
    client = PaperClient(key, secret)
    if args.check:
        acct = client.account()
        print(json.dumps({k: acct.get(k) for k in ("status", "currency", "cash", "equity",
                                                   "buying_power", "trading_blocked")},
                         indent=2))  # fmt: skip
        print(f"模拟盘提交开关：{'开' if config['enabled'] else '关'}（{args.config}）")
        return 0
    study = str(config.get("study", "sp500_trend_voltarget"))
    path = Path(config.get("holdings", f"portfolio/paper/{study}.yml"))
    previous = yaml.safe_load(path.read_text()) if path.exists() else None
    positions = client.positions()
    holdings = holdings_from_account(client.account(), positions, previous)
    breaks = reconcile(holdings.get("expected_after_fills"), positions)
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.sync:
        if breaks:
            holdings["reconciliation_breaks"] = breaks
        path.write_text(yaml.safe_dump(holdings, allow_unicode=True, sort_keys=False))
        print(f"模拟盘持仓已写入 {path}：净值 ${holdings['equity_usd']:,.2f}")
        for b in breaks:
            print("  对账不一致：", b)
        return 1 if breaks else 0
    if not config["enabled"]:
        print("模拟盘提交开关为关（阶段 1 复核通过后由你在 configs/paper_broker.yml 打开），未发送")
        return 0
    if breaks:
        print("对账不一致，停止发送新订单：\n  " + "\n  ".join(breaks))
        return 1
    files = sorted((args.orders_dir / study).glob("*.json"))
    if not files:
        print("没有订单清单")
        return 0
    intent = json.loads(files[-1].read_text())
    if intent["signal_day"] in holdings["submitted_signal_days"]:
        # e.g. today's list was set aside by the independent check: never fall back to an older one
        print(f"最新订单清单 {files[-1].name} 已经发送过，未重复发送")
        return 0
    stale = date.fromisoformat(intent["signal_day"]) < date.today() - timedelta(days=4)
    if stale or not intent["orders"]:
        print(f"最新订单清单 {files[-1].name} 已过期或为空，未发送")
        return 0
    sent, expected = submit(client, intent, holdings, now_utc=datetime.now(UTC))
    holdings["expected_after_fills"] = expected
    holdings["submitted_signal_days"] = sorted(
        set(holdings["submitted_signal_days"]) | {intent["signal_day"]}
    )[-60:]
    path.write_text(yaml.safe_dump(holdings, allow_unicode=True, sort_keys=False))
    entry = {"created_at": datetime.now(UTC).isoformat(timespec="seconds"),
             "type": "paper_submit", "study": study, "signal_day": intent["signal_day"],
             "orders": sent}  # fmt: skip
    with (args.orders_dir / "audit_log.jsonl").open("a") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(sent, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
