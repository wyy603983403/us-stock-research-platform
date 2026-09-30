"""Historical ticker -> SEC CIK map for every S&P 500 membership interval (offline).

Form 4 filings state both the issuer's CIK and the ticker it traded under *at that time*, so the
insider data sets give dated (ticker, CIK) pairs back to 2006 -- including delisted companies and
tickers later re-used by someone else. For each membership interval the CIK whose filings under
that ticker overlap the interval the most is chosen; ties or no overlap are reported, not guessed.
``configs/cik_overrides.yml`` (``SYMBOL: CIK``) wins over the data when a human has checked.

Output: ``parquet/meta/ticker_cik.parquet`` (symbol, start_date, end_date, cik, issuer_name,
source, filings_in_interval).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.collectors.sec_bulk import INSIDER_KIND
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore

SCHEMA = {
    "symbol": "VARCHAR",
    "start_date": "DATE",
    "end_date": "DATE",
    "cik": "BIGINT",
    "issuer_name": "VARCHAR",
    "source": "VARCHAR",
    "filings_in_interval": "INTEGER",
}
Filing = tuple[str, int, date, str]  # yahoo symbol, cik, filing date, issuer name


def yahoo(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def _in_interval(
    filings: list[tuple[int, date, str]], start: date, end: date | None
) -> list[tuple[int, date, str]]:
    kept = [f for f in filings if f[1] >= start and (end is None or f[1] < end)]
    return sorted(kept, key=lambda f: (f[1], f[0]))  # by filing date


def segments(found: list[tuple[int, date, str]]) -> list[tuple[int, date, str, int]] | None:
    """Split a membership interval where the filer's CIK changes over time.

    A company that re-domiciles or reorganises (Eaton 2012, Medtronic 2015, Cigna 2018) keeps its
    ticker but files under a new CIK from some date on: the filings form consecutive runs
    A..A B..B. Returns ``[(cik, first_filing, name, n), ...]`` for such clean sequences, the
    dominant CIK when one filer has >= 70% of filings (parent and subsidiary both reporting),
    or None when CIKs interleave with no clear owner.
    """
    runs: list[list[Any]] = []
    for cik, filed, name in found:
        if runs and runs[-1][0] == cik:
            runs[-1][3] += 1
        else:
            runs.append([cik, filed, name, 1])
    runs = [r for r in runs if r[3] >= 2] or runs  # drop single stray filings
    merged: list[list[Any]] = []
    for r in runs:
        if merged and merged[-1][0] == r[0]:
            merged[-1][3] += r[3]
        else:
            merged.append(list(r))
    ciks = [r[0] for r in merged]
    if len(ciks) == len(set(ciks)):
        return [(r[0], r[1], r[2], r[3]) for r in merged]
    totals: dict[int, int] = {}
    for cik, _f, _n in found:
        totals[cik] = totals.get(cik, 0) + 1
    best = max(totals, key=lambda c: totals[c])
    if totals[best] >= 0.7 * len(found):
        name = next(n for c, _f, n in found if c == best)
        return [(best, found[0][1], name, totals[best])]
    return None


def candidates(symbol: str, aliases_back: dict[str, str]) -> list[tuple[str, str]]:
    """Other tickers the same company may have filed under, with the rule that produced them."""
    out: list[tuple[str, str]] = []
    if symbol in aliases_back:
        out.append((aliases_back[symbol], "alias"))  # BNY filed as BK before the change
    plain = symbol.replace("-", "")
    if plain != symbol:
        out.append((plain, "class"))  # BF-B filed as BFB
    if "-" in symbol:
        out.append((symbol.split("-")[0] + "-A", "class"))
    out.append((symbol + "A", "class"))  # FOX filed as FOXA
    if symbol.endswith("A") and len(symbol) >= 4:
        out.append((symbol[:-1], "class"))  # NWSA filed as NWS
    if symbol.endswith("K") and len(symbol) >= 4:
        out.append((symbol[:-1] + "A", "class"))  # CMCSK filed as CMCSA
    if symbol.endswith("Q") and len(symbol) >= 4:  # bankrupt: BTUUQ traded as BTU before
        out += [(symbol[:-1], "bankrupt"), (symbol[:-2], "bankrupt")]
    seen, unique = {symbol}, []
    for cand, rule in out:
        if cand and cand not in seen and len(cand) >= 2:
            seen.add(cand)
            unique.append((cand, rule))
    return unique


def choose(
    intervals: list[tuple[str, date, date | None]],
    filings: dict[str, list[tuple[int, date, str]]],
    overrides: dict[str, int | None],
    since: date,
    aliases: dict[str, str] | None = None,
    data_end: date | None = None,
) -> tuple[list[list[Any]], dict[str, int]]:
    aliases_back = {new: old for old, new in (aliases or {}).items()}
    rows: list[list[Any]] = []
    stats = {"override": 0, "insider": 0, "fallback": 0, "split": 0, "ambiguous": 0}
    stats |= {"unmapped": 0, "before_since": 0}
    for symbol, start, end in intervals:
        if end is not None and end <= since:
            stats["before_since"] += 1
            continue
        key = f"{symbol}@{start.isoformat()}"
        if key in overrides or symbol in overrides:
            cik = overrides.get(key, overrides.get(symbol))
            label = "override" if cik is not None else "override:no_sec_filer"
            rows.append([symbol, start, end, cik, None, label, None])
            stats["override"] += 1
            continue
        found = _in_interval(filings.get(symbol, []), start, end)
        source = "insider"
        if not found:
            for cand, rule in candidates(symbol, aliases_back):
                found = _in_interval(filings.get(cand, []), start, end)
                if found:
                    source = f"{rule}:{cand}"
                    break
        if not found and data_end is not None and start > data_end:
            # membership began after the last filing in the data: take whoever filed most
            # recently under the old ticker (renames) or this ticker, if within a year
            order = [(symbol, "recent")]
            if symbol in aliases_back:
                order.insert(0, (aliases_back[symbol], "recent-alias"))
            for cand, rule in order:
                recent = [f for f in filings.get(cand, []) if f[1] >= start - timedelta(days=365)]
                if recent:
                    last = max(recent, key=lambda f: f[1])
                    found = [last]
                    source = f"{rule}:{cand}"
                    break
        if not found:
            rows.append([symbol, start, end, None, None, "unmapped", 0])
            stats["unmapped"] += 1
            continue
        segs = segments(found)
        if segs is None:
            rows.append([symbol, start, end, None, found[-1][2], "ambiguous", len(found)])
            stats["ambiguous"] += 1
            continue
        for k, (cik, first, name, n) in enumerate(segs):
            seg_start = start if k == 0 else first
            seg_end = segs[k + 1][1] if k + 1 < len(segs) else end
            label = source if len(segs) == 1 else f"{source}+split"
            rows.append([symbol, seg_start, seg_end, cik, name, label, n])
        stats["split" if len(segs) > 1 else ("insider" if source == "insider" else "fallback")] += 1
    return rows, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=date.fromisoformat, default=date(2009, 1, 1))
    parser.add_argument("--overrides", type=Path, default=Path("configs/cik_overrides.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    tables = TableStore.from_settings(load_settings())
    if not tables.keys(INSIDER_KIND):
        print(
            "no insider data yet: run usr-collect-sec-bulk --what insider --execute",
            file=sys.stderr,
        )
        return 2
    pattern = str(tables.root / "parquet" / INSIDER_KIND / "*.parquet").replace("'", "''")
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        raw = con.execute(
            "SELECT DISTINCT issuer_symbol, issuer_cik, filing_date, issuer_name "
            f"FROM read_parquet('{pattern}') "
            "WHERE issuer_symbol IS NOT NULL AND issuer_cik IS NOT NULL AND filing_date IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    filings: dict[str, list[tuple[int, date, str]]] = {}
    for sym, cik, filed, name in raw:
        for part in str(sym).replace(";", ",").split(","):  # some filings list several classes
            if part.strip():
                filings.setdefault(yahoo(part), []).append((int(cik), filed, name))
    overrides: dict[str, int | None] = {}
    if args.overrides.exists():
        for k, v in (yaml.safe_load(args.overrides.read_text()) or {}).items():
            overrides[str(k)] = None if str(v).lower() == "none" else int(v)
    history = [
        tuple(r) for r in tables.read("meta", "sp500_history", "symbol, start_date, end_date")
    ]
    aliases: dict[str, str] = {}
    if args.aliases.exists():
        raw_aliases = (yaml.safe_load(args.aliases.read_text()) or {}).get("aliases") or {}
        aliases = {str(k): str(v) for k, v in raw_aliases.items()}
    data_end = max(f[1] for fl in filings.values() for f in fl)
    rows, stats = choose(history, filings, overrides, args.since, aliases, data_end)  # type: ignore[arg-type]
    # a renamed ticker (FB -> META) filed under its new symbol after the rename; nothing to do,
    # the interval of each symbol is matched against filings made under that same symbol.
    tables.write("meta", "ticker_cik", SCHEMA, [[r[k] for r in rows] for k in range(7)], "symbol")
    unmapped = sorted({f"{r[0]}({r[5]})" for r in rows if r[5] in ("unmapped", "ambiguous")})
    fallbacks = sorted({f"{r[0]}<-{r[5]}" for r in rows if ":" in str(r[5]) or "split" in r[5]})
    summary = {
        "rows": len(rows),
        "by_source": stats,
        "mapped_share_of_intervals": round(
            1
            - (stats["unmapped"] + stats["ambiguous"])
            / max(1, sum(v for k, v in stats.items() if k != "before_since")),
            4,
        ),
        "unmapped_or_ambiguous": unmapped,
        "fallback_or_split": fallbacks,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
