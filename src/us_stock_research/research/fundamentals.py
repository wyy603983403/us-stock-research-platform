"""Point-in-time annual fundamentals and the quality+value score (study sp500_quality_value_pit).

Inputs, all from the local store:

* ``sec_fsds`` -- SEC financial statement data sets, one row per (filing, tag, date, period
  length). Only annual reports (10-K, 10-K/A, 10-KT) and the contract's tags are read: flows with
  ``qtrs = 4`` and stocks with ``qtrs = 0``, both dated at the report's period end.
* ``meta/ticker_cik`` -- which SEC filer used a ticker when (built from Form 4 filings).
* ``close`` prices and split events -- for the market value. Yahoo's ``close`` is split-adjusted
  to today and is turned back into the day's actual price with the later splits; Tiingo-sourced
  former members are stored unadjusted with a ``split_factor`` column.

Point-in-time rule: on a signal day only reports *filed* on or before it are used. Share counts
in a report are restated for splits that happened before the filing (ASC 260), so only splits
after the filing date are applied to them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT", "10-KT/A")
ITEMS = ("net_income", "operating_cash_flow", "assets", "equity", "shares")
FLOW_ITEMS = ("net_income", "operating_cash_flow", "shares")
QUALITY = ("roa", "cfoa")
VALUE = ("earnings_yield", "book_to_market", "cash_flow_yield")
STALE = 5


@dataclass
class Report:
    adsh: str
    cik: int
    sic: int | None
    period: date
    filed: date
    values: dict[str, float] = field(default_factory=dict)  # tag -> value


Splits = list[tuple[date, float]]  # (effective day, new shares per old share)


def tag_lists(spec: dict[str, Any]) -> dict[str, list[str]]:
    return {item: [str(t) for t in spec[item]] for item in ITEMS}


def parse_rows(rows: list[tuple[Any, ...]], tags: dict[str, list[str]]) -> dict[int, list[Report]]:
    """``adsh, cik, sic, period, filed, tag, qtrs, uom, value`` rows (``ddate = period``)."""
    flow_tags = {t for item in FLOW_ITEMS for t in tags[item]}
    wanted = {t for ts in tags.values() for t in ts}
    shares_tags = set(tags["shares"])
    reports: dict[str, Report] = {}
    for adsh, cik, sic, period, filed, tag, qtrs, uom, value in rows:
        if tag not in wanted or value is None:
            continue
        if (qtrs == 4) != (tag in flow_tags):
            continue
        if uom != ("shares" if tag in shares_tags else "USD"):
            continue
        rep = reports.setdefault(adsh, Report(adsh, int(cik), sic, period, filed))
        rep.values.setdefault(tag, float(value))
    out: dict[int, list[Report]] = {}
    for rep in reports.values():
        out.setdefault(rep.cik, []).append(rep)
    for reps in out.values():
        reps.sort(key=lambda r: (r.filed, r.adsh))
    return out


def load_reports(tables: Any, tags: dict[str, list[str]]) -> tuple[dict[int, list[Report]], str]:
    """Annual reports of every CIK in ``meta/ticker_cik`` and a content hash of the rows used."""
    pattern = str(tables.root / "parquet" / "sec_fsds" / "*.parquet").replace("'", "''")
    cik_file = str(tables.path("meta", "ticker_cik")).replace("'", "''")
    tag_sql = ", ".join("'" + t.replace("'", "''") + "'" for ts in tags.values() for t in ts)
    forms = ", ".join(f"'{f}'" for f in ANNUAL_FORMS)
    con = tables._duckdb().connect()  # noqa: SLF001
    try:
        rows = con.execute(
            "SELECT adsh, cik, sic, period, filed, tag, qtrs, uom, value "
            f"FROM read_parquet('{pattern}') WHERE form IN ({forms}) AND ddate = period "
            f"AND qtrs IN (0, 4) AND tag IN ({tag_sql}) "
            f"AND cik IN (SELECT cik FROM read_parquet('{cik_file}')) "
            "ORDER BY adsh, tag, qtrs, uom, value"
        ).fetchall()
    finally:
        con.close()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode())
    return parse_rows(rows, tags), "fsds-annual:sha256:" + digest.hexdigest()


def resolve(
    reports: list[Report], day: date, tags: dict[str, list[str]], max_age_days: int
) -> dict[str, Any] | None:
    """Items of the newest annual period known on ``day``; amendments win item by item."""
    known = [r for r in reports if r.filed <= day]
    if not known:
        return None
    period = max(r.period for r in known)
    if (day - period).days > max_age_days:
        return None
    same = sorted((r for r in known if r.period == period), key=lambda r: (r.filed, r.adsh))
    items: dict[str, Any] = {"period": period, "sic": same[-1].sic, "filed": None}
    for item, item_tags in tags.items():
        items[item] = None
        for rep in reversed(same):
            hit = next((rep.values[t] for t in item_tags if t in rep.values), None)
            if hit is not None:
                items[item] = hit
                if item == "shares":
                    items["filed"] = rep.filed  # share counts are restated up to this filing
                break
    if items["filed"] is None:
        items["filed"] = same[-1].filed
    return items


def split_ratio(splits: Splits, after: date, through: date | None) -> float:
    """Product of split ratios effective in (after, through]; ``through=None`` = no end."""
    out = 1.0
    for d, r in splits:
        if d > after and (through is None or d <= through):
            out *= r
    return out


def cik_on(segments: list[tuple[date, date | None, int | None]], day: date) -> int | None:
    for start, end, cik in segments:
        if start <= day and (end is None or day < end):
            return cik
    return None


def metrics(items: dict[str, Any], market_value: float | None) -> dict[str, float | None]:
    def ratio(num: Any, den: Any) -> float | None:
        if num is None or den is None or den <= 0:
            return None
        return float(num) / float(den)

    ni, cfo = items.get("net_income"), items.get("operating_cash_flow")
    assets, equity = items.get("assets"), items.get("equity")
    return {
        "roa": ratio(ni, assets),
        "cfoa": ratio(cfo, assets),
        "earnings_yield": ratio(ni, market_value),
        "book_to_market": ratio(equity, market_value)
        if equity is not None and equity > 0
        else None,
        "cash_flow_yield": ratio(cfo, market_value),
    }


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """(average rank - 1) / (n - 1), ascending; ties share their average rank; n = 1 -> 0.5."""
    n = len(values)
    if n == 1:
        return {s: 0.5 for s in values}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg = (i + j) / 2  # zero-based average rank
        for k in range(i, j + 1):
            out[ordered[k][0]] = avg / (n - 1)
        i = j + 1
    return out


def combine(
    per_symbol: dict[str, dict[str, float | None]], signal: dict[str, Any]
) -> dict[str, float]:
    quality = [str(m) for m in signal.get("quality", QUALITY)]
    value = [str(m) for m in signal.get("value", VALUE)]
    ranks: dict[str, dict[str, float]] = {}
    for m in quality + value:
        have = {s: v[m] for s, v in per_symbol.items() if v.get(m) is not None}
        ranks[m] = percentile_ranks(have) if have else {}  # type: ignore[arg-type]
    scores: dict[str, float] = {}
    for s in per_symbol:
        q = [ranks[m][s] for m in quality if s in ranks[m]]
        v = [ranks[m][s] for m in value if s in ranks[m]]
        if len(q) >= 1 and len(v) >= 2:
            scores[s] = 0.5 * sum(q) / len(q) + 0.5 * sum(v) / len(v)
    return scores


def in_ranges(sic: int | None, ranges: list[Any]) -> bool:
    if sic is None:
        return False
    flat = [int(x) for x in ranges]
    return any(lo <= sic <= hi for lo, hi in zip(flat[::2], flat[1::2], strict=False))


@dataclass
class QualityValue:
    """Callable used by ``cross_section.run``: candidates on day index ``t`` -> scores, stats."""

    contract: dict[str, Any]
    days: list[date]
    reports: dict[int, list[Report]]
    segments: dict[str, list[tuple[date, date | None, int | None]]]
    actual: dict[str, list[float | None]]  # the day's actual (unadjusted) close, by ``days``
    splits: dict[str, Splits]
    splits_known: set[str]

    def __post_init__(self) -> None:
        spec = self.contract["fundamentals"]
        self.tags = tag_lists(spec)
        self.max_age = int(spec["max_age_days"])
        self.exclude = list(self.contract["universe"].get("exclude_sic") or [])

    def market_value(self, symbol: str, t: int, items: dict[str, Any]) -> float | None:
        if symbol not in self.splits_known or items.get("shares") is None:
            return None
        series = self.actual.get(symbol)
        if series is None:
            return None
        price = next(
            (series[j] for j in range(t, max(t - STALE, 0) - 1, -1) if series[j] is not None),
            None,
        )
        if price is None:
            return None
        ratio = split_ratio(self.splits.get(symbol, []), items["filed"], self.days[t])
        return float(price * items["shares"] * ratio)

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.days[t]
        per: dict[str, dict[str, float | None]] = {}
        financial = 0
        for s in candidates:
            cik = cik_on(self.segments.get(s, []), day)
            items = (
                resolve(self.reports.get(cik, []), day, self.tags, self.max_age) if cik else None
            )
            if items is None:
                continue
            if in_ranges(items["sic"], self.exclude):
                financial += 1
                continue
            per[s] = metrics(items, self.market_value(s, t, items))
        scores = combine(per, self.contract["signal"])
        return scores, {
            "candidates": len(candidates),
            "financial": financial,
            "with_fundamentals": len(per),
            "scored": len(scores),
        }


def actual_prices(
    closes: dict[str, list[float | None]],
    days: list[date],
    adjusted: set[str],
    splits: dict[str, Splits],
) -> dict[str, list[float | None]]:
    """Undo Yahoo's split adjustment: close x splits effective after that day."""
    out: dict[str, list[float | None]] = {}
    for sym, series in closes.items():
        if sym not in adjusted:
            out[sym] = list(series)
            continue
        events = splits.get(sym, [])
        out[sym] = [
            v * split_ratio(events, d, None) if v is not None else None
            for d, v in zip(days, series, strict=True)
        ]
    return out


