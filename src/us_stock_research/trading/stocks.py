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
    usr-stocks look --symbol NVDA                                     # one-page profile
    usr-stocks report                                                # yearly totals
    usr-stocks watch add|rm|list [SYMBOLS] [--note TEXT]               # watchlist
    usr-stocks screen --adhoc                                        # on-demand screen
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
STRATEGY_SYMBOLS = frozenset({"SPY", "SSO", "BIL", "TLT", "IEF", "GLD"})  # configs/schwab_api.yml
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
    if symbol in STRATEGY_SYMBOLS:
        blocking.append(f"{symbol} 是实盘策略使用的代码，个股板块不能买卖它（否则两本账混在一起）")
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


# ---------------------------------------------------------------- short interest


def crowded_cut(dtc: dict[str, float], members: set[str], share: float) -> float | None:
    """Days-to-cover at or above which a member is in the most crowded ``share`` of members."""
    vals = sorted(v for s, v in dtc.items() if s in members)
    if not vals or share <= 0:
        return None
    return vals[max(0, math.ceil(len(vals) * (1 - share)) - 1)]


def short_notes(
    symbols: list[str], short: dict[str, Any] | None, members: set[str], share: float
) -> list[str]:
    """Reminders for held stocks among the most shorted (days to cover) S&P 500 members."""
    if not short:
        return []
    cut = crowded_cut(short["dtc"], members, share)
    if cut is None:
        return []
    return [
        f"{s} 空头拥挤（回补 {short['dtc'][s]:.1f} 天，标普前 {share:.0%}，"
        f"FINRA {short['settlement']}）"
        for s in sorted(symbols)
        if short["dtc"].get(s, -1.0) >= cut
    ]


def latest_short(client: Any, day: date, max_back: int = 30) -> dict[str, Any] | None:
    """Newest FINRA short interest file settled on or before ``day`` (None if none found)."""
    from us_stock_research.collectors.finra_short import URL, parse
    from us_stock_research.research.short_interest import days_to_cover

    for back in range(max_back + 1):
        d = day - timedelta(days=back)
        r = client.get(URL.format(d=d))
        if r.status_code != 200:
            continue
        rows = parse(r.text, d)
        return {
            "settlement": d.isoformat(),
            "dtc": days_to_cover([(x[1], x[3], x[4]) for x in rows]),
        }
    return None


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
    short: dict[str, Any] | None = None,
) -> dict[str, Any]:
    held = held or set()
    share = float(cfg.get("short_crowded_share", 0.10))
    cut = crowded_cut(short["dtc"], set(meta), share) if short else None
    rows = []
    for sym, closes in series.items():
        m = screen_metrics(closes, cfg)
        if m is None:
            continue
        name, sector = meta.get(sym, ("", ""))
        dtc = short["dtc"].get(sym) if short else None
        crowded = cut is not None and dtc is not None and dtc >= cut
        rows.append(
            {"symbol": sym, "name": name, "sector": sector, **m, "held": sym in held,
             "dtc": dtc, "crowded": crowded}
        )  # fmt: skip
    if not rows:
        return {"eligible": 0, "candidates": [], "vol_cut": None, "rows": {}}
    ranked = sorted(rows, key=lambda r: r["momentum_12_1"])
    for i, r in enumerate(ranked):
        r["mom_pct"] = (i + 1) / len(ranked)  # 1.0 = strongest 12-1 momentum among members
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
        "rows": {r["symbol"]: r for r in rows},
        "short": None if not short else {"settlement": short["settlement"], "cut": cut,
                                         "share": share},
    }  # fmt: skip


