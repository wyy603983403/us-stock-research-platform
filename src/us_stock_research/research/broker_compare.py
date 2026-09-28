"""Run one study contract under several broker cost scenarios and portfolio sizes.

Read-only sensitivity analysis: the contract, snapshot and parameters stay fixed; only the
commission / dividend-withholding model and notional capital change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from us_stock_research.research.backtest import GROSS, BrokerCosts, run_backtest
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.snapshots import Bundle, load_snapshot


def load_brokers(path: Path) -> tuple[list[BrokerCosts], dict[str, str]]:
    raw: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text())["brokers"]
    brokers: list[BrokerCosts] = []
    labels: dict[str, str] = {}
    for name, spec in raw.items():
        spec = dict(spec)
        labels[name] = str(spec.pop("label", name))
        brokers.append(BrokerCosts(name=name, **spec))
    return brokers, labels


def compare(
    c: StudyContract, bundle: Bundle, brokers: list[BrokerCosts], sizes: list[float]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for size in sizes:
        for broker in [GROSS, *brokers]:
            r = run_backtest(c, bundle, broker, size)
            m, b = r["strategy_metrics"], r["benchmark"]["metrics"]
            rows.append(
                {
                    "broker": broker.name,
                    "portfolio_usd": size,
                    "cagr": m["cagr"],
                    "worst_rolling_12m_return": m["worst_rolling_12m_return"],
                    "max_drawdown": m["max_drawdown"],
                    "commission_usd": r["costs"]["commission_usd"],
                    "orders": r["costs"]["orders"],
                    "benchmark_cagr": b["cagr"],
                    "benchmark_worst_rolling_12m_return": b["worst_rolling_12m_return"],
                    "start": m["start"],
                    "end": m["end"],
                    "dividend_data": r["dividend_data"],
                }
            )
    return rows


def to_markdown(rows: list[dict[str, Any]], labels: dict[str, str]) -> str:
    lines = [
        "| 本金 (USD) | 券商 | 策略年化 | 最差 12 个月 | 最大回撤 | 累计佣金 (USD) | 订单数 "
        "| SPY 年化 | SPY 最差 12 个月 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['portfolio_usd']:,.0f} | {labels.get(r['broker'], '未扣佣金和税（毛收益）')} "
            f"| {r['cagr']:.2%} | {r['worst_rolling_12m_return']:.1%} | {r['max_drawdown']:.1%} "
            f"| {r['commission_usd']:,.0f} | {r['orders']} | {r['benchmark_cagr']:.2%} "
            f"| {r['benchmark_worst_rolling_12m_return']:.1%} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--brokers", type=Path, default=Path("configs/brokers.yml"))
    parser.add_argument("--snapshots-dir", type=Path, default=Path("artifacts/snapshots"))
    parser.add_argument("--portfolio-usd", type=float, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--output", type=Path, required=True, help="JSON path; .md written too")
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if not contract.data.snapshot_id:
        parser.error("contract has no data.snapshot_id; create and record a snapshot first")
    brokers, labels = load_brokers(args.brokers)
    rows = compare(
        contract,
        load_snapshot(contract.data.snapshot_id, args.snapshots_dir),
        brokers,
        args.portfolio_usd,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"study": contract.name, "rows": rows}, indent=2) + "\n")
    table = to_markdown(rows, labels)
    args.output.with_suffix(".md").write_text(table + "\n")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
