"""Independent check of study sp500_earnings_drift (``usr-verify-earnings``; verification only).

Event selection is redone in one SQL query (DuckDB, no code shared with ``earnings.py``): item
lists split with ``string_split``, the company by joining ``meta/ticker_cik`` intervals, the
trading days around each filing from a calendar table, the most recent event whose reaction
window has closed within 92 days by ``row_number``. Window prices are looked up by a separate
backward scan over the price arrays. Then:

1. the per-stock EARs are compared with ``earnings.EarningsDrift`` on every rebalance day
   (relative 1e-9; unranked stocks must match exactly), together with the coverage stats;
2. the SQL scores are replayed through ``cross_section.run`` (the verified engine) and each
   month's picks and returns compared with the study's result file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from us_stock_research.research import cross_section as xs

SQL = """
WITH pairs AS (SELECT symbol FROM cand),
firm AS (
  SELECT p.symbol, arg_min(s.cik, s.start_date) AS cik
  FROM pairs p JOIN seg s ON s.symbol = p.symbol
   AND s.start_date <= $day AND (s.end_date IS NULL OR $day < s.end_date)
  GROUP BY p.symbol
),
win AS (
  SELECT e.cik, e.d,
         (SELECT max(i) FROM cal WHERE cal.day < e.d) AS i_prev,
         (SELECT min(i) FROM cal WHERE cal.day > e.d) AS i_next
  FROM ev e WHERE e.d <= $day AND e.d >= $day - INTERVAL 92 DAY
),
pick AS (
  SELECT f.symbol, f.cik, w.d, w.i_prev, w.i_next,
         row_number() OVER (PARTITION BY f.symbol ORDER BY w.d DESC) AS rk
  FROM firm f JOIN win w ON w.cik = f.cik
  WHERE w.i_prev IS NOT NULL AND w.i_next IS NOT NULL AND w.i_next <= $t
)
SELECT p.symbol, f.cik, k.d, k.i_prev, k.i_next
FROM pairs p LEFT JOIN firm f USING (symbol) LEFT JOIN (SELECT * FROM pick WHERE rk = 1) k
  ON k.symbol = p.symbol
"""


def back(series: list[float | None] | None, i: int) -> float | None:
    if series is None:
        return None
    j = i
    while j >= 0 and j >= i - 5:
        if series[j] is not None:
            return series[j]
        j -= 1
    return None


class SqlScorer:
    def __init__(self, tables: Any, prices: Any) -> None:
        import duckdb

        self.prices = prices
        self.con = duckdb.connect()
        root = tables.root / "parquet"
        k8 = str(root / "sec_8k" / "*.parquet").replace("'", "''")
        tc = str(root / "meta" / "ticker_cik.parquet").replace("'", "''")
        self.con.execute(
            "CREATE TABLE ev AS SELECT DISTINCT cik, filing_date AS d "
            f"FROM read_parquet('{k8}') WHERE form = '8-K' "
            "AND list_contains(list_transform(string_split(items, ','), x -> trim(x)), '2.02')"
        )
        self.con.execute(
            f"CREATE TABLE seg AS SELECT * FROM read_parquet('{tc}') WHERE cik IS NOT NULL"
        )
        self.con.execute("CREATE TABLE cal (i INTEGER, day DATE)")
        self.con.executemany("INSERT INTO cal VALUES (?, ?)", list(enumerate(prices.days)))
        self.log: dict[str, dict[str, Any]] = {}

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.prices.days[t]
        self.con.execute("CREATE OR REPLACE TEMP TABLE cand (symbol VARCHAR)")
        self.con.executemany("INSERT INTO cand VALUES (?)", [[s] for s in candidates])
        rows = self.con.execute(SQL, {"day": day, "t": t}).fetchall()
        scores: dict[str, float] = {}
        stats = {"candidates": len(candidates), "with_event": 0, "no_cik": 0, "invalid": 0}
        spy = self.prices.series.get("SPY")
        for sym, cik, d, i0, i1 in rows:
            if cik is None:
                stats["no_cik"] += 1
            if d is None:
                scores[sym] = float("-inf")
                continue
            s = self.prices.series.get(sym)
            a, b, ma, mb = back(s, i0), back(s, i1), back(spy, i0), back(spy, i1)
            if None in (a, b, ma, mb):
                stats["invalid"] += 1
                scores[sym] = float("-inf")
                continue
            scores[sym] = (b / a - 1) - (mb / ma - 1)  # type: ignore[operator]
            stats["with_event"] += 1
        self.log[day.isoformat()] = {"candidates": candidates, "scores": scores, "stats": stats}
        return scores, stats


def close(a: float, b: float) -> bool:
    if a == float("-inf") or b == float("-inf"):
        return a == b
    return abs(a - b) <= max(1e-12, 1e-9 * max(abs(a), abs(b)))


def compare_scores(sql: SqlScorer, engine: Any) -> list[str]:
    index = {d.isoformat(): i for i, d in enumerate(sql.prices.days)}
    issues: list[str] = []
    for day, rec in sql.log.items():
        scores, stats = engine(rec["candidates"], index[day])
        for s in rec["candidates"]:
            if not close(rec["scores"][s], scores[s]):
                issues.append(f"{day} {s}: sql {rec['scores'][s]} engine {scores[s]}")
        if stats != rec["stats"]:
            issues.append(f"{day}: stats {stats} vs sql {rec['stats']}")
    return issues


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research import earnings as ea
    from us_stock_research.research.fundamentals import load_segments
    from us_stock_research.tables import TableStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    parser.add_argument("--aliases", type=Path, default=Path("configs/ticker_aliases.yml"))
    args = parser.parse_args(argv)
    contract = xs.load_xs_contract(args.contract)
    tables = TableStore.from_settings(load_settings())
    uni = contract["universe"]
    history = list(tables.read("meta", "sp500_history", "symbol, start_date, end_date"))
    prices = xs.load_prices(
        tables, uni["start"] - timedelta(days=500), uni["end"] + timedelta(days=45)
    )
    xs.apply_aliases(prices, xs.load_aliases(args.aliases))
    quarantined = set(xs.load_exceptions(args.exceptions)[1])
    sql = SqlScorer(tables, prices)
    replay = xs.run(
        contract,
        prices,
        [tuple(r) for r in history],
        excluded=quarantined,
        blocked=xs.load_blocked(tables),
        scorer=sql,
    )["months"]
    engine = ea.EarningsDrift(contract, prices, ea.load_events(tables), load_segments(tables))
    issues = compare_scores(sql, engine)
    stored = json.loads(args.result.read_text())["months"]
    if len(stored) != len(replay):
        issues.append(f"months: result {len(stored)} vs replay {len(replay)}")
    for a, b in zip(stored, replay, strict=False):
        if a["date"] != b["date"] or a["top"] != b["top"] or a["picks"] != b["picks"]:
            issues.append(f"{a['date']}: picks differ {a['top'][:3]} vs {b['top'][:3]}")
        for key in ("strategy", "equal_weight"):
            if not close(a[key], b[key]):
                issues.append(f"{a['date']} {key}: result {a[key]} vs replay {b[key]}")
    report = {
        "study": contract["name"],
        "months": len(replay),
        "score_days_checked": len(sql.log),
        "ranked_stock_months": sum(r["stats"]["with_event"] for r in sql.log.values()),
        "issues": issues[:50],
        "issue_count": len(issues),
        "verdict": "一致" if not issues else "不一致",
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
