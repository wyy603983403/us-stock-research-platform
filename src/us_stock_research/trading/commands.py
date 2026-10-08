"""Phone commands over ntfy (``usr-commands --listen``; systemd ``usr-commands.service``).

The server listens on a separate, random ntfy topic (``NTFY_CMD_TOPIC`` in ``.env``) and accepts
only two commands, both harmless if a stranger learned the topic:

* ``状态`` / ``status``: replies with the latest status page summary, positions and alerts;
* ``停止`` / ``stop``: creates the stop file (``portfolio/STOP_TRADING``): no paper orders and no
  Schwab submissions until it is removed **from the Mac** (``bash scripts/control.sh resume``).

Anything else is ignored (and answered with the list of commands). Nothing here can resume
trading, enable orders, change the strategy or the capital. Replies go to the normal
notification topic. Messages older than ten minutes (e.g. after a restart) are not executed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

STOP_FILE = Path("portfolio/STOP_TRADING")
MAX_AGE_SECONDS = 600
MIN_GAP_SECONDS = 10
COMMANDS = {"状态": "status", "status": "status", "停止": "stop", "stop": "stop"}
HELP = "可用指令：状态、停止（恢复交易只能在 Mac 上运行 bash scripts/control.sh resume）"


def parse(text: str) -> str | None:
    return COMMANDS.get(text.strip().lower())


def first_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    return [x for x in path.read_text().splitlines() if x.strip()][:n]


def status_text(root: Path = Path(".")) -> str:
    parts = []
    stop = root / STOP_FILE
    parts.append("⛔ 已紧急停止（不发模拟单、不自动下单）" if stop.exists() else "运行中（未停止）")
    st = root / "artifacts/status.json"
    if st.exists():
        try:
            s = json.loads(st.read_text())
            pf, sig = s.get("portfolio") or {}, s.get("signal") or {}
            parts.append(f"{s.get('study')}｜数据日 {s.get('day')}｜{s.get('source', '')}")
            if pf:
                parts.append(f"净值 ${float(pf['nav']):,.0f}，回撤 {float(pf['drawdown']):.1%}")
            if sig:
                parts.append(
                    f"目标仓位 {float(sig['target_exposure']):.2f} 倍 / "
                    f"当前 {float(sig['current_exposure']):.2f} 倍"
                )
            pos = (s.get("holdings") or {}).get("positions") or {}
            if pos:
                parts.append("持仓：" + "，".join(f"{k} {v:g}" for k, v in sorted(pos.items())))
            att = s.get("attention") or []
            parts.append("需要关注：" + ("；".join(map(str, att)) if att else "无"))
        except (ValueError, OSError, KeyError, TypeError):
            parts.append("状态文件读取失败")
    parts += first_lines(root / "artifacts/live/status.md", 3)[1:3]
    parts += first_lines(root / "artifacts/stocks/status.md", 4)[1:4]
    return "\n".join(parts)[:3500]


def execute(command: str, root: Path = Path(".")) -> str:
    if command == "status":
        return status_text(root)
    if command == "stop":
        stop = root / STOP_FILE
        already = stop.exists()
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text(
            f"stopped from phone at {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        )
        return ("已经是停止状态" if already else "⛔ 已紧急停止：不再发模拟单、不自动下单") + (
            "。恢复：在 Mac 上运行 bash scripts/control.sh resume"
        )
    return HELP


def reply(title: str, body: str) -> None:
    subprocess.run(["bash", "scripts/notify.sh", title, body], check=False, timeout=60)


def log(line: str) -> None:
    path = Path("logs/commands.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {line}\n")


def handle_message(msg: dict[str, Any], now: float, last_run: float) -> tuple[str | None, float]:
    """Decide what to do with one ntfy event; returns (reply or None, new last_run)."""
    if msg.get("event") != "message":
        return None, last_run
    if now - float(msg.get("time", 0)) > MAX_AGE_SECONDS:
        return None, last_run
    if now - last_run < MIN_GAP_SECONDS:
        return None, last_run
    command = parse(str(msg.get("message", "")))
    return (execute(command) if command else HELP), now


def listen(topic: str) -> None:
    since = "10m"
    last_run = 0.0
    while True:
        try:
            url = f"https://ntfy.sh/{topic}/json"
            with httpx.stream(
                "GET", url, params={"since": since}, timeout=httpx.Timeout(30, read=None)
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("id"):
                        since = msg["id"]
                    text, last_run = handle_message(msg, time.time(), last_run)
                    if text is not None:
                        log(f"command {str(msg.get('message', ''))[:40]!r}")
                        reply("指令回复", text)
        except (httpx.HTTPError, ValueError) as exc:
            log(f"stream error {type(exc).__name__}: {str(exc)[:120]}")
            time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--run", help="execute one command locally (for testing)")
    args = parser.parse_args(argv)
    load_dotenv()
    if args.run:
        print(execute(parse(args.run) or ""))
        return 0
    topic = os.environ.get("NTFY_CMD_TOPIC", "").strip()
    if not topic:
        print("没有配置 NTFY_CMD_TOPIC，指令通道未启用")
        return 0
    if args.listen:
        listen(topic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
