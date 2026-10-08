"""Schwab Trader API (``usr-schwab``): authorization, read-only sync and -- only when the user
switches it on -- submission of verified live order lists.

Read path (``configs/schwab_api.yml`` ``read_enabled``):

* ``login``: ``auth-url`` prints the Schwab sign-in link; ``auth-code`` takes the address the
  browser was redirected to and stores the tokens in the usrtrade home directory (mode 600,
  never in the backed-up state). Schwab's refresh token lasts 7 days, so the user signs in again
  each week; ``status`` reports the days left and the daily run warns two days ahead.
* ``snapshot``: positions and cash, compared with the strategy ledger
  (``portfolio/live/schwab.yml``) and the stock-section ledger.
* ``record-fills``: filled orders of a day go into the right ledger (strategy symbols -> strategy
  ledger, everything else -> stock section), each Schwab order id once.

Write path (``orders_enabled``, default false; the risk-gate workflow checks it stays false until
the user decides otherwise and records it in AGENTS.md): ``submit`` sends the latest live order
list as DAY limit orders, sells first, only if the list passed the independent check, the account
matches the ledger, no stop file exists, it is inside the session window, quotes are within
``max_quote_deviation`` of the list's reference prices and the per-order/per-day limits hold.
Each signal day is submitted at most once.

Keys come from the environment (``.env``): ``SCHWAB_APP_KEY``, ``SCHWAB_APP_SECRET``,
``SCHWAB_REDIRECT_URI``; optional ``SCHWAB_ACCOUNT_LAST4``, ``SCHWAB_TOKEN_FILE``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import httpx
import yaml

API = "https://api.schwabapi.com"
AUTH_URL = API + "/v1/oauth/authorize"
TOKEN_URL = API + "/v1/oauth/token"
REFRESH_LIFETIME = timedelta(days=7)
NEW_YORK = ZoneInfo("America/New_York")
CONFIG = Path("configs/schwab_api.yml")


def auto_recording_on(path: Path = CONFIG) -> bool:
    """True when the daily run records Schwab fills itself (manual recording would double them)."""
    try:
        return bool((yaml.safe_load(path.read_text()) or {}).get("read_enabled"))
    except OSError:
        return False


class AuthExpired(RuntimeError):
    """The 7-day refresh token is gone: the user must sign in again."""


# ---------------------------------------------------------------- tokens


def token_path() -> Path:
    return Path(os.environ.get("SCHWAB_TOKEN_FILE") or Path.home() / ".schwab_token.json")


def save_tokens(path: Path, tokens: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens))
    tmp.chmod(0o600)
    tmp.replace(path)


def load_tokens(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AuthExpired("还没有嘉信授权：运行 bash scripts/schwab.sh login")
    return dict(json.loads(path.read_text()))


def authorize_url(app_key: str, redirect_uri: str, state: str | None = None) -> str:
    state = state or secrets.token_urlsafe(16)
    return (
        f"{AUTH_URL}?response_type=code&client_id={quote(app_key)}"
        f"&redirect_uri={quote(redirect_uri, safe='')}&state={state}"
    )


def code_from_redirect(received_url: str) -> str:
    """The ``code`` parameter of the address Schwab redirected to (``...%40`` -> ``...@``)."""
    values = parse_qs(urlparse(received_url.strip()).query).get("code")
    if not values:
        raise ValueError("粘贴的地址里没有 code=…，请复制登录后浏览器地址栏里的完整地址")
    return values[0]


def _token_request(
    http: httpx.Client, app_key: str, secret: str, data: dict[str, str], now: datetime
) -> dict[str, Any]:
    response = http.post(TOKEN_URL, data=data, auth=(app_key, secret))
    if response.status_code in (400, 401):
        raise AuthExpired(
            f"嘉信拒绝授权（{response.status_code}）：重新运行 bash scripts/schwab.sh login"
        )
    response.raise_for_status()
    body = response.json()
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "access_expires_at": (
            now + timedelta(seconds=int(body.get("expires_in", 1800)))
        ).isoformat(),
    }


def exchange_code(
    http: httpx.Client,
    app_key: str,
    secret: str,
    redirect_uri: str,
    received_url: str,
    now: datetime,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code_from_redirect(received_url),
        "redirect_uri": redirect_uri,
    }
    tokens = _token_request(http, app_key, secret, data, now)
    tokens["refresh_issued_at"] = now.isoformat()
    return tokens


def refresh_days_left(tokens: dict[str, Any], now: datetime) -> float:
    issued = datetime.fromisoformat(str(tokens["refresh_issued_at"]))
    return (issued + REFRESH_LIFETIME - now).total_seconds() / 86400


def access_token(http: httpx.Client, app_key: str, secret: str, path: Path, now: datetime) -> str:
    """A valid access token, refreshed (and saved) when it has less than a minute left."""
    tokens = load_tokens(path)
    if refresh_days_left(tokens, now) <= 0:
        raise AuthExpired("嘉信授权已过 7 天：运行 bash scripts/schwab.sh login 重新登录")
    if datetime.fromisoformat(tokens["access_expires_at"]) - now > timedelta(minutes=1):
        return str(tokens["access_token"])
    data = {"grant_type": "refresh_token", "refresh_token": str(tokens["refresh_token"])}
    new = _token_request(http, app_key, secret, data, now)
    tokens["access_token"] = new["access_token"]
    tokens["access_expires_at"] = new["access_expires_at"]
    if new.get("refresh_token"):
        tokens["refresh_token"] = new["refresh_token"]  # same 7-day clock as the sign-in
    save_tokens(path, tokens)
    return str(tokens["access_token"])


# ---------------------------------------------------------------- client


class SchwabClient:
    def __init__(self, http: httpx.Client, token: Callable[[], str]):
        self.http, self.token = http, token

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        response = self.http.request(method, API + path, headers=headers, **kw)
        response.raise_for_status()
        return response

    def account_hash(self, last4: str | None) -> tuple[str, str]:
        rows = self._req("GET", "/trader/v1/accounts/accountNumbers").json()
        if last4:
            rows = [r for r in rows if str(r["accountNumber"]).endswith(last4)]
        if len(rows) != 1:
            raise ValueError(
                f"找到 {len(rows)} 个匹配账户：在 .env 设 SCHWAB_ACCOUNT_LAST4=账号后四位"
            )
        return str(rows[0]["hashValue"]), "****" + str(rows[0]["accountNumber"])[-4:]

    def snapshot(self, account_hash: str) -> dict[str, Any]:
        body = self._req(
            "GET", f"/trader/v1/accounts/{account_hash}", params={"fields": "positions"}
        ).json()
        return parse_account(body)

    def orders(self, account_hash: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        params = {
            "fromEnteredTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toEnteredTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": 500,
        }
        return list(
            self._req("GET", f"/trader/v1/accounts/{account_hash}/orders", params=params).json()
        )

    def order(self, account_hash: str, order_id: str) -> dict[str, Any]:
        return dict(
            self._req("GET", f"/trader/v1/accounts/{account_hash}/orders/{order_id}").json()
        )

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        body = self._req(
            "GET", "/marketdata/v1/quotes", params={"symbols": ",".join(symbols)}
        ).json()
        out = {}
        for sym, q in body.items():
            quote_ = q.get("quote") or {}
            out[sym] = {
                k: float(quote_[k])
                for k in ("bidPrice", "askPrice", "lastPrice")
                if quote_.get(k) is not None
            }
        return out

    def place_limit(self, account_hash: str, symbol: str, side: str, qty: int, price: float) -> str:
        response = self._req(
            "POST",
            f"/trader/v1/accounts/{account_hash}/orders",
            json=limit_order(symbol, side, qty, price),
        )
        return response.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]


def parse_account(body: dict[str, Any]) -> dict[str, Any]:
    acct = body.get("securitiesAccount") or body
    positions: dict[str, float] = {}
    for p in acct.get("positions") or []:
        sym = str((p.get("instrument") or {}).get("symbol", ""))
        qty = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
        if sym and qty:
            positions[sym] = positions.get(sym, 0.0) + qty
    bal = acct.get("currentBalances") or {}
    return {"cash": float(bal.get("cashBalance") or 0.0), "positions": positions}


def price_str(price: float) -> str:
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def limit_order(symbol: str, side: str, qty: int, price: float) -> dict[str, Any]:
    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": price_str(price),
        "orderLegCollection": [
            {
                "instruction": side.upper(),
                "quantity": int(qty),
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }


def filled_legs(order: dict[str, Any]) -> list[dict[str, Any]]:
    """(symbol, side, qty, average price) of a filled or partly filled single-leg order."""
    legs = order.get("orderLegCollection") or []
    if len(legs) != 1:
        return []
    qty = px_qty = 0.0
    for act in order.get("orderActivityCollection") or []:
        for ex in act.get("executionLegs") or []:
            q = float(ex.get("quantity") or 0)
            qty += q
            px_qty += q * float(ex.get("price") or 0)
    if qty <= 0:
        return []
    leg = legs[0]
    side = str(leg.get("instruction", "")).upper()
    side = "BUY" if side.startswith("BUY") else "SELL" if side.startswith("SELL") else side
    return [
        {
            "order_id": str(order.get("orderId")),
            "symbol": str(leg["instrument"]["symbol"]),
            "side": side,
            "qty": qty,
            "price": px_qty / qty,
            "status": order.get("status"),
            "time": order.get("closeTime") or order.get("enteredTime"),
        }
    ]


# ---------------------------------------------------------------- reconciliation


def reconcile(
    broker: dict[str, Any],
    strategy: dict[str, Any] | None,
    stocks: dict[str, Any] | None,
    strategy_symbols: set[str],
) -> list[str]:
    """Differences between the account and the two ledgers (empty = they agree)."""
    expected: dict[str, float] = {}
    if strategy:
        for s, q in (strategy.get("positions") or {}).items():
            expected[s] = expected.get(s, 0.0) + float(q)
    if stocks:
        for s, p in (stocks.get("positions") or {}).items():
            expected[s] = expected.get(s, 0.0) + float(p["qty"])
    out = []
    for s in sorted(set(expected) | set(broker["positions"])):
        a, b = broker["positions"].get(s, 0.0), expected.get(s, 0.0)
        if abs(a - b) > 1e-6:
            book = "实盘账本" if s in strategy_symbols else "个股账本"
            out.append(f"{s}：账户 {a:g} 股，{book} {b:g} 股")
    return out


def strategy_matches(broker: dict[str, Any], strategy: dict[str, Any], symbols: set[str]) -> bool:
    held = {s: float(q) for s, q in (strategy.get("positions") or {}).items() if float(q)}
    acct = {s: q for s, q in broker["positions"].items() if s in symbols}
    return set(held) == set(acct) and all(abs(acct[s] - held[s]) < 1e-6 for s in held)


# ---------------------------------------------------------------- recording fills


def record_fills(
    legs: list[dict[str, Any]],
    day: date,
    *,
    live_path: Path,
    live_fills: Path,
    stocks_cfg: dict[str, Any] | None,
    strategy_symbols: set[str],
) -> list[str]:
    """Write each new filled order into its ledger; returns one line per action."""
    from us_stock_research.trading import live_ledger, stocks

    out = []
    live = live_ledger.load(live_path) if live_path.exists() else None
    seen_live = set((live or {}).get("schwab_order_ids") or [])
    st_path = Path(stocks_cfg["ledger"]) if stocks_cfg else None
    st = stocks.load(st_path) if st_path and st_path.exists() else None
    seen_st = set((st or {}).get("schwab_order_ids") or [])
    for leg in legs:
        oid, sym = leg["order_id"], leg["symbol"]
        if sym in strategy_symbols:
            if live is None:
                out.append(f"{sym} {leg['side']} {leg['qty']:g}：没有实盘账本，未记录")
                continue
            if oid in seen_live:
                continue
            live_ledger.fill(
                live_path,
                live_fills,
                day=day,
                symbol=sym,
                side=leg["side"],
                qty=leg["qty"],
                price=leg["price"],
                note=f"schwab:{oid}",
            )
            live = live_ledger.load(live_path)
            seen_live.add(oid)
            live["schwab_order_ids"] = sorted(seen_live)[-500:]
            live_ledger.save(live_path, live)
            out.append(f"实盘 {leg['side']} {sym} {leg['qty']:g} @ {leg['price']:.4f}")
        else:
            if st is None or st_path is None or stocks_cfg is None:
                out.append(f"{sym} {leg['side']} {leg['qty']:g}：没有个股账本，未记录")
                continue
            if oid in seen_st:
                continue
            realized = stocks.record(
                st, day=day, symbol=sym, side=leg["side"], qty=leg["qty"], price=leg["price"]
            )
            seen_st.add(oid)
            st["schwab_order_ids"] = sorted(seen_st)[-500:]
            stocks.save(st_path, st)
            stocks.write_fill(
                Path(stocks_cfg["fills"]),
                [
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    day.isoformat(),
                    sym,
                    leg["side"],
                    f"{leg['qty']:g}",
                    f"{leg['price']:.4f}",
                    "0.00",
                    f"{realized:.2f}",
                    f"schwab:{oid}",
                ],
            )
            out.append(f"个股 {leg['side']} {sym} {leg['qty']:g} @ {leg['price']:.4f}")
    return out


# ---------------------------------------------------------------- submission (off by default)


def submission_gates(
    cfg: dict[str, Any], intent_path: Path, now_utc: datetime, *, root: Path = Path(".")
) -> list[str]:
    """Reasons not to submit (empty list = allowed). Pure checks, no network."""
    from us_stock_research.calendar import is_trading_day

    reasons = []
    if cfg.get("orders_enabled") is not True:
        reasons.append("自动下单未打开（configs/schwab_api.yml orders_enabled）")
    if not str(cfg.get("user_decision") or "").strip():
        reasons.append("缺少用户决定记录（user_decision）")
    if (root / str(cfg.get("stop_file", "portfolio/STOP_TRADING"))).exists():
        reasons.append("存在停止文件")
    if not intent_path.exists():
        reasons.append(f"没有清单 {intent_path}（或已因复核不一致被搁置）")
        return reasons
    md = intent_path.with_suffix(".md")
    if not md.exists() or "**结论：一致**" not in md.read_text():
        reasons.append("清单没有通过独立复核")
    intent = json.loads(intent_path.read_text())
    if intent.get("mode") != "live-candidate":
        reasons.append(f"清单模式为 {intent.get('mode')}")
    now = now_utc.astimezone(NEW_YORK)
    signal = date.fromisoformat(intent["signal_day"])
    fill = signal + timedelta(days=1)
    while not is_trading_day(fill):
        fill += timedelta(days=1)
    if now.date() != fill:
        reasons.append(f"今天（纽约 {now.date()}）不是该清单的执行日 {fill}")
    lo, hi = (time.fromisoformat(t) for t in cfg.get("session_window_et", ["09:45", "15:30"]))
    if not lo <= now.time() <= hi:
        reasons.append(f"不在下单时段（纽约 {lo:%H:%M}–{hi:%H:%M}）")
    orders = intent.get("orders") or []
    if len(orders) > int(cfg.get("max_orders_per_day", 8)):
        reasons.append(f"订单 {len(orders)} 笔超过每日上限")
    cap = float(cfg.get("max_order_usd", 10_000))
    for o in orders:
        if float(o["est_value_usd"]) > cap:
            reasons.append(f"{o['symbol']} 约 ${o['est_value_usd']:,.0f} 超过单笔上限 ${cap:,.0f}")
    log = intent_path.parent / "submitted.jsonl"
    if log.exists() and any(
        json.loads(x).get("signal_day") == intent["signal_day"]
        for x in log.read_text().splitlines()
        if x.strip()
    ):
        reasons.append(f"{intent['signal_day']} 的清单已经提交过")
    return reasons


def limit_price(
    side: str, ref: float, quote_: dict[str, float], cfg: dict[str, Any]
) -> float | None:
    """Marketable-but-capped limit; None when the quote is missing or too far from the list."""
    off = float(cfg.get("limit_offset_bps", 10)) / 10_000
    dev = float(cfg.get("max_quote_deviation", 0.03))
    if side == "BUY":
        base = quote_.get("askPrice") or quote_.get("lastPrice")
        px = base * (1 + off) if base else None
    else:
        base = quote_.get("bidPrice") or quote_.get("lastPrice")
        px = base * (1 - off) if base else None
    if px is None or abs(base / ref - 1) > dev:  # type: ignore[operator]
        return None
    return float(price_str(px))


def submit(
    client: SchwabClient,
    account_hash: str,
    intent: dict[str, Any],
    cfg: dict[str, Any],
    *,
    wait: Callable[[float], None] = _time.sleep,
) -> list[dict[str, Any]]:
    """Sells first, wait for them, then buys within the available cash. Returns the log rows."""
    orders = intent.get("orders") or []
    quotes = client.quotes(sorted({o["symbol"] for o in orders}))
    rows: list[dict[str, Any]] = []
    sell_ids = []
    for o in [o for o in orders if o["side"] == "SELL"]:
        px = limit_price("SELL", float(o["ref_price"]), quotes.get(o["symbol"], {}), cfg)
        if px is None:
            rows.append({**o, "status": "skipped", "reason": "报价缺失或偏离参考价过大"})
            continue
        oid = client.place_limit(account_hash, o["symbol"], "SELL", int(o["shares"]), px)
        sell_ids.append(oid)
        rows.append({**o, "status": "submitted", "limit": px, "order_id": oid})
    deadline = float(cfg.get("sell_fill_wait_minutes", 10)) * 60
    waited = 0.0
    while sell_ids and waited < deadline:
        states = [client.order(account_hash, i).get("status") for i in sell_ids]
        if all(s in ("FILLED", "CANCELED", "REJECTED", "EXPIRED") for s in states):
            break
        wait(15)
        waited += 15
    cash = client.snapshot(account_hash)["cash"]
    for o in [o for o in orders if o["side"] == "BUY"]:
        px = limit_price("BUY", float(o["ref_price"]), quotes.get(o["symbol"], {}), cfg)
        if px is None:
            rows.append({**o, "status": "skipped", "reason": "报价缺失或偏离参考价过大"})
            continue
        qty = int(o["shares"])
        if qty * px > cash:
            qty = int(cash // px)
        if qty <= 0:
            rows.append({**o, "status": "skipped", "reason": f"现金不足（${cash:,.2f}）"})
            continue
        oid = client.place_limit(account_hash, o["symbol"], "BUY", qty, px)
        cash -= qty * px
        rows.append({**o, "status": "submitted", "limit": px, "order_id": oid, "shares_sent": qty})
    return rows


# ---------------------------------------------------------------- CLI


def _env() -> tuple[str, str, str]:
    key, secret = os.environ.get("SCHWAB_APP_KEY"), os.environ.get("SCHWAB_APP_SECRET")
    redirect = os.environ.get("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
    if not key or not secret:
        raise AuthExpired("服务器 .env 里还没有 SCHWAB_APP_KEY / SCHWAB_APP_SECRET")
    return key, secret, redirect


def _client(http: httpx.Client) -> SchwabClient:
    key, secret, _ = _env()
    path = token_path()
    return SchwabClient(http, lambda: access_token(http, key, secret, path, datetime.now(UTC)))


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915 - one CLI
    from us_stock_research.config import load_dotenv
    from us_stock_research.trading import stocks

    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-url")
    p = sub.add_parser("auth-code", help="redirected address on stdin (or --url)")
    p.add_argument("--url")
    p = sub.add_parser("status")
    p.add_argument("--line", action="store_true", help="one line for the daily notification")
    sub.add_parser("snapshot")
    p = sub.add_parser("record-fills")
    p.add_argument("--day", type=date.fromisoformat)
    p = sub.add_parser("submit")
    p.add_argument("--intent", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = dict(yaml.safe_load(args.config.read_text()) or {})
    symbols = {str(s) for s in cfg.get("strategy_symbols", [])}
    live_path = Path("portfolio/live/schwab.yml")
    now = datetime.now(UTC)
    try:
        if args.cmd == "auth-url":
            key, _, redirect = _env()
            print(authorize_url(key, redirect))
            return 0
        if args.cmd == "auth-code":
            key, secret, redirect = _env()
            received = args.url or sys.stdin.readline()
            with httpx.Client(timeout=30) as http:
                tokens = exchange_code(http, key, secret, redirect, received, now)
            save_tokens(token_path(), tokens)
            print("嘉信授权成功：7 天内有效，到期前会提醒你重新登录")
            return 0
        if args.cmd == "status":
            try:
                left = refresh_days_left(load_tokens(token_path()), now)
            except AuthExpired as exc:
                print("" if args.line and not cfg.get("read_enabled") else str(exc))
                return 0
            if args.line:
                if cfg.get("read_enabled") and left <= 2:
                    print(
                        f"嘉信授权还剩 {max(left, 0):.1f} 天：请运行 bash scripts/schwab.sh login"
                    )
                return 0
            print(
                f"嘉信授权剩余 {left:.1f} 天；只读 {'开' if cfg.get('read_enabled') else '关'}，"
                f"自动下单 {'开' if cfg.get('orders_enabled') else '关'}"
            )
            return 0
        if not cfg.get("read_enabled"):
            print("嘉信接口未启用（configs/schwab_api.yml read_enabled: false）")
            return 0
        st_cfg = stocks.load_config() if stocks.CONFIG.exists() else None
        with httpx.Client(timeout=30) as http:
            client = _client(http)
            acct, masked = client.account_hash(os.environ.get("SCHWAB_ACCOUNT_LAST4"))
            if args.cmd == "snapshot":
                snap = client.snapshot(acct)
                live = yaml.safe_load(live_path.read_text()) if live_path.exists() else None
                st_path = Path(st_cfg["ledger"]) if st_cfg else None
                st = yaml.safe_load(st_path.read_text()) if st_path and st_path.exists() else None
                diffs = reconcile(snap, live, st, symbols)
                print(
                    f"账户 {masked}：现金 ${snap['cash']:,.2f}；持仓 "
                    + (
                        "，".join(f"{s} {q:g}" for s, q in sorted(snap["positions"].items()))
                        or "无"
                    )
                )
                print("与账本一致" if not diffs else "与账本不一致：" + "；".join(diffs))
                out = Path("artifacts/schwab/snapshot.json")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(
                        {"time": now.isoformat(), "account": masked, **snap, "differences": diffs},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0 if not diffs else 3
            if args.cmd == "record-fills":
                day = args.day or now.astimezone(NEW_YORK).date()
                start = datetime.combine(day, time(0), NEW_YORK)
                legs = []
                for o in client.orders(acct, start, start + timedelta(days=1)):
                    if o.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REPLACED"):
                        legs += filled_legs(o)
                lines = record_fills(
                    legs,
                    day,
                    live_path=live_path,
                    live_fills=Path("portfolio/live/fills.csv"),
                    stocks_cfg=st_cfg,
                    strategy_symbols=symbols,
                )
                print(f"嘉信成交 {day}：" + ("；".join(lines) if lines else "无新成交"))
                return 0
            # submit
            reasons = submission_gates(cfg, args.intent, now)
            if reasons:
                print("未提交：" + "；".join(reasons))
                return 0
            live = yaml.safe_load(live_path.read_text())
            snap = client.snapshot(acct)
            if not strategy_matches(snap, live, symbols):
                print("未提交：账户持仓与实盘账本不一致，先对账（bash scripts/schwab.sh snapshot）")
                return 3
            intent = json.loads(args.intent.read_text())
            rows = submit(client, acct, intent, cfg)
            log = args.intent.parent / "submitted.jsonl"
            with log.open("a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "time": now.isoformat(),
                            "signal_day": intent["signal_day"],
                            "orders": rows,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            sent = [r for r in rows if r["status"] == "submitted"]
            print(
                f"已提交 {len(sent)} 笔限价单（当日有效）："
                + "；".join(
                    f"{r['side']} {r['symbol']} {r.get('shares_sent', r['shares'])} @ {r['limit']}"
                    for r in sent
                )
                + "".join(
                    f"；跳过 {r['symbol']}：{r['reason']}" for r in rows if r["status"] == "skipped"
                )
            )
            return 0
    except AuthExpired as exc:
        print(str(exc))
        return 2
    except (httpx.HTTPError, ValueError) as exc:
        print(f"嘉信接口出错：{type(exc).__name__}: {str(exc)[:200]}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
