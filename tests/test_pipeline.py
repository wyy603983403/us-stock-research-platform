from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from us_stock_research.bars import (
    DailyBar,
    dividends_path,
    merge_bars,
    read_bars,
    symbol_path,
    write_bars,
    write_dividends,
)
from us_stock_research.collectors.yahoo_daily import parse_chart, parse_dividends
from us_stock_research.quality.ohlcv import audit_bars
from us_stock_research.research.backtest import GROSS, BrokerCosts, run_backtest
from us_stock_research.research.broker_compare import compare, load_brokers
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
    assert set(load_snapshot(sid, snaps).bars) == {"SPY", "IEF"}
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


def test_parse_dividends() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": -14400},
                    "events": {"dividends": {"1": {"amount": 1.5, "date": 1704205800}}},
                }
            ]
        }
    }
    assert parse_dividends(payload) == {date(2024, 1, 2): 1.5}


def test_order_fee_models() -> None:
    ibkr = BrokerCosts("ibkr", per_share_usd=0.005, min_per_order_usd=1.0, max_pct_of_value=0.01)
    assert ibkr.order_fee(500, 50) == pytest.approx(1.0)  # 10 shares -> minimum
    assert ibkr.order_fee(100_000, 50) == pytest.approx(10.0)  # 2000 shares * 0.005
    assert ibkr.order_fee(50, 0.5) == pytest.approx(0.5)  # capped at 1% of value
    binance = BrokerCosts("binance", commission_bps=10, min_per_order_usd=0.35)
    assert binance.order_fee(100, 10) == pytest.approx(0.35)
    assert binance.order_fee(10_000, 10) == pytest.approx(10.0)
    assert GROSS.order_fee(10_000, 10) == 0.0


def test_dividend_tax_and_commission_reduce_returns(data_dir: Path, tmp_path: Path) -> None:
    symbols = ["SPY", "QQQ", "IEF", "SHY"]
    for sym in symbols:
        rows = read_bars(symbol_path(data_dir, sym))
        quarterly = {
            b.day: b.close * 0.004 for b in rows if b.day.month % 3 == 0 and b.day.day <= 3
        }
        write_dividends(dividends_path(data_dir, sym), quarterly)
    sid = create_snapshot(data_dir, symbols, tmp_path / "snaps")
    bundle = load_snapshot(sid, tmp_path / "snaps")
    assert bundle.dividends["SPY"]
    c = contract(data__snapshot_id=sid)
    gross = run_backtest(c, bundle)
    taxed = run_backtest(c, bundle, BrokerCosts("t", dividend_withholding_rate=0.3))
    feed = run_backtest(c, bundle, BrokerCosts("f", commission_bps=10, min_per_order_usd=0.35))
    assert gross["dividend_data"] is True
    g, t, f = (r["strategy_metrics"]["total_return"] for r in (gross, taxed, feed))
    assert t < g and f < g
    assert (
        taxed["benchmark"]["metrics"]["total_return"]
        < gross["benchmark"]["metrics"]["total_return"]
    )
    assert feed["costs"]["commission_usd"] > 0 and feed["costs"]["orders"] > 0


def test_repo_broker_config_compares(data_dir: Path, tmp_path: Path) -> None:
    brokers, labels = load_brokers(ROOT / "configs/brokers.yml")
    assert {"schwab_international", "ibkr_fixed", "binance_us_stocks"} <= set(labels)
    sid = create_snapshot(data_dir, ["SPY", "QQQ", "IEF", "SHY"], tmp_path / "snaps")
    rows = compare(
        contract(data__snapshot_id=sid), load_snapshot(sid, tmp_path / "snaps"), brokers, [10_000]
    )
    by = {r["broker"]: r for r in rows}
    assert by["schwab_international"]["commission_usd"] == 0
    assert by["binance_us_stocks"]["commission_usd"] > by["ibkr_fixed"]["commission_usd"] > 0
