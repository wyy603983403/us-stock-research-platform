"""Single-stock section (``usr-stocks``): the user picks and trades by hand; the system keeps the
books, checks a trade against the alert limits before it is placed, monitors holdings daily and
lists weekly screening candidates.

Nothing here talks to a brokerage account or places orders. Separate money from the strategy
(``configs/live.yml``); limits come from ``configs/stocks.yml`` and only produce warnings.

    usr-stocks init --cash 5000
    usr-stocks plan --symbol NVDA --side BUY --qty 5 --price 180     # before placing the order
    usr-stocks buy  --symbol NVDA --qty 5 --price 179.60 [--fee 0] [--day 2026-11-02]
    usr-stocks sell --symbol NVDA --qty 2 --price 190
    usr-stocks cash --amount 3.21 --note "NVDA dividend (net)"       # deposits: --deposit
    usr-stocks split --symbol NVDA --ratio 10                        # 10-for-1 split
    usr-stocks show | monitor | screen
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path("configs/stocks.yml")
FILL_COLUMNS = [
    "recorded_at",
    "day",
    "symbol",
    "side",
    "qty",
    "price",
    "fee",
    "realized_usd",
    "note",
]


# ---------------------------------------------------------------- ledger


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text()) or {})


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"还没有个股账本 {path}：先运行 usr-stocks init --cash 金额")
    return dict(yaml.safe_load(path.read_text()) or {})


def save(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pos = ledger.get("positions") or {}
    ledger["positions"] = {s: p for s, p in sorted(pos.items()) if p.get("qty")}
    path.write_text(yaml.safe_dump(ledger, allow_unicode=True, sort_keys=False))


def init(path: Path, cash: float, today: date) -> dict[str, Any]:
    if path.exists():
        raise ValueError(f"{path} 已存在；如需重来请先手动改名备份")
    if not 0 < cash <= 10_000_000:
        raise ValueError("金额不合理")
    ledger = {
        "section": "stocks (manual)",
        "start_day": today.isoformat(),
        "as_of": today.isoformat(),
        "cash_usd": round(cash, 2),
        "funded_usd": round(cash, 2),
        "realized_usd": 0.0,
        "peak_nav_usd": round(cash, 2),
        "benchmark_start": None,
        "positions": {},
    }
    save(path, ledger)
    return ledger


def valuation(ledger: dict[str, Any], prices: dict[str, float]) -> dict[str, Any]:
    """NAV and one row per holding (price falls back to average cost when unknown)."""
    rows = []
    for sym, p in (ledger.get("positions") or {}).items():
        qty, cost = float(p["qty"]), float(p["cost_usd"])
        avg = cost / qty if qty else 0.0
        price = prices.get(sym)
        known = price is not None
        px = float(price) if known else avg
        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "avg_cost": avg,
                "price": px,
                "priced": known,
                "value": qty * px,
                "pnl": qty * px - cost,
                "pnl_pct": (px / avg - 1) if avg else 0.0,
            }
        )
    nav = float(ledger["cash_usd"]) + sum(r["value"] for r in rows)
    for r in rows:
        r["weight"] = r["value"] / nav if nav > 0 else 0.0
    return {"nav": nav, "cash": float(ledger["cash_usd"]), "rows": rows}


def check_trade(
    ledger: dict[str, Any],
    prices: dict[str, float],
    *,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    limits: dict[str, Any],
) -> dict[str, Any]:
    """Weight after the trade and any warnings (nothing is recorded)."""
    side = side.upper()
    if side not in ("BUY", "SELL") or qty <= 0 or price <= 0 or fee < 0:
        raise ValueError("方向须为 BUY/SELL，股数、价格须为正")
    pos = dict(ledger.get("positions") or {})
    have = float((pos.get(symbol) or {}).get("qty", 0.0))
    warnings, blocking = [], []
    if side == "SELL" and qty > have + 1e-9:
        blocking.append(f"卖出 {qty:g} 股 {symbol}，但账本只有 {have:g} 股")
    cash_after = float(ledger["cash_usd"]) - (
        qty * price + fee if side == "BUY" else -qty * price + fee
    )
    if side == "BUY" and cash_after < -0.005:
        warnings.append(
            f"现金不足：买入后账本现金为 ${cash_after:,.2f}（入金后先用 cash --deposit 记录）"
        )
    marks = dict(prices)
    marks[symbol] = price
    v = valuation(ledger, marks)
    nav = v["nav"] - fee
    qty_after = have + (qty if side == "BUY" else -qty)
    weight = qty_after * price / nav if nav > 0 else 0.0
    cap = float(limits.get("max_position_share", 1.0))
    if side == "BUY" and weight > cap + 1e-9:
        room = max(0.0, cap * nav - have * price)
        warnings.append(
            f"买入后 {symbol} 占板块 {weight:.1%}，超过 {cap:.0%}；"
            f"不超限最多再买约 {math.floor(room / price)} 股"
        )
    return {
        "weight_after": weight,
        "nav": nav,
        "cash_after": cash_after,
        "warnings": warnings,
        "blocking": blocking,
    }


def record(
    ledger: dict[str, Any],
    *,
    day: date,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    fee: float = 0.0,
) -> float:
    """Apply a fill; returns the realized P&L (sells, average-cost basis)."""
    side = side.upper()
    pos = dict(ledger.get("positions") or {})
    p = dict(pos.get(symbol) or {"qty": 0.0, "cost_usd": 0.0})
    have, cost = float(p["qty"]), float(p["cost_usd"])
    realized = 0.0
    if side == "BUY":
        p = {"qty": have + qty, "cost_usd": round(cost + qty * price + fee, 4)}
        ledger["cash_usd"] = round(float(ledger["cash_usd"]) - qty * price - fee, 2)
    else:
        if qty > have + 1e-9:
            raise ValueError(f"卖出 {qty:g} 股 {symbol}，但账本只有 {have:g} 股")
        basis = cost * qty / have
        realized = qty * price - fee - basis
        p = {"qty": have - qty, "cost_usd": round(cost - basis, 4)}
        ledger["cash_usd"] = round(float(ledger["cash_usd"]) + qty * price - fee, 2)
        ledger["realized_usd"] = round(float(ledger.get("realized_usd") or 0) + realized, 2)
    pos[symbol] = p
    ledger["positions"] = pos
    ledger["as_of"] = day.isoformat()
    return realized


def split(ledger: dict[str, Any], symbol: str, ratio: float) -> None:
    pos = dict(ledger.get("positions") or {})
    if symbol not in pos:
        raise ValueError(f"账本里没有 {symbol}")
    if ratio <= 0:
        raise ValueError("拆股比例须为正（10 拆 1 填 10，合股 1 合 10 填 0.1）")
    pos[symbol] = {"qty": float(pos[symbol]["qty"]) * ratio, "cost_usd": pos[symbol]["cost_usd"]}
    ledger["positions"] = pos


def alerts(ledger: dict[str, Any], v: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    out = []
    cap = float(limits.get("max_position_share", 1.0))
    drop = float(limits.get("drop_from_cost", 1.0))
    for r in v["rows"]:
        if r["weight"] > cap + 1e-9:
            out.append(f"{r['symbol']} 占板块 {r['weight']:.0%}（上限 {cap:.0%}）")
        if r["priced"] and r["pnl_pct"] <= -drop:
            out.append(f"{r['symbol']} 比成本跌 {-r['pnl_pct']:.0%}（提醒线 {drop:.0%}）")
        if not r["priced"]:
            out.append(f"{r['symbol']} 没有拿到最新价格（按成本估值）")
    peak = max(float(ledger.get("peak_nav_usd") or 0), v["nav"])
    dd = v["nav"] / peak - 1 if peak else 0.0
    if dd <= -float(limits.get("section_drawdown", 1.0)):
        out.append(f"板块净值比最高点低 {-dd:.0%}")
    return out


def write_fill(path: Path, row: list[Any]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FILL_COLUMNS)
        w.writerow(row)


# ---------------------------------------------------------------- screening


def screen_metrics(closes: list[float], cfg: dict[str, Any]) -> dict[str, float] | None:
    """Metrics from adjusted closes (oldest first); None while the history is too short."""
    look, skip = int(cfg["lookback_days"]), int(cfg["skip_days"])
    sma_n, vol_n = int(cfg["trend_sma_days"]), int(cfg["vol_days"])
    if len(closes) < max(look + 1, sma_n, vol_n + 1):
        return None
    last = closes[-1]
    sma = sum(closes[-sma_n:]) / sma_n
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - vol_n, len(closes))]
    mean = sum(rets) / vol_n
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (vol_n - 1) * 252)
    high = max(closes[-252:])
    return {
        "momentum_12_1": closes[-1 - skip] / closes[-1 - look] - 1,
        "return_6m": last / closes[-127] - 1,
        "above_sma": last > sma,
        "vs_sma": last / sma - 1,
        "vol": vol,
        "from_high": last / high - 1,
        "close": last,
    }


def screen(
    series: dict[str, list[float]],
    meta: dict[str, tuple[str, str]],
    cfg: dict[str, Any],
    held: set[str] | None = None,
) -> dict[str, Any]:
    held = held or set()
    rows = []
    for sym, closes in series.items():
        m = screen_metrics(closes, cfg)
        if m is None:
            continue
        name, sector = meta.get(sym, ("", ""))
        rows.append({"symbol": sym, "name": name, "sector": sector, **m, "held": sym in held})
    if not rows:
        return {"eligible": 0, "candidates": [], "vol_cut": None}
    vols = sorted(r["vol"] for r in rows)
    cut_i = max(0, math.ceil(len(vols) * (1 - float(cfg["drop_top_vol_share"]))) - 1)
    vol_cut = vols[cut_i]
    pool = [r for r in rows if r["above_sma"] and r["vol"] <= vol_cut]
    pool.sort(key=lambda r: r["momentum_12_1"], reverse=True)
    picked, per_sector = [], {}  # type: ignore[var-annotated]
    for r in pool:
        n = per_sector.get(r["sector"], 0)
        if n >= int(cfg["max_per_sector"]):
            continue
        per_sector[r["sector"]] = n + 1
        picked.append(r)
        if len(picked) >= int(cfg["top_n"]):
            break
    return {
        "eligible": len(rows),
        "trend_pool": len(pool),
        "vol_cut": vol_cut,
        "candidates": picked,
    }


def render_screen(result: dict[str, Any], day: date, bench: dict[str, float] | None) -> str:
    lines = [
        f"# 个股候选清单（{day.isoformat()}）",
        "",
        "**参考清单，不是买入建议。** 规则（configs/stocks.yml）：当前标普 500 成分股中，"
        "收盘价在 200 日均线上方、剔除波动率最高的 10%，",
        "按 12-1 动量（过去一年涨幅，去掉最近一个月）排序，同一行业最多 5 只。",
        "该规则未经验证有超额收益：本项目时点成分股研究中同类动量规则 2012–2025 年化 14.0%，"
        "SPY 14.5%。",
        "",
        f"- 有足够历史的成分股 {result['eligible']} 只；"
        f"趋势向上且波动不过高 {result.get('trend_pool', 0)} 只",
    ]
    if bench:
        lines.append(
            f"- 对照 SPY：12-1 动量 {bench['momentum_12_1']:+.1%}，60 日波动 {bench['vol']:.0%}"
        )
    lines += [
        "",
        "| # | 代码 | 公司 | 行业 | 12-1 动量 | 近 6 月 | 距 200 日线 | 60 日波动 "
        "| 距一年高点 | 已持有 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(result["candidates"], 1):
        lines.append(
            f"| {i} | {r['symbol']} | {r['name']} | {r['sector']} | {r['momentum_12_1']:+.0%} "
            f"| {r['return_6m']:+.0%} | {r['vs_sma']:+.0%} | {r['vol']:.0%} "
            f"| {r['from_high']:+.0%} | {'是' if r['held'] else ''} |"
        )
    lines += [
        "",
        "买之前：`bash scripts/stocks.sh plan 代码 BUY 股数 价格` 检查仓位上限；"
        "成交后：`bash scripts/stocks.sh buy 代码 股数 成交价`。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- monitor


def render_status(
    ledger: dict[str, Any], v: dict[str, Any], notes: list[str], bench_ret: float | None, day: date
) -> str:
    funded = float(ledger["funded_usd"])
    ret = v["nav"] / funded - 1 if funded else 0.0
    lines = [
        f"# 个股板块（{day.isoformat()}）",
        "",
        f"- 净值 ${v['nav']:,.2f}（入金 ${funded:,.2f}，{ret:+.1%}）；现金 ${v['cash']:,.2f}；"
        f"已实现盈亏 ${float(ledger.get('realized_usd') or 0):,.2f}",
        f"- 同期 SPY（含分红）：{bench_ret:+.1%}" if bench_ret is not None else "- 同期 SPY：暂无",
        f"- 提醒：{'；'.join(notes) if notes else '无'}",
        "",
        "| 代码 | 股数 | 成本价 | 现价 | 市值 | 占比 | 盈亏 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(v["rows"], key=lambda r: -r["value"]):
        lines.append(
            f"| {r['symbol']} | {r['qty']:g} | {r['avg_cost']:.2f} | {r['price']:.2f} "
            f"| ${r['value']:,.0f} | {r['weight']:.0%} | {r['pnl_pct']:+.1%} |"
        )
    return "\n".join(lines) + "\n"


def summary_line(
    ledger: dict[str, Any], v: dict[str, Any], notes: list[str], bench_ret: float | None
) -> str:
    funded = float(ledger["funded_usd"])
    ret = v["nav"] / funded - 1 if funded else 0.0
    vs = f"，同期 SPY {bench_ret:+.1%}" if bench_ret is not None else ""
    warn = f"；提醒：{'；'.join(notes)}" if notes else ""
    return f"个股板块：净值 ${v['nav']:,.0f}（{ret:+.1%}{vs}），持有 {len(v['rows'])} 只{warn}"


# ---------------------------------------------------------------- CLI


def _latest_raw_closes(symbols: list[str], day: date) -> dict[str, float]:
    import httpx

    from us_stock_research.collectors.alpaca_daily import alpaca_keys, fetch_many

    keys = alpaca_keys()
    if not keys or not symbols:
        return {}
    with httpx.Client(timeout=30) as client:
        bars = fetch_many(client, symbols, day - timedelta(days=10), day, *keys, adjustment="raw")
    return {s: b[-1].close for s, b in bars.items() if b}


def _spy_adj(day: date) -> dict[date, float]:
    from us_stock_research.config import load_settings
    from us_stock_research.storage import open_store

    try:
        bars = open_store(load_settings()).read_bars("SPY")
    except Exception:  # noqa: BLE001 - benchmark is informative only
        return {}
    return {b.day: b.adj_close for b in bars if b.day <= day and b.adj_close > 0}


def _last_session() -> date:
    from us_stock_research.quality.intraday import last_closed_session

    return last_closed_session(datetime.now(UTC))


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912, PLR0915 - one small CLI
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--cash", type=float, required=True)
    for name in ("plan", "buy", "sell"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", required=True)
        if name == "plan":
            p.add_argument("--side", required=True)
        p.add_argument("--qty", type=float, required=True)
        p.add_argument("--price", type=float, required=True)
        p.add_argument("--fee", type=float, default=0.0)
        p.add_argument("--day", type=date.fromisoformat)
        p.add_argument("--note", default="")
    p = sub.add_parser("cash")
    p.add_argument("--amount", type=float, required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--deposit", action="store_true", help="new money (or withdrawal if negative)")
    p = sub.add_parser("split")
    p.add_argument("--symbol", required=True)
    p.add_argument("--ratio", type=float, required=True)
    sub.add_parser("show")
    p = sub.add_parser("monitor")
    p.add_argument("--output", type=Path, default=Path("artifacts/stocks/status.md"))
    p = sub.add_parser("screen")
    p.add_argument("--out-dir", type=Path, default=Path("artifacts/stocks"))
    p.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    path, fills, limits = Path(cfg["ledger"]), Path(cfg["fills"]), cfg.get("alerts") or {}
    try:
        if args.cmd == "init":
            init(path, args.cash, datetime.now(UTC).date())
            print(f"个股账本已建立：现金 ${args.cash:,.2f}（{path}）")
        elif args.cmd in ("plan", "buy", "sell"):
            ledger = load(path)
            sym = args.symbol.upper().replace(".", "-")
            side = args.side.upper() if args.cmd == "plan" else args.cmd.upper()
            held = list((ledger.get("positions") or {}).keys())
            prices = _latest_raw_closes(held, _last_session()) if held else {}
            chk = check_trade(
                ledger,
                prices,
                symbol=sym,
                side=side,
                qty=args.qty,
                price=args.price,
                fee=args.fee,
                limits=limits,
            )
            for b in chk["blocking"]:
                print(f"未执行：{b}")
            if chk["blocking"]:
                return 2
            head = (
                f"{side} {sym} {args.qty:g} 股 @ {args.price}：之后占板块 {chk['weight_after']:.1%}"
            )
            if args.cmd == "plan":
                print(head + ("" if chk["warnings"] else "，未触发提醒"))
                for w in chk["warnings"]:
                    print(f"提醒：{w}")
                return 0
            day = args.day or datetime.now(UTC).date()
            realized = record(
                ledger, day=day, symbol=sym, side=side, qty=args.qty, price=args.price, fee=args.fee
            )
            save(path, ledger)
            write_fill(
                fills,
                [
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    day.isoformat(),
                    sym,
                    side,
                    f"{args.qty:g}",
                    f"{args.price:.4f}",
                    f"{args.fee:.2f}",
                    f"{realized:.2f}",
                    args.note,
                ],
            )
            extra = f"，实现盈亏 ${realized:,.2f}" if side == "SELL" else ""
            print(f"已记录：{head}{extra}；现金 ${ledger['cash_usd']:,.2f}")
            for w in chk["warnings"]:
                print(f"提醒：{w}")
        elif args.cmd == "cash":
            ledger = load(path)
            ledger["cash_usd"] = round(float(ledger["cash_usd"]) + args.amount, 2)
            if args.deposit:
                ledger["funded_usd"] = round(float(ledger["funded_usd"]) + args.amount, 2)
            log = list(ledger.get("cash_adjustments") or [])
            log.append(
                {
                    "at": datetime.now(UTC).date().isoformat(),
                    "amount": round(args.amount, 2),
                    "deposit": bool(args.deposit),
                    "note": args.note,
                }
            )
            ledger["cash_adjustments"] = log[-200:]
            save(path, ledger)
            print(f"现金 {args.amount:+,.2f}（{args.note}）；现金 ${ledger['cash_usd']:,.2f}")
        elif args.cmd == "split":
            ledger = load(path)
            split(ledger, args.symbol.upper(), args.ratio)
            save(path, ledger)
            print(f"{args.symbol.upper()} 股数 ×{args.ratio:g}，成本总额不变")
        elif args.cmd == "show":
            print(yaml.safe_dump(load(path), allow_unicode=True, sort_keys=False))
        elif args.cmd == "monitor":
            if not path.exists():
                print("个股板块：未建账本")
                return 0
            ledger = load(path)
            day = _last_session()
            prices = _latest_raw_closes(list((ledger.get("positions") or {}).keys()), day)
            v = valuation(ledger, prices)
            notes = alerts(ledger, v, limits)
            ledger["peak_nav_usd"] = round(max(float(ledger.get("peak_nav_usd") or 0), v["nav"]), 2)
            spy = _spy_adj(day)
            bench_ret = None
            if spy:
                start = date.fromisoformat(str(ledger["start_day"]))
                base = ledger.get("benchmark_start")
                if base is None:
                    prior = [d for d in spy if d <= start]
                    base = spy[max(prior)] if prior else None
                    ledger["benchmark_start"] = base
                if base:
                    bench_ret = spy[max(spy)] / float(base) - 1
            save(path, ledger)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_status(ledger, v, notes, bench_ret, day))
            args.output.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "day": day.isoformat(),
                        "nav": v["nav"],
                        "funded": ledger["funded_usd"],
                        "benchmark_return": bench_ret,
                        "alerts": notes,
                        "rows": v["rows"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            print(summary_line(ledger, v, notes, bench_ret))
        else:  # screen
            import httpx

            from us_stock_research.collectors.alpaca_daily import alpaca_keys, fetch_many
            from us_stock_research.collectors.universe import SP500_URL, parse_sp500_meta

            keys = alpaca_keys()
            if not keys:
                print("个股候选：.env 里没有 Alpaca 密钥，跳过")
                return 0
            day = args.day or _last_session()
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(SP500_URL)
                resp.raise_for_status()
                cols = parse_sp500_meta(resp.text)
                meta = {s: (n, sec) for s, n, sec in zip(cols[0], cols[1], cols[2], strict=True)}
                bars = fetch_many(
                    client,
                    sorted(meta) + ["SPY"],
                    day - timedelta(days=420),
                    day,
                    *keys,
                    adjustment="all",
                )
            series = {s: [b.close for b in b_ if b.day <= day] for s, b_ in bars.items()}
            spy = series.pop("SPY", [])
            held = set()
            if path.exists():
                held = set((load(path).get("positions") or {}).keys())
            result = screen(series, meta, cfg["screen"], held)
            bench = screen_metrics(spy, cfg["screen"]) if spy else None
            args.out_dir.mkdir(parents=True, exist_ok=True)
            out = args.out_dir / f"screen_{day.isoformat()}.md"
            out.write_text(render_screen(result, day, bench))
            top = "、".join(r["symbol"] for r in result["candidates"][:5])
            print(f"个股候选：{len(result['candidates'])} 只（前 5：{top}）→ {out}")
    except ValueError as exc:
        print(f"未执行：{exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
