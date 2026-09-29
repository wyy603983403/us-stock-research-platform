"""Data catalog / security master: what is in the store, how fresh and how clean it is.

Writes ``parquet/meta/securities.parquet`` (one row per symbol) and a readable
``artifacts/catalog.md``. Kinds come from ``configs/universes`` (etf / macro / stock); names and
sectors from the S&P 500 constituents table when it has been downloaded.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.config import load_settings
from us_stock_research.quality.ohlcv import load_exceptions
from us_stock_research.storage import BarStore, open_store
from us_stock_research.tables import TableStore

KIND_BY_UNIVERSE = {"etf_core": "etf", "macro_indices": "macro", "mega_caps": "stock"}
FRESH_DAYS = 5  # weekends and holidays
SCHEMA = {
    "symbol": "VARCHAR",
    "kind": "VARCHAR",
    "name": "VARCHAR",
    "sector": "VARCHAR",
    "rows": "INTEGER",
    "first": "DATE",
    "last": "DATE",
    "dividends": "INTEGER",
    "stale_days": "INTEGER",
    "quality_errors": "INTEGER",
    "quality_warnings": "INTEGER",
    "quarantined": "BOOLEAN",
}


def universe_kinds(universes_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, kind in KIND_BY_UNIVERSE.items():
        path = universes_dir / f"{name}.yml"
        if path.exists():
            for symbol in yaml.safe_load(path.read_text())["symbols"]:
                out[str(symbol)] = kind
    return out


def build_rows(
    store: BarStore,
    kinds: dict[str, str],
    today: date,
    sp500: dict[str, tuple[str, str]] | None = None,
    quality: dict[str, tuple[int, int]] | None = None,
    quarantine: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One row per stored symbol. ``sp500`` maps symbol -> (name, sector)."""
    sp500, quality = sp500 or {}, quality or {}
    rows: list[dict[str, Any]] = []
    for symbol in store.symbols():
        bars = store.read_bars(symbol)
        name, sector = sp500.get(symbol, ("", ""))
        errors, warnings = quality.get(symbol, (0, 0))
        n_dividends = len(store.read_dividends(symbol)) if store.has_dividends(symbol) else 0
        rows.append(
            {
                "symbol": symbol,
                "kind": kinds.get(symbol) or ("stock" if symbol in sp500 else "other"),
                "name": name,
                "sector": sector,
                "rows": len(bars),
                "first": bars[0].day,
                "last": bars[-1].day,
                "dividends": n_dividends,
                "stale_days": (today - bars[-1].day).days,
                "quality_errors": errors,
                "quality_warnings": warnings,
                "quarantined": symbol in (quarantine or {}),
            }
        )
    return rows


def render_markdown(
    rows: list[dict[str, Any]], macro: dict[str, tuple[date, int]], fundamentals: list[str]
) -> str:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    stale = [r["symbol"] for r in rows if r["stale_days"] > FRESH_DAYS]
    flagged = [r["symbol"] for r in rows if r["quality_errors"]]
    lines = [
        "# 数据目录",
        "",
        f"价格数据 {len(rows)} 只；宏观序列 {len(macro)} 个；基本面 {len(fundamentals)} 家。",
        "",
        "## 价格数据",
        "",
        "| 类型 | 数量 | 最早 | 最新 | 总行数 | 未更新(>5天) | 质量错误 |",
        "|---|---:|---|---|---:|---:|---:|",
    ]
    for kind, items in sorted(by_kind.items()):
        lines.append(
            f"| {kind} | {len(items)} | {min(i['first'] for i in items)} "
            f"| {max(i['last'] for i in items)} | {sum(i['rows'] for i in items):,} "
            f"| {sum(i['stale_days'] > FRESH_DAYS for i in items)} "
            f"| {sum(bool(i['quality_errors']) for i in items)} |"
        )
    lines += ["", f"需要人工复核（质量错误）：{', '.join(flagged) or '无'}"]
    quarantined = [r["symbol"] for r in rows if r.get("quarantined")]
    lines += [f"已隔离（数据不可信，禁止用于研究）：{', '.join(quarantined) or '无'}"]
    lines += [f"数据过旧：{', '.join(stale) or '无'}", "", "## 宏观序列", ""]
    lines += [f"- {sid}：{n:,} 条，最新 {last}" for sid, (last, n) in sorted(macro.items())]
    if not macro:
        lines.append("- 尚未下载（运行 usr-collect-macro --execute）")
    lines += ["", "## 基本面（SEC EDGAR，按披露日点对点）", ""]
    lines.append(
        ", ".join(fundamentals) if fundamentals else "尚未下载（见 usr-collect-fundamentals）"
    )
    sectors: dict[str, int] = {}
    for r in rows:
        if r["sector"]:
            sectors[r["sector"]] = sectors.get(r["sector"], 0) + 1
    if sectors:
        lines += ["", "## 标普 500 行业分布（当前成分股，有幸存者偏差）", ""]
        lines += [f"- {k}：{v}" for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universes-dir", type=Path, default=Path("configs/universes"))
    parser.add_argument("--quality", type=Path, default=Path("artifacts/quality/universe.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/catalog.md"))
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    args = parser.parse_args(argv)
    settings = load_settings()
    store = open_store(settings)
    tables = TableStore(settings.storage_root) if settings.storage_root else None
    sp500: dict[str, tuple[str, str]] = {}
    macro: dict[str, tuple[date, int]] = {}
    fundamentals: list[str] = []
    if tables:
        if tables.has("meta", "sp500"):
            for symbol, name, sector in tables.read("meta", "sp500", "symbol, name, sector"):
                sp500[symbol] = (name, sector)
        for sid in tables.keys("macro"):
            ((last, n),) = tables.read("macro", sid, "max(date), count(*)")
            macro[sid] = (last, n)
        fundamentals = tables.keys("fundamentals")
    quality: dict[str, tuple[int, int]] = {}
    if args.quality.exists():
        for r in json.loads(args.quality.read_text())["reports"]:
            quality[r["symbol"]] = (len(r["errors"]), len(r["warnings"]))
    rows = build_rows(
        store,
        universe_kinds(args.universes_dir),
        datetime.now(UTC).date(),
        sp500,
        quality,
        load_exceptions(args.exceptions)[1],
    )
    if tables and rows:
        tables.write(
            "meta",
            "securities",
            SCHEMA,
            [[r[name] for r in rows] for name in SCHEMA],
            "kind, symbol",
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(rows, macro, fundamentals)
    args.output.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
