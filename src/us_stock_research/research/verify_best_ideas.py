"""Independent check of study sp500_best_ideas_13f (``usr-verify-best-ideas``; verification only).

The signal is recomputed for every rebalance day in one SQL pipeline (DuckDB window functions,
no code shared with ``best_ideas.py``): CUSIP -> symbol by the latest fails-to-deliver record in
the previous 365 days, each manager's newest 13F-HR by (period, filing date, accession) via
``row_number``, the 10-150 member filter, weights, consensus, the best idea by rank, and the
score. Then:

1. the per-stock scores are compared with ``best_ideas.BestIdeas`` month by month (relative
   1e-9), and the candidate counts, mapping coverage and manager counts with its stats;
2. the SQL scores are replayed through ``cross_section.run`` (the verified engine) and each
   month's picks and returns compared with the study's result file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from us_stock_research.research import cross_section as xs

SQL = """
WITH map AS (
  SELECT cusip, symbol FROM (
    SELECT cusip, symbol, seen,
           row_number() OVER (PARTITION BY cusip ORDER BY seen DESC, symbol DESC) AS rk
    FROM ftd WHERE seen <= $day
  ) WHERE rk = 1 AND seen >= $day - INTERVAL 365 DAY
), latest AS (
  SELECT cik, accession FROM (
    SELECT cik, accession,
           row_number() OVER (PARTITION BY cik
                              ORDER BY period DESC, filing_date DESC, accession DESC) AS rk
    FROM (SELECT DISTINCT cik, accession, period, filing_date FROM h
          WHERE filing_date <= $day AND period >= $day - INTERVAL 200 DAY)
  ) WHERE rk = 1
), pos AS (
  SELECT h.cik, map.symbol, sum(h.value) AS v
  FROM h JOIN latest USING (cik, accession) JOIN map USING (cusip)
  WHERE map.symbol IN (SELECT symbol FROM cand)
  GROUP BY h.cik, map.symbol
), mgr AS (
  SELECT cik, sum(v) AS tot FROM pos GROUP BY cik HAVING count(*) BETWEEN 10 AND 150
), w AS (
  SELECT pos.cik, pos.symbol, pos.v / mgr.tot AS w FROM pos JOIN mgr USING (cik)
), cons AS (
  SELECT symbol, sum(w) / (SELECT count(*) FROM mgr) AS c FROM w GROUP BY symbol
), ranked AS (
  SELECT w.cik, w.symbol, w.w - cons.c AS tilt,
         row_number() OVER (PARTITION BY w.cik
                            ORDER BY w.w - cons.c DESC, w.w DESC, w.symbol ASC) AS rk
  FROM w JOIN cons USING (symbol)
)
SELECT symbol, count(*) + 0.001 * sum(tilt) AS score, (SELECT count(*) FROM mgr) AS managers
FROM ranked WHERE rk = 1 GROUP BY symbol
"""


class SqlScorer:
    def __init__(self, tables: Any, days: list[date]) -> None:
        import duckdb

        self.days = days
        self.con = duckdb.connect()
        root = tables.root / "parquet"
        h = str(root / "sec13f" / "*.parquet").replace("'", "''")
        f = str(root / "meta" / "ftd_cusip.parquet").replace("'", "''")
        self.con.execute(
            f"CREATE TABLE h AS SELECT * FROM read_parquet('{h}') "
            "WHERE form = '13F-HR' AND value > 0"
        )
        self.con.execute(f"CREATE TABLE ftd AS SELECT * FROM read_parquet('{f}')")
        self.log: dict[str, dict[str, Any]] = {}

    def scores(self, candidates: list[str], day: date) -> tuple[dict[str, float], int, int]:
        self.con.execute("CREATE OR REPLACE TEMP TABLE cand (symbol VARCHAR)")
        self.con.executemany("INSERT INTO cand VALUES (?)", [[s] for s in candidates])
        rows = self.con.execute(SQL, {"day": day}).fetchall()
        managers = int(rows[0][2]) if rows else 0
        mapped = self.con.execute(
            """
            SELECT count(DISTINCT symbol) FROM (
              SELECT cusip, symbol, seen,
                     row_number() OVER (PARTITION BY cusip ORDER BY seen DESC, symbol DESC) rk
              FROM ftd WHERE seen <= $day)
            WHERE rk = 1 AND seen >= $day - INTERVAL 365 DAY
              AND symbol IN (SELECT symbol FROM cand)
            """,
            {"day": day},
        ).fetchone()[0]
        return {str(s): float(v) for s, v, _ in rows}, managers, int(mapped)

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.days[t]
        got, managers, mapped = self.scores(candidates, day)
        self.log[day.isoformat()] = {
            "candidates": candidates,
            "scores": got,
            "managers": managers,
            "mapped": mapped,
        }
        return {s: got.get(s, 0.0) for s in candidates}, {"candidates": len(candidates)}


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-12, 1e-9 * max(abs(a), abs(b)))


def compare_scores(sql: SqlScorer, engine: Any, days: list[date]) -> list[str]:
    """SQL scores vs ``BestIdeas`` on every logged rebalance day."""
    index = {d.isoformat(): i for i, d in enumerate(days)}
    issues: list[str] = []
    for day, rec in sql.log.items():
        scores, stats = engine(rec["candidates"], index[day])
        nonzero = {s: v for s, v in scores.items() if v}
        if set(nonzero) != set(rec["scores"]):
            diff = sorted(set(nonzero) ^ set(rec["scores"]))[:5]
            issues.append(f"{day}: scored stocks differ {diff}")
        for s, v in rec["scores"].items():
            if not close(v, scores.get(s, 0.0)):
                issues.append(f"{day} {s}: sql {v} engine {scores.get(s)}")
        if stats["managers"] != rec["managers"] or stats["mapped"] != rec["mapped"]:
            issues.append(f"{day}: stats {stats} vs sql {rec['managers']}/{rec['mapped']}")
    return issues


def main(argv: list[str] | None = None) -> int:
    from us_stock_research.config import load_settings
    from us_stock_research.research.best_ideas import BestIdeas
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
    sql = SqlScorer(tables, prices.days)
    replay = xs.run(
        contract,
        prices,
        [tuple(r) for r in history],
        excluded=quarantined,
        blocked=xs.load_blocked(tables),
        scorer=sql,
    )["months"]
    issues = compare_scores(sql, BestIdeas(contract, prices.days, tables), prices.days)
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
        "scored_stock_months": sum(len(r["scores"]) for r in sql.log.values()),
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
