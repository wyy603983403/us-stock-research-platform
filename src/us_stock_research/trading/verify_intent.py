"""Independent re-check of a ``vt_plus_defensive`` order list (``usr-verify-intent``).

Written separately from ``mix_intent`` from the contract text, with different structures
(``statistics`` for averages and volatility, a plain day-by-day replay with dicts): it recomputes
the signal facts, the model weights on the signal day and the order sizes from the stored adjusted
closes and the ledger, compares them with the order list, appends a "自动独立复核" table to the
list's ``.md`` file and prints one line. Exit code 4 when anything differs.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.calendar import is_trading_day

TOL_WEIGHT = 1e-5
MAX_BUFFER = 0.05


def _last_of_month(day: date) -> bool:
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt.month != day.month


def replay(
    adj: dict[str, dict[date, float]],
    start: date,
    signal_day: date,
    *,
    sma_days: int,
    vol_days: int,
    vol_target: float,
    cap: float,
    band: float,
    aggressive_weight: float,
    slots: list[str],
) -> dict[str, Any]:
    dates = sorted(d for d in adj["SPY"] if all(d in adj[s] for s in adj) and d <= signal_day)
    spy = [adj["SPY"][d] for d in dates]

    def aim_at(i: int) -> tuple[float, float, float]:
        sma = statistics.fmean(spy[i - sma_days + 1 : i + 1])
        moves = [spy[k] / spy[k - 1] - 1 for k in range(i - vol_days + 1, i + 1)]
        vol = statistics.stdev(moves) * math.sqrt(252)
        if spy[i] <= sma:
            return 0.0, sma, vol
        return (cap if vol == 0 else min(cap, vol_target / vol)), sma, vol

    def split(e: float) -> dict[str, float]:
        if e <= 1:
            return {"SPY": e, "BIL": 1 - e}
        return {"SSO": e - 1, "SPY": 2 - e}

    def slot_on(sym: str, i: int) -> tuple[bool, float]:
        series = [adj[sym][d] for d in dates[i - sma_days + 1 : i + 1]]
        avg = statistics.fmean(series)
        return series[-1] > avg, avg

    i0 = dates.index(min(d for d in dates if d >= start))
    held = aim_at(i0)[0]
    on = {s: slot_on(s, i0)[0] for s in slots}
    value = {"aggressive": aggressive_weight}
    value.update({s: (1 - aggressive_weight) / len(slots) for s in slots})
    for i in range(i0, len(dates)):
        if i > i0:
            a, b = dates[i - 1], dates[i]
            growth = sum(w * (adj[s][b] / adj[s][a]) for s, w in split(held).items() if w > 0)
            growth += 1 - sum(w for w in split(held).values() if w > 0)
            value["aggressive"] *= growth
            for s in slots:
                fund = s if on[s] else "BIL"
                value[s] *= adj[fund][b] / adj[fund][a]
            target = aim_at(i)[0]
            if (target == 0) != (held == 0) or abs(target - held) > band:
                held = target
            on = {s: slot_on(s, i)[0] for s in slots}
        if _last_of_month(dates[i]):
            total = sum(value.values())
            value["aggressive"] = aggressive_weight * total
            for s in slots:
                value[s] = (1 - aggressive_weight) * total / len(slots)
    total = sum(value.values())
    weights: dict[str, float] = {}
    for s, w in split(held).items():
        if w > 1e-9:
            weights[s] = weights.get(s, 0.0) + w * value["aggressive"] / total
    for s in slots:
        fund = s if on[s] else "BIL"
        weights[fund] = weights.get(fund, 0.0) + value[s] / total
    target, sma, vol = aim_at(len(dates) - 1)
    facts = {
        "spy_close": spy[-1],
        "spy_sma": sma,
        "spy_vol20": vol,
        "target_exposure": target,
        "held_exposure": held,
        "slots": {
            s: {"close": adj[s][dates[-1]], "sma": slot_on(s, len(dates) - 1)[1], "on": on[s]}
            for s in slots
        },  # fmt: skip
        "aggressive_share": value["aggressive"] / total,
    }
    return {"weights": weights, "facts": facts}


def expected_orders(
    weights: dict[str, float],
    positions: dict[str, float],
    cash: float,
    prices: dict[str, float],
    buffer: float = 0.0,
) -> dict[str, tuple[str, int]]:
    nav = cash + sum(n * prices[s] for s, n in positions.items())
    weights = {s: round(w, 6) for s, w in weights.items()}  # the list stores 6 decimals
    if buffer:  # buys sized to leave ``buffer`` of the account in cash (stated in the list)
        weights = {s: w * (1 - buffer) for s, w in weights.items()}
    out: dict[str, tuple[str, int]] = {}
    for s in sorted(set(weights) | set(positions)):
        have = positions.get(s, 0.0)
        gap = weights.get(s, 0.0) * nav - have * prices[s]
        n = int(have) if gap < 0 and weights.get(s, 0.0) == 0 else math.floor(abs(gap) / prices[s])
        if n > 0 and n * prices[s] >= 100.0:
            out[s] = ("SELL" if gap < 0 else "BUY", n)
    return out


def check(
    intent: dict[str, Any], recomputed: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    problems = []
    buffer = float(intent.get("cash_buffer") or 0.0)
    if not 0.0 <= buffer <= MAX_BUFFER:
        problems.append(f"现金缓冲 {buffer:.2%} 超出 0–{MAX_BUFFER:.0%}")
    for s in sorted(set(intent["target_weights"]) | set(recomputed["weights"])):
        a = intent["target_weights"].get(s, 0.0)
        b = recomputed["weights"].get(s, 0.0)
        if abs(a - b) > TOL_WEIGHT:
            problems.append(f"{s} 目标权重：清单 {a:.4%}，复核 {b:.4%}")
    if intent["reduce_only"]:
        expected = {s: v for s, v in expected.items() if v[0] == "SELL"}
    listed = {o["symbol"]: (o["side"], int(o["shares"])) for o in intent["orders"]}
    for s in sorted(set(listed) | set(expected)):
        if listed.get(s) != expected.get(s):
            problems.append(f"{s} 订单：清单 {listed.get(s)}，复核 {expected.get(s)}")
    return problems


def render(recomputed: dict[str, Any], intent: dict[str, Any], problems: list[str]) -> str:
    f = recomputed["facts"]
    lines = [
        "",
        "## 自动独立复核（另一套代码从原始价格重算）",
        "",
        "| 项目 | 复核结果 |",
        "|---|---|",
        f"| SPY 复权收盘 / 200 日均线 | {f['spy_close']:.2f} / {f['spy_sma']:.2f}"
        f"（{'上方' if f['spy_close'] > f['spy_sma'] else '下方'}） |",
        f"| 20 日年化波动 → 目标敞口 | {f['spy_vol20']:.2%} → {f['target_exposure']:.2f} 倍"
        f"（模型持有 {f['held_exposure']:.2f} 倍，进攻部分占 {f['aggressive_share']:.1%}） |",
    ]
    for s, x in f["slots"].items():
        lines.append(
            f"| {s} 复权收盘 / 200 日均线 | {x['close']:.2f} / {x['sma']:.2f}"
            f"（{'持有' if x['on'] else '转国库券'}） |"
        )
    lines.append(
        "| 目标权重 | "
        + "，".join(f"{s} {w:.2%}" for s, w in sorted(recomputed["weights"].items()))
        + " |"
    )
    lines.append(f"| 订单 | {'与清单一致' if not problems else '不一致'} |")
    lines.append("")
    lines.append("**结论：一致**" if not problems else "**结论：不一致，请勿执行**")
    lines += [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research.leveraged_trend import load_lt_contract
    from us_stock_research.research.sleeve_mix import load_mix_contract
    from us_stock_research.storage import open_store
    from us_stock_research.trading.order_intent import load_holdings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", type=Path, required=True, help="orders/<study>/<day>.json")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--holdings", type=Path, help="ledger the list was generated from")
    parser.add_argument("--capital", type=float, default=100_000.0)
    args = parser.parse_args(argv)
    intent = json.loads(args.intent.read_text())
    mix = load_mix_contract(args.contract)
    vt = load_lt_contract(Path(mix["data"]["aggressive_sleeve"]))["rule"]
    slots = [str(s) for s in mix["data"]["defensive_assets"]]
    store = open_store(load_settings())
    holdings = load_holdings(args.holdings, args.capital)
    syms = ["SPY", "SSO", "BIL", *slots, *holdings.positions]
    bars = {s: [b for b in store.read_bars(s)] for s in dict.fromkeys(syms)}
    day = date.fromisoformat(intent["signal_day"])
    adj = {s: {b.day: b.adj_close for b in rows if b.adj_close > 0} for s, rows in bars.items()}
    adj = {s: v for s, v in adj.items() if s in ("SPY", "SSO", "BIL", *slots)}
    prices = {s: [b.close for b in rows if b.day <= day][-1] for s, rows in bars.items()}
    recomputed = replay(
        adj,
        date.fromisoformat(intent["model_start"]),
        day,
        sma_days=int(vt["sma_days"]),
        vol_days=int(vt["vol_window_days"]),
        vol_target=float(vt["vol_target"]),
        cap=float(vt["max_leverage"]),
        band=float(vt["rebalance_band"]),
        aggressive_weight=float(mix["rule"]["aggressive_weight"]),
        slots=slots,
    )
    expected = expected_orders(
        recomputed["weights"], dict(holdings.positions), holdings.cash_usd, prices,
        float(intent.get("cash_buffer") or 0.0),
    )  # fmt: skip
    problems = check(intent, recomputed, expected)
    md = args.intent.with_suffix(".md")
    if md.exists() and "自动独立复核" not in md.read_text():
        md.write_text(md.read_text().rstrip("\n") + "\n" + render(recomputed, intent, problems))
    print("独立复核：一致" if not problems else "独立复核：不一致——" + "；".join(problems[:3]))
    return 4 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
