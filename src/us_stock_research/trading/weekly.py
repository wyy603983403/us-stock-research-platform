"""Weekly report (``usr-weekly``): sent by the daily run after each week's last session.

From the status pages the daily run already writes: paper (and, once started, live) account
growth this week and since the start, next to the research model and SPY; the signal (distance
to the 200-day average, target and current exposure); orders of the week; the stock section;
anything needing attention.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def growth(curves: list[dict[str, Any]], key: str, since: str | None) -> float | None:
    rows = [c for c in curves if c.get(key) is not None]
    if not rows:
        return None
    base = (
        rows[0] if since is None else next((c for c in reversed(rows) if c["d"] <= since), rows[0])
    )
    return float(rows[-1][key]) / float(base[key]) - 1


def section(name: str, s: dict[str, Any], week_start: str) -> list[str]:
    curves = s.get("curves") or []
    lines = [f"【{name}】数据日 {s.get('day')}"]
    pf, sig = s.get("portfolio") or {}, s.get("signal") or {}
    if pf:
        lines.append(f"净值 ${float(pf['nav']):,.0f}，回撤 {float(pf['drawdown']):.1%}")
    if curves:

        def fmt(v: float | None) -> str:
            return "—" if v is None else f"{v:+.2%}"

        for label, since in (("本周", week_start), ("开始以来", None)):
            lines.append(f"{label}：账户 {fmt(growth(curves, 'portfolio', since))}，"
                         f"模型 {fmt(growth(curves, 'model', since))}，"
                         f"SPY {fmt(growth(curves, 'spy', since))}")  # fmt: skip
    if sig:
        lines.append(f"信号：SPY 距 200 日线 {float(sig['distance_to_sma']):+.1%}，"
                     f"目标 {float(sig['target_exposure']):.2f} 倍 / 当前 "
                     f"{float(sig['current_exposure']):.2f} 倍")  # fmt: skip
    orders = [o for o in (s.get("orders") or []) if str(o.get("signal_day", "")) > week_start]
    if orders:
        side = {"BUY": "买", "SELL": "卖"}
        lines.append(
            "本周订单："
            + "；".join(
                f"{o['signal_day']} {side.get(o['side'], o['side'])} {o['symbol']} {o['shares']} 股"
                for o in orders[:6]
            )
        )
    att = s.get("attention") or []
    lines.append("需要关注：" + ("；".join(map(str, att)) if att else "无"))
    return lines


def build(root: Path, day: date) -> str:
    week_start = (day - timedelta(days=day.weekday() + 3)).isoformat()  # the previous Friday
    out: list[str] = []
    for name, path in (("模拟盘", root / "artifacts/status.json"),
                       ("实盘", root / "artifacts/live/status.json")):  # fmt: skip
        if path.exists():
            try:
                out += section(name, json.loads(path.read_text()), week_start)
            except (ValueError, KeyError, TypeError) as exc:
                out.append(f"【{name}】状态文件读取失败：{type(exc).__name__}")
    stocks = root / "artifacts/stocks/status.json"
    if stocks.exists():
        try:
            st = json.loads(stocks.read_text())
            ret = float(st["nav"]) / float(st["funded"]) - 1 if st.get("funded") else None
            bench = st.get("benchmark_return")
            out.append(f"【个股】净值 ${float(st['nav']):,.0f}"
                       + (f"（{ret:+.1%}" if ret is not None else "（")
                       + (f"，同期 SPY {bench:+.1%}）" if bench is not None else "）"))  # fmt: skip
            if st.get("alerts"):
                out.append("个股提醒：" + "；".join(st["alerts"]))
        except (ValueError, KeyError, TypeError):
            out.append("【个股】状态文件读取失败")
    return "\n".join(out) if out else "本周没有可用的状态数据"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    print(build(Path("."), args.day))
    return 0


if __name__ == "__main__":
    sys.exit(main())
