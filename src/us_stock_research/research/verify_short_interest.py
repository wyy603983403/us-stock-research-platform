"""Independent check of study sp500_short_interest (``usr-verify-short``; verification only).

Days to cover are recomputed in SQL (DuckDB, no code shared with ``short_interest.py``): the
settlement date by ``max`` over the stored files' ``settlement`` column, symbols normalised with
``regexp_replace``, duplicates resolved by ``row_number``. Then:

1. scores are compared with ``short_interest.ShortInterest`` on every rebalance day (relative
   1e-9; unranked stocks must match exactly), with the coverage stats;
2. the SQL scores are replayed through ``cross_section.run`` and each month's picks and returns
   compared with the study's result file.
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
WITH s AS (SELECT max(settlement) AS d FROM si WHERE settlement + INTERVAL 14 DAY <= $day),
r AS (
  SELECT regexp_replace(trim(symbol), '[./]', '-', 'g') AS sym, short_qty, adv,
         row_number() OVER (PARTITION BY regexp_replace(trim(symbol), '[./]', '-', 'g')
                            ORDER BY short_qty DESC NULLS LAST, symbol) AS rk
  FROM si WHERE settlement = (SELECT d FROM s)
)
SELECT c.symbol, r.short_qty / r.adv AS dtc, (SELECT d FROM s) AS settle
FROM cand c LEFT JOIN r ON r.sym = c.symbol AND r.rk = 1 AND r.adv > 0
  AND r.short_qty IS NOT NULL
"""


class SqlScorer:
    def __init__(self, tables: Any, days: list[Any]) -> None:
        import duckdb

        self.days = days
        self.con = duckdb.connect()
        pattern = str(tables.root / "parquet" / "short_interest" / "*.parquet").replace("'", "''")
        self.con.execute(f"CREATE TABLE si AS SELECT * FROM read_parquet('{pattern}')")
        self.log: dict[str, dict[str, Any]] = {}

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        day = self.days[t]
        self.con.execute("CREATE OR REPLACE TEMP TABLE cand (symbol VARCHAR)")
        self.con.executemany("INSERT INTO cand VALUES (?)", [[s] for s in candidates])
        rows = self.con.execute(SQL, {"day": day}).fetchall()
        scores = {str(s): (-float(v) if v is not None else float("-inf")) for s, v, _ in rows}
        settle = rows[0][2] if rows else None
        stats = {
            "candidates": len(candidates),
            "with_value": sum(1 for _, v, _ in rows if v is not None),
            "lag_days": (day - settle).days if settle else -1,
        }
        self.log[day.isoformat()] = {"candidates": candidates, "scores": scores, "stats": stats}
        return scores, stats


def close(a: float, b: float) -> bool:
    if a == float("-inf") or b == float("-inf"):
        return a == b
    return abs(a - b) <= max(1e-12, 1e-9 * max(abs(a), abs(b)))


def compare_scores(sql: SqlScorer, engine: Any) -> list[str]:
    index = {d.isoformat(): i for i, d in enumerate(sql.days)}
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
    from us_stock_research.research.short_interest import ShortInterest
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
    issues = compare_scores(sql, ShortInterest(contract, prices.days, tables))
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
        "ranked_stock_months": sum(r["stats"]["with_value"] for r in sql.log.values()),
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
