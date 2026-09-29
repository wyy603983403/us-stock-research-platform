"""Read-only promotion assessment. It never changes a contract and never promotes on its own."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from us_stock_research.quality.ohlcv import load_exceptions
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.risk import RiskConfig, load_risk


def assess(
    c: StudyContract,
    artifact: dict[str, Any],
    risk: RiskConfig,
    quarantine: dict[str, str] | None = None,
) -> dict[str, Any]:
    m = artifact["strategy_metrics"]
    inf = artifact.get("inference", {})
    worst = m.get("worst_rolling_12m_return")
    checks = {
        "artifact_matches_contract": artifact.get("study") == c.name
        and artifact.get("snapshot_id") == c.data.snapshot_id,
        "contract_frozen": c.status == "frozen",
        "enough_oos_months": artifact.get("oos_months", 0) >= risk.min_oos_months,
        "worst_12m_loss_within_cap": worst is not None and worst >= -risk.max_worst_12m_loss,
        "single_asset_weight_within_cap": 1 / c.parameters.top_k <= risk.max_single_asset_weight
        if c.strategy == "dual_momentum_v1"
        else 1 / max(len(c.risk_universe()), 1) <= risk.max_single_asset_weight,
        "excess_interval_lower_positive": bool(inf.get("interval")) and inf["interval"][0] > 0,
        "survivorship_safe": c.data.universe_kind != "stocks_current_constituents",
        "deflated_sharpe_ok": artifact.get("multiple_testing", {}).get("deflated_sharpe", 0.0)
        >= artifact.get("multiple_testing", {}).get("threshold", 0.95),
        "no_quarantined_symbols": not {x.upper() for x in c.data.universe}
        & {x.upper() for x in (quarantine or {})},
        "human_approved": c.human_review.approved,
        "trading_disabled": artifact.get("trading_enabled") is False,
    }
    return {
        "study": c.name,
        "eligible_for_human_promotion": all(checks.values()),
        "checks": checks,
        "note": "Assessment only. Promotion is a manual contract edit after human review.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--risk", type=Path, default=Path("configs/risk/default.yml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exceptions", type=Path, default=Path("configs/quality_exceptions.yml"))
    args = parser.parse_args(argv)
    _, quarantine = load_exceptions(args.exceptions)
    result = assess(
        load_contract(args.contract),
        json.loads(args.artifact.read_text()),
        load_risk(args.risk),
        quarantine,
    )
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