def load_market_inputs(
    tables: Any, days: list[date], aliases: dict[str, str]
) -> tuple[dict[str, list[float | None]], dict[str, Splits], set[str]]:
    """Actual daily closes, split events and the symbols whose split history is known.

    Yahoo series (``daily`` and Yahoo-sourced former members) are split-adjusted; their splits
    come from ``parquet/splits`` and count as known only when ``meta/splits_checked`` lists the
    symbol (no file = no splits only if it was checked). Tiingo-sourced former members are stored
    unadjusted with a ``split_factor`` column. Renamed tickers borrow the successor's prices and
    splits like the adj_close series do (dated splices: the successor's before the cut).
    """
    index = {d: i for i, d in enumerate(days)}
    closes: dict[str, list[float | None]] = {}
    splits: dict[str, Splits] = {}
    adjusted: set[str] = set()
    tiingo: set[str] = set()
    if tables.has("meta", "sp500_coverage"):
        tiingo = {
            s
            for s, src in tables.read("meta", "sp500_coverage", "symbol, source")
            if src == "tiingo"
        }
    for kind in ("daily_delisted", "daily"):  # Yahoo ("daily") wins, as in load_prices
        if not tables.keys(kind):
            continue
        pattern = str(tables.root / "parquet" / kind / "*.parquet").replace("'", "''")
        factor = "split_factor" if kind == "daily_delisted" else "1.0"
        con = tables._duckdb().connect()  # noqa: SLF001
        try:
            rows = con.execute(
                "SELECT regexp_extract(filename, '([^/]+)\\.parquet$', 1), date, close, "
                f"{factor} FROM read_parquet('{pattern}', filename=true) WHERE close > 0"
            ).fetchall()
        finally:
            con.close()
        fresh: dict[str, list[float | None]] = {}
        events: dict[str, Splits] = {}
        for sym, d, c, sf in rows:
            if sf and sf != 1.0:
                events.setdefault(sym, []).append((d, float(sf)))
            i = index.get(d)
            if i is not None:
                fresh.setdefault(sym, [None] * len(days))[i] = float(c)
        for sym, series in fresh.items():
            closes[sym] = series
            if kind == "daily_delisted" and sym in tiingo:
                adjusted.discard(sym)
                splits[sym] = sorted(events.get(sym, []))
            else:
                adjusted.add(sym)
                splits.pop(sym, None)
    raw_known = set(closes) - adjusted
    checked: set[str] = set()
    if tables.has("meta", "splits_checked"):
        checked = {s for (s,) in tables.read("meta", "splits_checked", "symbol")}
    if tables.keys("splits"):
        pattern = str(tables.root / "parquet" / "splits" / "*.parquet").replace("'", "''")
        con = tables._duckdb().connect()  # noqa: SLF001
        try:
            rows = con.execute(
                "SELECT regexp_extract(filename, '([^/]+)\\.parquet$', 1), date, numerator, "
                f"denominator FROM read_parquet('{pattern}', filename=true) "
                "WHERE numerator > 0 AND denominator > 0"
            ).fetchall()
        finally:
            con.close()
        for sym, d, n, m in rows:
            if sym in adjusted:
                splits.setdefault(sym, []).append((d, n / m))
        for sym in adjusted:
            splits[sym] = sorted(splits.get(sym, []))
    known = raw_known | (adjusted & checked)
    actual = actual_prices(closes, days, adjusted, splits)
    apply_aliases_market(actual, splits, known, days, aliases)
    return actual, splits, known