def render_screen(
    result: dict[str, Any],
    day: date,
    bench: dict[str, float] | None,
    changes: dict[str, list[str]] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    prev_day: str | None = None,
    watch: list[dict[str, Any]] | None = None,
    earnings_flag_days: int = 14,
) -> str:
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
        "| 距一年高点 | β | 一年最大回撤 | 回补天数 | 下次财报（推算） | 已持有 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for i, r in enumerate(result["candidates"], 1):
        dtc = r.get("dtc")
        cell = "—" if dtc is None else f"{dtc:.1f}" + (" ⚠️拥挤" if r.get("crowded") else "")
        earn = r.get("earnings")
        e_cell = "—"
        if earn:
            e_cell = f"{earn['estimate']:%m-%d}"
            if 0 <= earn["days_to"] <= earnings_flag_days:
                e_cell += f" ⚠️{earn['days_to']} 天后"
        lines.append(
            f"| {i} | {r['symbol']} | {r['name']} | {r['sector']} | {r['momentum_12_1']:+.0%} "
            f"| {r['return_6m']:+.0%} | {r['vs_sma']:+.0%} | {r['vol']:.0%} "
            f"| {r['from_high']:+.0%} | {_pct(r.get('beta'), '.2f')} | {_pct(r.get('max_dd'))} "
            f"| {cell} | {e_cell} | {'是' if r['held'] else ''} |"
        )
    lines.append("")
    if any(r.get("earnings") for r in result["candidates"]):
        lines += [
            f"下次财报按过去 8-K 业绩公告（Item 2.02）间隔推算；⚠️ = {earnings_flag_days} 天内，"
            "财报前后股价波动通常更大。β、一年最大回撤为过去一年数据。",
            "",
        ]
    if changes is not None:
        lines.append(
            f"**和上一期（{prev_day}）相比**：新进 {'、'.join(changes['new']) or '无'}；"
            f"掉出 {'、'.join(changes['dropped']) or '无'}"
        )
        lines.append("")
    if holdings:
        lines += [
            "**你的持仓在本周规则下的位置**（动量百分位：100% = 标普 500 中最强）：",
            "",
            "| 代码 | 动量百分位 | 200 日线上方 | 波动在剔除线内 | 本周候选 |",
            "|---|---:|---|---|---|",
        ]
        for h in holdings:
            if not h["evaluated"]:
                lines.append(f"| {h['symbol']} | — | — | — | 不在当前标普 500 或历史不足，未评估 |")
                continue
            lines.append(
                f"| {h['symbol']} | {h['mom_pct']:.0%} | {'是' if h['above_sma'] else '**否**'} "
                f"| {'是' if h['vol_ok'] else '否'} "
                f"| {('第 ' + str(h['rank']) + ' 位') if h['rank'] else '不在'} |"
            )
        lines.append("")
    if watch:
        lines += [
            "**观察清单在本周规则下的位置**：",
            "",
            "| 代码 | 动量百分位 | 200 日线上方 | 波动在剔除线内 | 本周候选 |",
            "|---|---:|---|---|---|",
        ]
        for h in watch:
            if not h["evaluated"]:
                lines.append(f"| {h['symbol']} | — | — | — | 不在当前标普 500 或历史不足，未评估 |")
                continue
            lines.append(
                f"| {h['symbol']} | {h['mom_pct']:.0%} | {'是' if h['above_sma'] else '否'} "
                f"| {'是' if h['vol_ok'] else '否'} "
                f"| {('第 ' + str(h['rank']) + ' 位') if h['rank'] else '不在'} |"
            )
        lines.append("")
    sh = result.get("short")
    if sh and sh.get("cut") is not None:
        lines.append(
            f"回补天数 = 空头股数 ÷ 日均成交量（FINRA，结算日 {sh['settlement']}）。"
            f"⚠️拥挤 = 标普 500 中最高的 {sh['share']:.0%}（≥ {sh['cut']:.1f} 天）。"
            "研究 18（docs/results/sp500-short-interest.md）中，回补天数最高的 30 只 2018–2025 "
            "年化 3.2%，同期等权 10.1%（看过结果后的观察，未经独立验证）；只作提醒。"
        )
    else:
        lines.append("回补天数：本次没有拿到 FINRA 空头数据。")
    lines += [
        "",
        "买之前：`bash scripts/stocks.sh plan 代码 BUY 股数 价格` 检查仓位上限；"
        "成交后：`bash scripts/stocks.sh buy 代码 股数 成交价`。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- monitor


def _pct(x: float | None, fmt: str = "+.0%") -> str:
    return "—" if x is None else format(x, fmt)


def render_status(
    ledger: dict[str, Any],
    v: dict[str, Any],
    notes: list[str],
    bench_ret: float | None,
    day: date,
    extra: dict[str, Any] | None = None,
) -> str:
    from us_stock_research.trading.stock_analysis import sector_weights, weighted

    extra = extra or {}
    funded = float(ledger["funded_usd"])
    ret = v["nav"] / funded - 1 if funded else 0.0
    shadow = extra.get("shadow")
    if shadow:
        diff = v["nav"] - shadow["value"]
        bench = (
            f"- 同样的入金在入金当天买 SPY（含分红）：现值 ${shadow['value']:,.2f}"
            f"（{shadow['return']:+.1%}）；个股板块{'多' if diff >= 0 else '少'} ${abs(diff):,.2f}"
        )
    elif bench_ret is not None:
        bench = f"- 同期 SPY（含分红）：{bench_ret:+.1%}"
    else:
        bench = "- 同期 SPY：暂无"
    lines = [
        f"# 个股板块（{day.isoformat()}）",
        "",
        f"- 净值 ${v['nav']:,.2f}（入金 ${funded:,.2f}，{ret:+.1%}）；现金 ${v['cash']:,.2f}；"
        f"已实现盈亏 ${float(ledger.get('realized_usd') or 0):,.2f}",
        bench,
    ]
    rows = v["rows"]
    if rows:
        b, vol = weighted(rows, "beta"), weighted(rows, "vol_60")
        lines.append(
            f"- 板块 β {_pct(b, '.2f')}、持仓 60 日波动加权 {_pct(vol, '.0%')}"
            "（按净值加权，现金按 0；β 1 = 与 SPY 同步）"
        )
        secs = sector_weights(rows, v["nav"])
        if secs:
            lines.append("- 行业：" + "、".join(f"{s_} {w:.0%}" for s_, w in secs))
    lines += [
        f"- 提醒：{'；'.join(notes) if notes else '无'}",
        "",
        "| 代码 | 股数 | 成本价 | 现价 | 当日 | 市值 | 占比 | 盈亏 | 距 200 日线 | β "
        "| 下次财报（推算） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in sorted(rows, key=lambda r: -r["value"]):
        earn = r.get("earnings")
        e_cell = "—" if not earn else f"约 {earn['estimate']:%m-%d}（{earn['days_to']:+d} 天）"
        lines.append(
            f"| {r['symbol']} | {r['qty']:g} | {r['avg_cost']:.2f} | {r['price']:.2f} "
            f"| {_pct(r.get('chg_1d'), '+.1%')} | ${r['value']:,.0f} | {r['weight']:.0%} "
            f"| {r['pnl_pct']:+.1%} | {_pct(r.get('vs_sma200'))} | {_pct(r.get('beta'), '.2f')} "
            f"| {e_cell} |"
        )
    if rows:
        lines += [
            "",
            "下次财报为按过去 8-K 业绩公告（Item 2.02）间隔推算的日期，以公司公告为准。",
        ]
    return "\n".join(lines) + "\n"


def summary_line(
    ledger: dict[str, Any],
    v: dict[str, Any],
    notes: list[str],
    bench_ret: float | None,
    shadow: dict[str, float] | None = None,
) -> str:
    funded = float(ledger["funded_usd"])
    ret = v["nav"] / funded - 1 if funded else 0.0
    if shadow:
        vs = f"，同样入金买 SPY {shadow['return']:+.1%}"
    else:
        vs = f"，同期 SPY {bench_ret:+.1%}" if bench_ret is not None else ""
    warn = f"；提醒：{'；'.join(notes)}" if notes else ""
    return f"个股板块：净值 ${v['nav']:,.0f}（{ret:+.1%}{vs}），持有 {len(v['rows'])} 只{warn}"


def holding_alerts(rows: list[dict[str, Any]], limits: dict[str, Any]) -> list[str]:
    """Trend-break and upcoming-earnings reminders (figures attached by ``usr-stocks monitor``)."""
    out = []
    within = limits.get("earnings_within_days")
    for r in sorted(rows, key=lambda r: r["symbol"]):
        if limits.get("below_sma200") and r.get("vs_sma200") is not None and r["vs_sma200"] < 0:
            out.append(f"{r['symbol']} 收盘在 200 日均线下方（{r['vs_sma200']:+.0%}）")
        earn = r.get("earnings")
        if within is not None and earn and 0 <= earn["days_to"] <= int(within):
            out.append(
                f"{r['symbol']} 预计 {earn['estimate']:%m-%d} 前后发财报"
                f"（推算，{earn['days_to']} 天后）"
            )
    return out


def render_look(
    sym: str,
    prof: dict[str, Any],
    spy: dict[str, Any] | None,
    *,
    member: tuple[str, str, str] | None = None,
    earn: dict[str, Any] | None = None,
    short: dict[str, Any] | None = None,
    members: set[str] | None = None,
    screen_info: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    crowded_share: float = 0.10,
    watched: dict[str, Any] | None = None,
) -> str:
    """One-page profile of a stock: figures only, no recommendation."""
    name, sector = (member[0], member[1]) if member else ("", "")
    title = " · ".join(x for x in (sym, name, sector) if x)
    lines = [
        f"# {title}（{prof['day']} 收盘）",
        "",
        "**资料汇总，不是买入建议。**" + ("" if member else "（不在当前标普 500 成分股中）"),
        "",
        "## 价格与趋势",
        f"- 收盘 ${prof['close']:,.2f}（当日 {_pct(prof['chg_1d'], '+.1%')}）；"
        f"距 50 日线 {_pct(prof['vs_sma50'])}，距 200 日线 {_pct(prof['vs_sma200'])}"
        + (
            ""
            if prof["vs_sma200"] is None
            else ("（在上方）" if prof["vs_sma200"] >= 0 else "（**在下方**）")
        )
        + f"；距一年高点 {_pct(prof['from_high'])}",
        f"- 涨跌：1 月 {_pct(prof['ret_1m'])}、3 月 {_pct(prof['ret_3m'])}、"
        f"6 月 {_pct(prof['ret_6m'])}、12 月 {_pct(prof['ret_12m'])}"
        + (f"（SPY 12 月 {_pct(spy['ret_12m'])}）" if spy else ""),
    ]
    mom = f"- 12-1 动量 {_pct(prof['momentum_12_1'])}"
    if screen_info and screen_info.get("row"):
        mom += (
            f"，在标普 500 中位于 {screen_info['row']['mom_pct']:.0%} 百分位"
            f"（100% 最强；{screen_info['day']} 候选清单）"
        )
    lines += [mom, "", "## 风险"]
    lines.append(
        f"- 60 日年化波动 {_pct(prof['vol_60'], '.0%')}"
        + (f"（SPY {_pct(spy['vol_60'], '.0%')}）" if spy else "")
        + f"；β {_pct(prof['beta'], '.2f')}（过去一年对 SPY）"
        + f"；一年内最大回撤 {_pct(prof['max_dd_1y'])}"
    )
    if short:
        dtc = short["dtc"].get(sym)
        cut = crowded_cut(short["dtc"], members or set(), crowded_share) if members else None
        if dtc is None:
            lines.append(f"- 回补天数：FINRA {short['settlement']} 文件里没有 {sym}")
        else:
            flag = ""
            if cut is not None:
                top = f"标普 500 最拥挤的 {crowded_share:.0%}"
                flag = (
                    f"，**属{top}**（≥ {cut:.1f} 天）"
                    if dtc >= cut
                    else f"（{top}的线为 {cut:.1f} 天）"
                )
            lines.append(f"- 空头回补天数 {dtc:.1f}{flag}；FINRA 结算日 {short['settlement']}")
    lines += ["", "## 财报（SEC 8-K Item 2.02）"]
    nxt = (earn or {}).get("next")
    if nxt:
        lines.append("- 最近几次业绩公告：" + "、".join(d.isoformat() for d in nxt["recent"]))
        when = (f"{nxt['days_to']} 天后" if nxt["days_to"] >= 0
                else f"已过 {-nxt['days_to']} 天，可能即将公布")  # fmt: skip
        lines.append(
            f"- 下一次约 {nxt['estimate'].isoformat()}（{when}；{nxt['basis']}，"
            f"间隔约 {nxt['gap_days']} 天；以公司公告为准）"
        )
    else:
        lines.append("- 没有拿到（需要服务器 .env 有 SEC_USER_AGENT，或该公司没有此类 8-K）")
    if screen_info:
        lines += ["", f"## 候选清单规则（{screen_info['day']} 那期）"]
        row = screen_info.get("row")
        if row is None:
            lines.append("- 不在当期评估范围（非标普 500 成分或历史不足）")
        else:
            vol_ok = screen_info.get("vol_cut") is None or row["vol"] <= screen_info["vol_cut"]
            rank = screen_info.get("rank")
            lines.append(
                f"- 200 日线上方：{'是' if row['above_sma'] else '否'}；"
                f"波动在剔除线内：{'是' if vol_ok else '否'}"
                f"（剔除线 {screen_info['vol_cut']:.0%}）；"
                + (f"当期候选第 {rank} 位" if rank else "不在当期候选")
            )
    lines += ["", "## 个股板块"]
    if watched is not None:
        note = f"，备注：{watched['note']}" if watched.get("note") else ""
        lines.append(f"- 在观察清单中（{watched.get('added', '')} 加入{note}）")
    if position is None:
        lines.append("- 还没有个股账本")
    else:
        if position.get("qty"):
            lines.append(
                f"- 已持有 {position['qty']:g} 股，成本价 {position['avg_cost']:.2f}，"
                f"盈亏 {position['pnl_pct']:+.1%}，占板块 {position['weight']:.0%}"
            )
        else:
            lines.append("- 未持有")
        cap = float((limits or {}).get("max_position_share", 1.0))
        lines.append(
            f"- 按单只 {cap:.0%} 提醒线，按收盘价最多还可买约 {position['room']} 股"
            f"（板块净值 ${position['nav']:,.0f}，现金 ${position['cash']:,.0f}）"
        )
    return "\n".join(lines) + "\n"


def render_report(years: dict[int, dict[str, float]]) -> str:
    lines = [
        "# 个股板块年度汇总",
        "",
        "| 年份 | 成交笔数 | 买入金额 | 卖出金额 | 手续费 | 已实现盈亏 | 分红及其他现金 | 入金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for y, r in years.items():
        lines.append(
            f"| {y} | {r['trades']:.0f} | ${r['bought']:,.2f} | ${r['sold']:,.2f} "
            f"| ${r['fees']:,.2f} | ${r['realized']:,.2f} | ${r['other_cash']:,.2f} "
            f"| ${r['deposits']:,.2f} |"
        )
    lines += [
        "",
        "已实现盈亏按平均成本法、含手续费；"
        "“分红及其他现金”是 cash 命令记的非入金项（税后分红、费用等）。",
        "这是账本汇总，不是税务意见；申报口径以税务机关或专业人士意见为准。",
    ]
    return "\n".join(lines) + "\n"


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


def _try_short(client: Any, day: date) -> dict[str, Any] | None:
    try:
        return latest_short(client, day)
    except Exception:  # noqa: BLE001 - the short-interest reminder is optional
        return None


def _short_alerts(held: list[str], day: date, limits: dict[str, Any]) -> list[str]:
    """Crowded-short reminders for held stocks; empty when the data cannot be fetched."""
    if not held:
        return []
    import httpx

    from us_stock_research.collectors.universe import SP500_URL, parse_sp500_meta

    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            short = latest_short(client, day)
            resp = client.get(SP500_URL)
            resp.raise_for_status()
            members = set(parse_sp500_meta(resp.text)[0])
    except Exception:  # noqa: BLE001 - optional reminder
        return []
    return short_notes(held, short, members, float(limits.get("short_crowded_share", 0.10)))


def _history(client: Any, symbols: list[str], day: date, days: int = 420) -> dict[str, Any]:
    """Adjusted daily closes (splits and dividends) from Alpaca: {symbol: [(day, close)]}."""
    from us_stock_research.collectors.alpaca_daily import alpaca_keys, fetch_many

    keys = alpaca_keys()
    if not keys or not symbols:
        return {}
    bars = fetch_many(client, symbols, day - timedelta(days=days), day, *keys, adjustment="all")
    return {s: [(b.day, b.close) for b in b_ if b.day <= day] for s, b_ in bars.items()}


def _sp500(client: Any) -> dict[str, tuple[str, str, str]]:
    """Current S&P 500 members: {symbol: (name, sector, CIK)}."""
    from us_stock_research.collectors.universe import SP500_URL, parse_sp500_meta

    resp = client.get(SP500_URL)
    resp.raise_for_status()
    c = parse_sp500_meta(resp.text)
    return {s: (n, sec, cik) for s, n, sec, cik in zip(c[0], c[1], c[2], c[5], strict=True)}


def _sec_client() -> Any:
    import os

    import httpx

    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        return None
    return httpx.Client(timeout=60, headers={"User-Agent": agent}, follow_redirects=True)


def _earnings(symbols: list[str], ciks: dict[str, str], today: date) -> dict[str, Any]:
    """Next-earnings estimates from SEC submissions (needs SEC_USER_AGENT; empty otherwise)."""
    import time

    from us_stock_research.trading.stock_analysis import earnings_dates, next_earnings

    sec = _sec_client() if symbols else None
    if sec is None:
        return {}
    out: dict[str, Any] = {}
    with sec:
        missing = [s for s in symbols if not ciks.get(s)]
        if missing:
            try:
                tickers = sec.get("https://www.sec.gov/files/company_tickers.json").json()
                by = {str(t["ticker"]).upper().replace(".", "-"): str(t["cik_str"])
                      for t in tickers.values()}  # fmt: skip
                ciks = {**ciks, **{s: by[s] for s in missing if s in by}}
            except Exception:  # noqa: BLE001 - optional
                pass
        for s in symbols:
            cik = str(ciks.get(s) or "").strip()
            if not cik.isdigit():
                continue
            try:
                r = sec.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
                r.raise_for_status()
                dates = earnings_dates(r.json())
                out[s] = {"dates": dates, "next": next_earnings(dates, today)}
            except Exception:  # noqa: BLE001 - optional
                continue
            time.sleep(0.15)
    return out


def _enrich(rows: list[dict[str, Any]], held: list[str], day: date) -> None:
    """Attach trend, risk, sector and next-earnings figures to valuation rows (best effort)."""
    import httpx

    from us_stock_research.trading.stock_analysis import profile

    if not held:
        return
    members: dict[str, tuple[str, str, str]] = {}
    hist: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            hist = _history(client, held + ["SPY"], day)
            members = _sp500(client)
    except Exception:  # noqa: BLE001 - extra figures are optional
        pass
    earn = _earnings(held, {s: m[2] for s, m in members.items()}, day)
    for r in rows:
        s = r["symbol"]
        prof = profile(hist.get(s) or [], hist.get("SPY")) or {}
        for k in ("chg_1d", "vs_sma200", "beta", "vol_60"):
            r[k] = prof.get(k)
        r["sector"] = members[s][1] if s in members else ""
        r["earnings"] = (earn.get(s) or {}).get("next")


def _load_watch(path: Path) -> dict[str, dict[str, Any]]:
    from us_stock_research.trading.stock_analysis import load_watch

    try:
        return load_watch(path)
    except Exception:  # noqa: BLE001 - a broken watch file must not stop the rest
        return {}


def render_watch(watch: dict[str, dict[str, Any]]) -> str:
    if not watch:
        return "观察清单是空的：bash scripts/stocks.sh watch add 代码 [代码 ...]"
    lines = [
        f"观察清单（{len(watch)} 只；每日检查，状态变化时提醒一次）",
        "",
        "| 代码 | 加入 | 200 日线上方 | 在每周候选中 | 备注 |",
        "|---|---|---|---|---|",
    ]

    def yn(x: Any) -> str:
        return "—" if x is None else ("是" if x else "否")

    for s_, w in sorted(watch.items()):
        lines.append(
            f"| {s_} | {w.get('added', '')} | {yn(w.get('above_sma'))} "
            f"| {yn(w.get('in_screen'))} | {w.get('note', '')} |"
        )
    lines += ["", "单只详情：bash scripts/stocks.sh look 代码"]
    return "\n".join(lines)


def _watch_monitor(path: Path, day: date, limits: dict[str, Any], screens: Path) -> list[str]:
    """Daily watchlist check (best effort; returns reminders, saves the new state)."""
    import httpx

    from us_stock_research.trading.stock_analysis import profile, save_watch, watch_events

    watch = _load_watch(path)
    if not watch:
        return []
    syms = sorted(watch)
    members: dict[str, tuple[str, str, str]] = {}
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            hist = _history(client, syms + ["SPY"], day)
            members = _sp500(client)
    except Exception:  # noqa: BLE001 - optional
        return []
    earn = _earnings(syms, {s: m[2] for s, m in members.items()}, day)
    figures = {}
    for s in syms:
        prof = profile(hist.get(s) or [], hist.get("SPY")) or {}
        figures[s] = {
            "vs_sma200": prof.get("vs_sma200"),
            "earnings": (earn.get(s) or {}).get("next"),
        }
    files = _screen_files(screens)
    candidates = set(json.loads(files[-1].read_text()).get("candidates") or []) if files else None
    within = limits.get("earnings_within_days")
    notes = watch_events(watch, figures, candidates, int(within) if within is not None else None)
    try:
        save_watch(path, watch)
    except OSError:
        pass
    return notes


def _enrich_candidates(
    rows: list[dict[str, Any]], bars: dict[str, Any], members: dict[str, Any], day: date
) -> None:
    """Beta, one-year drawdown and next-earnings estimate for the weekly candidates."""
    from us_stock_research.trading.stock_analysis import profile

    spy = [(b.day, b.close) for b in bars.get("SPY") or [] if b.day <= day]
    earn = _earnings(
        [r["symbol"] for r in rows],
        {r["symbol"]: members[r["symbol"]][2] for r in rows if r["symbol"] in members},
        day,
    )
    for r in rows:
        series = [(b.day, b.close) for b in bars.get(r["symbol"]) or [] if b.day <= day]
        prof = profile(series, spy) or {}
        r["beta"], r["max_dd"] = prof.get("beta"), prof.get("max_dd_1y")
        r["earnings"] = (earn.get(r["symbol"]) or {}).get("next")


def _screen_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("screen_*.json")) if folder.exists() else []


def _previous_screen(folder: Path, day: date) -> dict[str, Any] | None:
    older = [f for f in _screen_files(folder) if f.stem.removeprefix("screen_") < day.isoformat()]
    if not older:
        return None
    data: dict[str, Any] = json.loads(older[-1].read_text())
    return data


def _screen_info(folder: Path, sym: str, day: date) -> dict[str, Any] | None:
    files = [f for f in _screen_files(folder) if f.stem.removeprefix("screen_") <= day.isoformat()]
    if not files:
        return None
    data = json.loads(files[-1].read_text())
    cands = list(data.get("candidates") or [])
    return {
        "day": data["day"],
        "row": (data.get("rows") or {}).get(sym),
        "vol_cut": data.get("vol_cut"),
        "rank": cands.index(sym) + 1 if sym in cands else None,
    }


def _last_session() -> date:
    from us_stock_research.quality.intraday import last_closed_session

    return last_closed_session(datetime.now(UTC))


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912, PLR0915 - one small CLI
    from us_stock_research.config import load_dotenv

    load_dotenv()  # Alpaca keys live in the server's .env
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
        p.add_argument("--force", action="store_true", help="record by hand although sync is on")
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
    p = sub.add_parser("look")
    p.add_argument("--symbol", required=True)
    p.add_argument("--day", type=date.fromisoformat)
    p.add_argument("--screens", type=Path, default=Path("artifacts/stocks"))
    sub.add_parser("report")
    p = sub.add_parser("watch")
    p.add_argument("action", choices=("add", "rm", "list"))
    p.add_argument("symbols", nargs="*")
    p.add_argument("--note", default="")
    p = sub.add_parser("screen")
    p.add_argument("--out-dir", type=Path, default=Path("artifacts/stocks"))
    p.add_argument("--day", type=date.fromisoformat)
    p.add_argument(
        "--adhoc",
        action="store_true",
        help="on-demand run: write adhoc_<day>.md (not part of the weekly comparison) and print it",
    )
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    path, fills, limits = Path(cfg["ledger"]), Path(cfg["fills"]), cfg.get("alerts") or {}
    watch_path = Path(cfg.get("watchlist", "portfolio/stocks/watchlist.yml"))
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
            from us_stock_research.trading.schwab_api import auto_recording_on

            if auto_recording_on() and not args.force:
                print("未执行：嘉信只读同步已打开，成交会自动记账；确需手动记录请加 --force")
                return 2
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
        elif args.cmd == "watch":
            from us_stock_research.trading.stock_analysis import load_watch, save_watch

            watch = load_watch(watch_path)
            syms = [x.upper().replace(".", "-") for x in args.symbols]
            bad = [x for x in syms if not x.replace("-", "").isalnum() or len(x) > 10]
            if bad:
                raise ValueError(f"代码格式不对：{'、'.join(bad)}")
            if args.action == "add":
                if not syms:
                    raise ValueError("用法：watch add 代码 [代码 ...] [--note 备注]")
                if len(set(watch) | set(syms)) > 50:
                    raise ValueError("观察清单最多 50 只")
                today = datetime.now(UTC).date().isoformat()
                for x in syms:
                    watch.setdefault(x, {"added": today})
                    if args.note:
                        watch[x]["note"] = args.note
                save_watch(watch_path, watch)
                print(f"已加入观察清单：{'、'.join(syms)}（共 {len(watch)} 只）")
            elif args.action == "rm":
                gone = [x for x in syms if watch.pop(x, None) is not None]
                save_watch(watch_path, watch)
                print(f"已移出：{'、'.join(gone) or '无'}（共 {len(watch)} 只）")
            else:
                print(render_watch(watch))
        elif args.cmd == "monitor":
            day = _last_session()
            watch_notes = _watch_monitor(watch_path, day, limits, args.output.parent)
            if not path.exists():
                warn = f"；提醒：{'；'.join(watch_notes)}" if watch_notes else ""
                print(f"个股板块：未建账本{warn}")
                return 0
            from us_stock_research.trading.stock_analysis import deposits_of, shadow_spy

            ledger = load(path)
            held = list((ledger.get("positions") or {}).keys())
            prices = _latest_raw_closes(held, day)
            v = valuation(ledger, prices)
            _enrich(v["rows"], held, day)
            notes = alerts(ledger, v, limits)
            notes += holding_alerts(v["rows"], limits)
            notes += _short_alerts(held, day, limits)
            notes += watch_notes
            ledger["peak_nav_usd"] = round(max(float(ledger.get("peak_nav_usd") or 0), v["nav"]), 2)
            spy = _spy_adj(day)
            bench_ret, shadow = None, None
            if spy:
                start = date.fromisoformat(str(ledger["start_day"]))
                base = ledger.get("benchmark_start")
                if base is None:
                    prior = [d for d in spy if d <= start]
                    base = spy[max(prior)] if prior else None
                    ledger["benchmark_start"] = base
                if base:
                    bench_ret = spy[max(spy)] / float(base) - 1
                shadow = shadow_spy(deposits_of(ledger), spy, day)
            save(path, ledger)
            status_extra = {"shadow": shadow}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_status(ledger, v, notes, bench_ret, day, status_extra))
            args.output.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "day": day.isoformat(),
                        "nav": v["nav"],
                        "funded": ledger["funded_usd"],
                        "benchmark_return": bench_ret,
                        "shadow_spy": shadow,
                        "alerts": notes,
                        "rows": v["rows"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n"
            )
            print(summary_line(ledger, v, notes, bench_ret, shadow))
        elif args.cmd == "report":
            from us_stock_research.trading.stock_analysis import yearly_summary

            print(render_report(yearly_summary(fills, load(path))), end="")
        elif args.cmd == "look":
            import httpx

            from us_stock_research.trading.stock_analysis import profile

            sym = args.symbol.upper().replace(".", "-")
            if sym in STRATEGY_SYMBOLS:
                print(f"提示：{sym} 是实盘策略使用的代码，个股板块不能买卖它")
            day = args.day or _last_session()
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                hist = _history(client, [sym, "SPY"], day)
                try:
                    members = _sp500(client)
                except Exception:  # noqa: BLE001 - optional
                    members = {}
                short = _try_short(client, day)
            prof = profile(hist.get(sym) or [], hist.get("SPY"))
            if prof is None:
                print(f"没有拿到 {sym} 的行情（代码是否正确？.env 是否有 Alpaca 密钥？）")
                return 2
            spy_prof = profile(hist.get("SPY") or [])
            member = members.get(sym)
            earn = _earnings([sym], {sym: member[2]} if member else {}, day).get(sym)
            position = None
            if path.exists():
                ledger = load(path)
                held = list((ledger.get("positions") or {}).keys())
                marks = _latest_raw_closes([h for h in held if h != sym], day)
                marks[sym] = prof["close"]
                v = valuation(ledger, marks)
                row = next((r for r in v["rows"] if r["symbol"] == sym), None)
                cap = float(limits.get("max_position_share", 1.0))
                have = row["value"] if row else 0.0
                position = {
                    "qty": row["qty"] if row else 0.0,
                    "avg_cost": row["avg_cost"] if row else 0.0,
                    "pnl_pct": row["pnl_pct"] if row else 0.0,
                    "weight": row["weight"] if row else 0.0,
                    "nav": v["nav"],
                    "cash": v["cash"],
                    "room": max(0, math.floor((cap * v["nav"] - have) / prof["close"])),
                }
            print(
                render_look(
                    sym,
                    prof,
                    spy_prof,
                    member=member,
                    earn=earn,
                    short=short,
                    members=set(members),
                    screen_info=_screen_info(args.screens, sym, day),
                    position=position,
                    limits=limits,
                    crowded_share=float(limits.get("short_crowded_share", 0.10)),
                    watched=_load_watch(watch_path).get(sym),
                ),
                end="",
            )
        else:  # screen
            import httpx

            from us_stock_research.collectors.alpaca_daily import alpaca_keys, fetch_many
            from us_stock_research.trading.stock_analysis import holding_checks, screen_changes

            keys = alpaca_keys()
            if not keys:
                print("个股候选：.env 里没有 Alpaca 密钥，跳过")
                return 0
            day = args.day or _last_session()
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                members = _sp500(client)
                meta = {s_: (n, sec) for s_, (n, sec, _) in members.items()}
                bars = fetch_many(
                    client,
                    sorted(meta) + ["SPY"],
                    day - timedelta(days=420),
                    day,
                    *keys,
                    adjustment="all",
                )
                short = _try_short(client, day)
            series = {s_: [b.close for b in b_ if b.day <= day] for s_, b_ in bars.items()}
            spy_closes = series.pop("SPY", [])
            held_set: set[str] = set()
            if path.exists():
                held_set = set((load(path).get("positions") or {}).keys())
            result = screen(series, meta, cfg["screen"], held_set, short)
            bench = screen_metrics(spy_closes, cfg["screen"]) if spy_closes else None
            picked = [r["symbol"] for r in result["candidates"]]
            _enrich_candidates(result["candidates"], bars, members, day)
            args.out_dir.mkdir(parents=True, exist_ok=True)
            prev = None if args.adhoc else _previous_screen(args.out_dir, day)
            changes = screen_changes(picked, prev["candidates"] if prev else None)
            checks = holding_checks(sorted(held_set), result["rows"], picked, result["vol_cut"])
            watched = sorted(_load_watch(watch_path))
            wchecks = holding_checks(watched, result["rows"], picked, result["vol_cut"])
            name = f"adhoc_{day.isoformat()}.md" if args.adhoc else f"screen_{day.isoformat()}.md"
            out = args.out_dir / name
            md = render_screen(
                result,
                day,
                bench,
                changes,
                checks,
                prev["day"] if prev else None,
                wchecks,
                int(cfg["screen"].get("earnings_flag_days", 14)),
            )
            if args.adhoc:
                md = md.replace("# 个股候选清单（", "# 个股候选清单·临时重筛（", 1)
            out.write_text(md)
            if args.adhoc:
                print(md)
                return 0
            out.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "day": day.isoformat(),
                        "candidates": picked,
                        "vol_cut": result["vol_cut"],
                        "rows": {
                            s_: {k: r[k] for k in ("mom_pct", "momentum_12_1", "above_sma", "vol")}
                            for s_, r in result["rows"].items()
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            top = "、".join(picked[:5])
            new_txt = ""
            if changes is not None:
                new_txt = f"；新进 {len(changes['new'])} 只、掉出 {len(changes['dropped'])} 只"
            weak = [h["symbol"] for h in checks if h["evaluated"] and not h["above_sma"]]
            weak_txt = f"；持仓跌破 200 日线：{'、'.join(weak)}" if weak else ""
            print(f"个股候选：{len(picked)} 只（前 5：{top}{new_txt}{weak_txt}）→ {out}")
    except ValueError as exc:
        print(f"未执行：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - network/data errors must not crash the daily run
        code = getattr(getattr(exc, "response", None), "status_code", None)
        what = f"HTTP {code}" if code else type(exc).__name__
        print(f"个股{'候选' if args.cmd == 'screen' else '板块'}：数据下载失败（{what}），本次跳过")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
