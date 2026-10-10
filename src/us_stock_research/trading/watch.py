"""Intraday drop alert (``usr-watch``; systemd ``usr-watch.timer`` every 15 minutes in the session).

The strategy only trades at the close, so nothing here sells. With up to 2x exposure the user
should still know when the S&P 500 falls hard during the day: when SPY is down 3%, 5% or 7% from
the previous close, one notification per level per day, with the estimated move of the paper and
live accounts (current exposure x SPY move). The notification carries the usual buttons
("查询状态" / "紧急停止"); stopping blocks new orders but does not sell existing positions.

Price: Alpaca market-data snapshot for SPY (IEX feed, real time on the free plan).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

NEW_YORK = ZoneInfo("America/New_York")
LEVELS = (-0.03, -0.05, -0.07)
SNAPSHOT = "https://data.alpaca.markets/v2/stocks/{symbol}/snapshot"


def spy_move(client: httpx.Client, key: str, secret: str) -> tuple[float, float, float]:
    r = client.get(SNAPSHOT.format(symbol="SPY"), params={"feed": "iex"},
                   headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})  # fmt: skip
    r.raise_for_status()
    body = r.json()
    last = float(body["latestTrade"]["p"])
    prev = float(body["prevDailyBar"]["c"])
    return last, prev, last / prev - 1


def new_levels(move: float, alerted: list[float]) -> list[float]:
    return [lv for lv in LEVELS if move <= lv and lv not in alerted]


def exposure(path: Path) -> float | None:
    try:
        return float(json.loads(path.read_text())["signal"]["current_exposure"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def message(move: float, last: float, prev: float, paper: float | None, live: float | None) -> str:
    lines = [f"SPY 现价 {last:.2f}，比昨收 {prev:.2f} {move:+.1%}"]
    for name, e in (("模拟盘", paper), ("实盘", live)):
        if e is not None:
            lines.append(f"{name}当前 {e:.2f} 倍仓位，估计今日 {e * move:+.1%}")
    lines.append("策略按收盘价调整，盘中不会自动卖出；收盘后若波动上升或跌破均线会自动降仓。")
    lines.append("“紧急停止”只阻止新订单，不卖出现有持仓；实盘如需减仓请在嘉信 App 手动操作。")
    return "\n".join(lines)


def in_session(now: datetime) -> bool:
    from us_stock_research.calendar import is_trading_day

    local = now.astimezone(NEW_YORK)
    return is_trading_day(local.date()) and time(9, 40) <= local.time() <= time(16, 0)


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.collectors.alpaca_daily import alpaca_keys
    from us_stock_research.config import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="run outside the session (testing)")
    args = parser.parse_args(argv)
    load_dotenv()
    now = datetime.now(UTC)
    if not args.force and not in_session(now):
        return 0
    keys = alpaca_keys()
    if not keys:
        print("盘中预警：.env 里没有 Alpaca 密钥")
        return 0
    try:
        with httpx.Client(timeout=20) as client:
            last, prev, move = spy_move(client, *keys)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"盘中预警：取价失败 {type(exc).__name__}")
        return 0
    day = now.astimezone(NEW_YORK).date().isoformat()
    state = Path(f"artifacts/.watch_{day}.json")
    alerted: list[float] = json.loads(state.read_text()) if state.exists() else []
    fresh = new_levels(move, alerted)
    print(f"{day} SPY {move:+.2%}" + (f"，触发 {fresh}" if fresh else ""))
    if not fresh:
        return 0
    text = message(move, last, prev, exposure(Path("artifacts/status.json")),
                   exposure(Path("artifacts/live/status.json")))  # fmt: skip
    subprocess.run(["bash", "scripts/notify.sh", f"⚠️ 盘中大跌 {move:+.1%}", text], check=False,
                   timeout=60)  # fmt: skip
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(alerted + fresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