def apply_aliases_market(
    actual: dict[str, list[float | None]],
    splits: dict[str, Splits],
    known: set[str],
    days: list[date],
    aliases: dict[str, str],
) -> None:
    for key, new in sorted(aliases.items()):
        if new not in actual:
            continue
        old, _, cut = key.partition("@")
        if not cut:
            if old not in actual:
                actual[old] = actual[new]
                splits[old] = list(splits.get(new, []))
                if new in known:
                    known.add(old)
            elif old not in known and new in known:
                # same company under its new ticker (the old one is gone from Yahoo, e.g. GPS
                # -> GAP): its split history is the new ticker's
                splits[old] = list(splits.get(new, []))
                known.add(old)
            continue
        until = date.fromisoformat(cut)
        own = actual.get(old) or [None] * len(days)
        actual[old] = [
            b if d < until else o for d, b, o in zip(days, actual[new], own, strict=True)
        ]
        splits[old] = sorted(
            [e for e in splits.get(new, []) if e[0] < until]
            + [e for e in splits.get(old, []) if e[0] >= until]
        )
        if not (new in known and old in known):
            known.discard(old)


def load_segments(tables: Any) -> dict[str, list[tuple[date, date | None, int | None]]]:
    out: dict[str, list[tuple[date, date | None, int | None]]] = {}
    if not tables.has("meta", "ticker_cik"):
        return out
    for sym, a, b, cik in tables.read("meta", "ticker_cik", "symbol, start_date, end_date, cik"):
        out.setdefault(sym, []).append((a, b, int(cik) if cik is not None else None))
    for segs in out.values():
        segs.sort(key=lambda s: s[0])
    return out


__all__ = [
    "QualityValue",
    "Report",
    "combine",
    "load_market_inputs",
    "load_reports",
    "load_segments",
    "metrics",
    "parse_rows",
    "percentile_ranks",
    "resolve",
    "split_ratio",
]
