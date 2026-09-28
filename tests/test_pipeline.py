from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from us_stock_research.bars import DailyBar, merge_bars, read_bars, symbol_path, write_bars
from us_stock_research.collectors.yahoo_daily import parse_chart
from us_stock_research.quality.ohlcv import audit_bars
from us_stock_research.research.backtest import run_backtest
from us_stock_research.research.contracts import StudyContract, load_contract
from us_stock_research.research.promotion import assess
from us_stock_research.research.snapshots import create_snapshot, load_snapshot
from us_stock_research.risk import load_risk

ROOT = Path(__file__).resolve().parents[1]


def contract(**overrides: Any) -> StudyContract:
    raw = yaml.safe_load((ROOT / "research/etf-trend-baseline/study.yml").read_text())
    raw["data"]["universe"] = ["SPY", "QQQ", "IEF", "SHY"]
    raw["data"]["start"] = "2010-01-01"
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            raw[section][field] = value
        else:
            raw[section] = value
    return StudyContract.model_validate(raw)


def test_repo_contracts_and_risk_config_are_valid() -> None:
    for path in (ROOT / "research").glob("*/study.yml"):
        load_contract(path)
    risk = load_risk(ROOT / "configs/risk/default.yml")
    assert risk.trading_enabled is False
    assert risk.max_worst_12m_loss <= 0.25


def test_contract_rejects_benchmark_outside_universe() -> None:
    with pytest.raises(ValueError):
        contract(benchmark={"symbol": "DIA"})


def test_frozen_contract_requires_snapshot() -> None:
    with pytest.raises(ValueError):
        contract(status="frozen")


def test_parse_chart_skips_null_rows() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": -14400},
                    "timestamp": [1704205800, 1704292200],
                    "indicators": {
                        "quote": [
                            {
                                "open": [1.0, None],
                                "high": [2.0, 2.0],
                                "low": [0.5, 0.5],
                                "close": [1.5, 1.5],
                                "volume": [10, 10],
                            }
                        ],
                        "adjclose": [{"adjclose": [1.4, 1.4]}],
                    },
                }
            ],
            "error": None,
        }
    }
    bars = parse_chart(payload)
    assert len(bars) == 1 and bars[0].day == date(2024, 1, 2)


def test_quality_gate_flags_bad_rows() -> None:
    good = DailyBar(date(2024, 1, 2), 10, 11, 9, 10, 10, 1)
    bad = DailyBar(date(2024, 1, 3), 10, 9, 9.5, 10, 20, 1)  # high<low and +100% jump
    report = audit_bars("X", [good, bad])
    assert not report.passed and len(report.errors) >= 2
    assert audit_bars("X", [good]).passed


def test_merge_is_idempotent(tmp_path: Path) -> None:
    b = DailyBar(date(2024, 1, 2), 10, 11, 9, 10, 10, 1)
    path = tmp_path / "X.csv"
    write_bars(path, merge_bars([b], [b]))
    assert read_bars(path) == [b]


def test_snapshot_is_content_addressed_and_verified(data_dir: Path, tmp_path: Path) -> None:
    snaps = tmp_path / "snaps"
    sid = create_snapshot(data_dir, ["SPY", "IEF"], snaps)
    assert create_snapshot(data_dir, ["IEF", "SPY"], snaps) == sid
    assert set(load_snapshot(sid, snaps)) == {"SPY", "IEF"}
    digest = sid.rsplit(":", 1)[1]
    (snaps / digest / "daily" / "SPY.csv").write_text("tampered")
    with pytest.raises(ValueError):
        load_snapshot(sid, snaps)


@pytest.mark.parametrize("strategy", ["trend_sma_v1", "dual_momentum_v1", "buy_and_hold_v1"])
def test_backtest_end_to_end(data_dir: Path, tmp_path: Path, strategy: str) -> None:
    symbols = ["SPY", "QQQ", "IEF", "SHY"]
    sid = create_snapshot(data_dir, symbols, tmp_path / "snaps")
    c = contract(strategy=strategy, data__snapshot_id=sid)
    result = run_backtest(c, load_snapshot(sid, tmp_path / "snaps"))
    m = result["strategy_metrics"]
    assert result["trading_enabled"] is False
    assert result["oos_months"] > 60
    assert -1 < m["max_drawdown"] <= 0
    assert result["walk_forward"]
    assert "interval" in result["inference"]
    verdict = assess(c, result, load_risk(ROOT / "configs/risk/default.yml"))
    assert verdict["eligible_for_human_promotion"] is False  # draft + not approved
    assert verdict["checks"]["contract_frozen"] is False


def test_buy_and_hold_single_asset_matches_benchmark(data_dir: Path, tmp_path: Path) -> None:
    sid = create_snapshot(data_dir, ["SPY", "SHY"], tmp_path / "snaps")
    c = contract(
        strategy="buy_and_hold_v1",
        data__snapshot_id=sid,
        data__universe=["SPY", "SHY"],
        parameters__transaction_cost_bps=0,
    )
    result = run_backtest(c, load_snapshot(sid, tmp_path / "snaps"))
    assert result["strategy_metrics"]["total_return"] == pytest.approx(
        result["benchmark"]["metrics"]["total_return"], rel=1e-9
    )


def test_symbol_path_uppercases(tmp_path: Path) -> None:
    assert symbol_path(tmp_path, "spy").name == "SPY.csv"
