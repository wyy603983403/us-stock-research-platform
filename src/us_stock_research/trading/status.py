"""Daily status of the approved strategy: signal, portfolio, risk limits and tracking vs the model.

Run after the daily update. It values the holdings (rehearsal ledger, or the paper-account file
once stage 2 is on) at the last close, appends one row per day to ``<holdings>.nav.csv`` and writes
``artifacts/status.md``. Tracking compares the portfolio since its first fill with the research
model run over the same days -- the stage 2 gate in ``configs/stage_gates.yml`` is judged on it.

Exit code 0, or 3 when something needs attention (stale data, reduce-only, breaker within 10
points, tracking outside the band) so the daily script can raise a notification.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.research.leveraged_trend import forward_fill, simulate_vol_target
from us_stock_research.trading.lt_intent import current_exposure, vol_target_exposure
from us_stock_research.trading.order_intent import Holdings, load_holdings


def append_nav(path: Path, day: date, nav: float, exposure: float) -> list[tuple[date, float]]:
    rows: dict[date, tuple[float, float]] = {}
    if path.exists():
        with path.open() as fh:
            for r in csv.DictReader(fh):
                rows[date.fromisoformat(r["date"])] = (float(r["nav"]), float(r["exposure"]))
    rows[day] = (nav, exposure)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "nav", "exposure"])
        for d in sorted(rows):
            w.writerow([d.isoformat(), f"{rows[d][0]:.2f}", f"{rows[d][1]:.4f}"])
    return [(d, rows[d][0]) for d in sorted(rows)]


def read_nav(path: Path) -> list[tuple[date, float, float]]:
    if not path.exists():
        return []
    with path.open() as fh:
        return [
            (date.fromisoformat(r["date"]), float(r["nav"]), float(r["exposure"]))
            for r in csv.DictReader(fh)
        ]


def chart_series(
    rule: dict[str, Any],
    days: list[date],
    closes: list[float],
    yields_pct: list[float | None],
    nav_rows: list[tuple[date, float, float]],
    signal_days: int = 260,
    model_index: dict[date, float] | None = None,
) -> dict[str, Any]:
    """Curves for the dashboard: portfolio, research model and SPY indexed to 100 at the first
    valuation; exposure history; SPY and its moving average over the last year."""
    n = int(rule["sma_days"])
    sma = [sum(closes[i - n + 1 : i + 1]) / n if i >= n - 1 else None for i in range(len(closes))]
    tail = range(max(0, len(days) - signal_days), len(days))
    out: dict[str, Any] = {
        "signal_series": [
            {"d": days[i].isoformat(), "spy": round(closes[i], 4),
             "sma": None if sma[i] is None else round(float(sma[i] or 0.0), 4)}
            for i in tail
        ],
        "curves": [],
        "exposure": [{"d": d.isoformat(), "e": e} for d, _, e in nav_rows],
    }  # fmt: skip
    if not nav_rows:
        return out
    start, nav0 = nav_rows[0][0], nav_rows[0][1]
    if model_index is not None:
        base = model_index.get(start)
        model = {d: v / base for d, v in model_index.items() if base and d >= start}
    else:
        sim = simulate_vol_target(
            days,
            closes,
            yields_pct,
            sma_days=n,
            vol_window=int(rule["vol_window_days"]),
            vol_target=float(rule["vol_target"]),
            max_leverage=float(rule["max_leverage"]),
            band=float(rule["rebalance_band"]),
            cost_bps=float(rule["trading_cost_bps"]),
        )
        model = {start: 1.0}
        g = 1.0
        for d, r, _, _ in sim:
            if d > start:
                g *= 1 + r
                model[d] = g
    price = dict(zip(days, closes, strict=True))
    for d, v, _ in nav_rows:
        if d in model and d in price and start in price:
            out["curves"].append(
                {"d": d.isoformat(), "portfolio": round(100 * v / nav0, 4),
                 "model": round(100 * model[d], 4),
                 "spy": round(100 * price[d] / price[start], 4)}
            )  # fmt: skip
    return out


def recent_orders(folder: Path, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        intent = json.loads(path.read_text())
        for o in intent.get("orders", []):
            rows.append(
                {"signal_day": intent["signal_day"], "side": o["side"], "symbol": o["symbol"],
                 "shares": o["shares"], "ref_price": o["ref_price"],
                 "value": o["est_value_usd"], "mode": intent.get("mode")}
            )  # fmt: skip
    return rows[-limit:]


def model_growth(
    rule: dict[str, Any],
    days: list[date],
    closes: list[float],
    yields_pct: list[float | None],
    start: date,
) -> float:
    """Growth of the research model from the close of ``start`` to the last day."""
    sim = simulate_vol_target(
        days,
        closes,
        yields_pct,
        sma_days=int(rule["sma_days"]),
        vol_window=int(rule["vol_window_days"]),
        vol_target=float(rule["vol_target"]),
        max_leverage=float(rule["max_leverage"]),
        band=float(rule["rebalance_band"]),
        cost_bps=float(rule["trading_cost_bps"]),
    )
    g = 1.0
    for d, r, _, _ in sim:
        if d > start:
            g *= 1 + r
    return g


def build(
    contract: dict[str, Any],
    gates: dict[str, Any],
    signal_closes: list[tuple[date, float]],
    prices: dict[str, float],
    holdings: Holdings,
    nav_history: list[tuple[date, float]],
    yields: dict[date, float],
    *,
    one_x: str,
    leveraged: str,
    breaker: float,
    data_issues: list[str],
    model_index: dict[date, float] | None = None,
    target_exposure: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = contract["rule"]
    days = [d for d, _ in signal_closes]
    closes = [c for _, c in signal_closes]
    trend = vol_target_exposure(closes, rule) > 0
    aim = vol_target_exposure(closes, rule) if target_exposure is None else target_exposure
    now = current_exposure(holdings, prices, one_x, leveraged)
    sma = sum(closes[-int(rule["sma_days"]) :]) / int(rule["sma_days"])
    nav = nav_history[-1][1]
    peak = max(float(holdings.peak_nav_usd or 0.0), max(v for _, v in nav_history))
    drawdown = nav / peak - 1 if peak else 0.0
    attention = list(data_issues)
    if -drawdown > breaker - 0.10:
        attention.append(f"回撤 {drawdown:.1%}，离 {breaker:.0%} 熔断不到 10 个百分点")
    tracking: dict[str, Any] | None = None
    if len(nav_history) >= 2:
        start, nav0 = nav_history[0]
        if model_index is not None and start in model_index:
            model = model_index[max(model_index)] / model_index[start]
        else:
            model = model_growth(rule, days, closes, forward_fill(days, yields), start)
        actual = nav / nav0
        span_years = max((nav_history[-1][0] - start).days / 365.25, 1 / 365.25)
        gap = actual - model
        tracking = {
            "since": start.isoformat(),
            "portfolio_growth": actual,
            "model_growth": model,
            "gap": gap,
            "gap_annualized": (actual / model) ** (1 / span_years) - 1 if span_years >= 1 else None,
        }
        band = float(gates.get("stage2", {}).get("max_cumulative_tracking_gap", 0.03))
        if abs(gap) > band:
            attention.append(f"与模型的累计偏差 {gap:+.2%} 超过 ±{band:.0%}")
    return {
        "day": days[-1].isoformat(),
        "signal": {
            "close": closes[-1],
            "sma": sma,
            "distance_to_sma": closes[-1] / sma - 1,
            "target_exposure": aim,
            "current_exposure": now,
            "trend_on": trend,
        },
        **(extra or {}),
        "portfolio": {"nav": nav, "peak": peak, "drawdown": drawdown, "breaker": breaker},
        "tracking": tracking,
        "attention": attention,
    }


def render(s: dict[str, Any], holdings: Holdings, source: str) -> str:
    sig, pf, tr = s["signal"], s["portfolio"], s["tracking"]
    lines = [
        f"# 交易系统状态（{s['day']}）",
        "",
        f"策略：{s.get('strategy_label', '均线 + 波动率目标')}（已批准，{source}）。"
        "只生成文件/模拟单，不涉及真钱。",
        "",
        "## 信号",
        f"- SPY 复权收盘 {sig['close']:.2f}，200 日均线 {sig['sma']:.2f}"
        f"（{sig['distance_to_sma']:+.1%}，{'上方' if sig['trend_on'] else '下方'}）",
        f"- 目标敞口 {sig['target_exposure']:.2f} 倍，当前 {sig['current_exposure']:.2f} 倍"
        + ("（股票总敞口，占净值）" if s.get("slots_on") else ""),
        *(
            [
                "- 防守部分："
                + "，".join(f"{k} {'持有' if v else '转国库券'}" for k, v in s["slots_on"].items())
            ]
            if s.get("slots_on")
            else []
        ),
        "",
        "## 组合",
        f"- 净值 ${pf['nav']:,.2f}，最高 ${pf['peak']:,.2f}，回撤 {pf['drawdown']:.1%}"
        f"（熔断线 {pf['breaker']:.0%}）",
        "- 持仓："
        + ("，".join(f"{k} {v:g} 股" for k, v in sorted(holdings.positions.items())) or "无")
        + f"；现金 ${holdings.cash_usd:,.2f}",
    ]
    if tr:
        lines += [
            "",
            "## 与研究模型对照",
            f"- 自 {tr['since']} 起：组合 {tr['portfolio_growth'] - 1:+.2%}，"
            f"模型 {tr['model_growth'] - 1:+.2%}，偏差 {tr['gap']:+.2%}",
        ]
    lines += ["", "## 需要关注", *([f"- {a}" for a in s["attention"]] or ["- 无"]), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research.leveraged_trend import load_lt_contract
    from us_stock_research.storage import open_store
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--holdings", type=Path, required=True)
    parser.add_argument("--signal-symbol", default="SPY")
    parser.add_argument("--one-x", default="SPY")
    parser.add_argument("--risk-on", default="SSO")
    parser.add_argument("--risk-off", default="BIL")
    parser.add_argument("--breaker", type=float, default=0.40)
    parser.add_argument("--gates", type=Path, default=Path("configs/stage_gates.yml"))
    parser.add_argument("--update-report", type=Path, help="latest usr-update JSON")
    parser.add_argument("--output", type=Path, default=Path("artifacts/status.md"))
    parser.add_argument("--orders-dir", type=Path, default=Path("orders"))
    parser.add_argument(
        "--capital", type=float, default=100_000.0, help="本金（还没有持仓文件时按全部现金显示）"
    )
    parser.add_argument(
        "--model-start", type=date.fromisoformat, help="sleeve_mix：模型重放起点（与出单一致）"
    )
    args = parser.parse_args(argv)
    pending = not args.holdings.exists()
    args.holdings.parent.mkdir(parents=True, exist_ok=True)
    mix = None
    if (yaml.safe_load(args.contract.read_text()) or {}).get("kind") == "sleeve_mix":
        from us_stock_research.research.sleeve_mix import load_mix_contract

        mix = load_mix_contract(args.contract)
        if args.model_start is None:
            parser.error("--model-start is required for a sleeve_mix contract")
        contract = load_lt_contract(Path(mix["data"]["aggressive_sleeve"]))
    else:
        contract = load_lt_contract(args.contract)
    gates = yaml.safe_load(args.gates.read_text()) if args.gates.exists() else {}
    settings = load_settings()
    store = open_store(settings)
    holdings = load_holdings(args.holdings, args.capital)
    bars = [b for b in store.read_bars(args.signal_symbol) if b.adj_close > 0]
    day = bars[-1].day
    prices = {}
    data_issues = []
    extra_symbols = list(mix["data"]["defensive_assets"]) if mix else []
    for s in {args.one_x, args.risk_on, args.risk_off, *extra_symbols, *holdings.positions}:
        sb = store.read_bars(s)
        prices[s] = sb[-1].close
        if sb[-1].day != day:
            data_issues.append(f"{s} 数据截至 {sb[-1].day}，SPY 为 {day}")
    if args.update_report and args.update_report.exists():
        behind = json.loads(args.update_report.read_text()).get("behind") or []
        if behind:
            data_issues.append(f"{len(behind)} 只标的未拿到最新交易日数据")
    nav = holdings.cash_usd + sum(n * prices[s] for s, n in holdings.positions.items())
    history = append_nav(
        args.holdings.with_suffix(".nav.csv"),
        day,
        nav,
        current_exposure(holdings, prices, args.one_x, args.risk_on),
    )
    rows = TableStore.from_settings(settings).read("macro", "DTB3", "date, value")
    yields = {d: float(v) for d, v in rows if v is not None and v == v}
    model_index: dict[date, float] | None = None
    overrides: dict[str, Any] = {}
    if mix is not None:
        from us_stock_research.trading.mix_intent import model_path

        slots = [str(x) for x in mix["data"]["defensive_assets"]]
        syms = [args.one_x, args.risk_on, args.risk_off, *slots]
        adj = {x: {b.day: b.adj_close for b in store.read_bars(x) if b.adj_close > 0} for x in syms}
        common = sorted(set.intersection(*(set(v) for v in adj.values())))
        path = model_path(adj, common, args.model_start, contract["rule"], mix["rule"], slots)
        model_index = {r["day"]: r["value"] for r in path}
        last = path[-1]
        w = last["weights"]
        overrides = {
            "model_index": model_index,
            "target_exposure": w.get(args.one_x, 0.0) + 2 * w.get(args.risk_on, 0.0),
            "extra": {
                "slots_on": last["slots_on"],
                "aggressive_share": last["aggressive_share"],
                "target_weights": w,
                "strategy_label": "标普趋势 + 波动率目标 50% / 防守趋势（TLT、IEF、GLD）50%",
            },
        }
    status = build(
        contract,
        gates,
        [(b.day, b.adj_close) for b in bars],
        prices,
        holdings,
        history,
        yields,
        one_x=args.one_x,
        leveraged=args.risk_on,
        breaker=args.breaker,
        data_issues=data_issues,
        **overrides,
    )
    source = "Alpaca 模拟盘" if "paper" in str(args.holdings) else "演练账本"
    if pending:
        source += "（首笔订单尚未成交，按全部现金显示）"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(status, holdings, source))
    from us_stock_research.trading.dashboard import render_html

    days_all = [b.day for b in bars]
    payload = {
        **status,
        "source": source,
        "study": (mix or contract)["name"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "holdings": {"cash": holdings.cash_usd, "positions": holdings.positions},
        "prices": prices,
        "orders": recent_orders(args.orders_dir / (mix or contract)["name"]),
        **chart_series(
            contract["rule"],
            days_all,
            [b.adj_close for b in bars],
            forward_fill(days_all, yields),
            read_nav(args.holdings.with_suffix(".nav.csv")),
            model_index=model_index,
        ),
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    args.output.with_name("status.json").write_text(text)
    args.output.with_name("dashboard.html").write_text(render_html(payload))
    sig, pf = status["signal"], status["portfolio"]
    print(
        f"{status['day']}：目标 {sig['target_exposure']:.2f} 倍 / "
        f"当前 {sig['current_exposure']:.2f} 倍，"
        f"净值 ${pf['nav']:,.0f}，回撤 {pf['drawdown']:.1%}"
        + (f"，偏差 {status['tracking']['gap']:+.2%}" if status["tracking"] else "")
    )
    for a in status["attention"]:
        print("  需要关注：", a)
    return 3 if status["attention"] else 0


if __name__ == "__main__":
    sys.exit(main())
