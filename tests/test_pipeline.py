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
    # The repo contract may be frozen with a real snapshot; tests start from a blank draft.
    raw["status"] = "draft"
    raw["data"]["snapshot_id"] = raw["data"]["quality_report"] = None
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


@pytest.mark.parametrize(
    "strategy", ["trend_sma_v1", "dual_momentum_v1", "buy_and_hold_v1", "vol_target_v1"]
)
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


def test_universe_download_is_resumable_and_reports_failures(tmp_path: Path) -> None:
    from conftest import synthetic_bars

    from us_stock_research.collectors.universe import download, parse_sp500_csv, to_yahoo
    from us_stock_research.storage import CsvStore

    assert to_yahoo("brk.b") == "BRK-B"
    assert parse_sp500_csv("Symbol,Name\nBRK.B,x\nAAPL,y\nAAPL,y\n") == ["AAPL", "BRK-B"]
    store = CsvStore(tmp_path)
    bars = synthetic_bars(date(2020, 1, 1), 30, 0.001, 0.0, 0.0)
    calls: list[str] = []

    def fetch(symbol: str, start: date, end: date):  # type: ignore[no-untyped-def]
        calls.append(symbol)
        if symbol == "BAD":
            raise ValueError("boom")
        return bars, {date(2020, 1, 15): 0.5}

    args = {"execute": True, "refresh": False, "pause": 0.0, "sleep": lambda _: None}
    first = download(["AAA", "BAD"], store, fetch, date(2020, 1, 1), date(2020, 3, 1), **args)
    assert first["downloaded"] == ["AAA"] and "BAD" in first["failed"]
    assert store.read_dividends("AAA") == {date(2020, 1, 15): 0.5}
    calls.clear()
    second = download(["AAA", "CCC"], store, fetch, date(2020, 1, 1), date(2020, 3, 1), **args)
    assert second["skipped_existing"] == ["AAA"] and calls == ["CCC"]


def test_macro_series_price_issues_are_warnings() -> None:
    from conftest import synthetic_bars

    bars = synthetic_bars(date(2020, 1, 1), 3, 0.0, 0.0, 0.0)
    bars[1] = DailyBar(bars[1].day, 10.0, 12.0, 9.0, 10.0, 5.0, 0)  # adj differs from raw
    assert audit_bars("SPY", bars).errors
    report = audit_bars("^VIX", bars)
    assert not report.errors and report.warnings


def test_nyse_calendar_known_dates() -> None:
    from us_stock_research.calendar import is_trading_day, trading_days

    closed = [(2021, 12, 24), (2022, 6, 20), (2020, 4, 10), (2025, 1, 9), (2012, 10, 29)]
    open_ = [(2021, 12, 31), (2022, 6, 17), (2026, 9, 28), (2009, 4, 9)]
    assert not any(is_trading_day(date(*d)) for d in closed)
    assert all(is_trading_day(date(*d)) for d in open_)
    assert len(trading_days(date(2023, 1, 1), date(2023, 12, 31))) == 250


def test_quality_gate_uses_trading_calendar() -> None:
    from conftest import synthetic_bars

    bars = synthetic_bars(date(2024, 1, 2), 60, 0.0, 0.0, 0.0)  # weekdays, includes holidays
    report = audit_bars("SPY", bars)
    assert any("non-trading days" in w for w in report.warnings)
    holes = [b for i, b in enumerate(bars) if i not in (10, 11, 12)]
    assert any(
        "missing" in w for w in audit_bars("SPY", holes).warnings + audit_bars("SPY", holes).errors
    )


def test_crosscheck_detects_disagreement() -> None:
    from conftest import synthetic_bars

    from us_stock_research.quality.crosscheck import compare, parse_close_csv

    bars = synthetic_bars(date(2024, 1, 2), 100, 0.001, 0.01, 0.0)
    good = {b.day: b.close for b in bars}
    assert compare(bars, good)["passed"]
    bad = dict(good)
    for i, b in enumerate(bars):
        if i % 10 == 0:
            bad[b.day] = b.close * 1.05
    assert not compare(bars, bad)["passed"]
    csv_text = "Date,Open,High,Low,Close,Volume\n2024-01-02,1,1,1,10.5,5\n"
    assert parse_close_csv(csv_text) == {date(2024, 1, 2): 10.5}
    with pytest.raises(ValueError):
        parse_close_csv("Get your apikey")


def test_deflated_sharpe_penalises_many_trials() -> None:
    from us_stock_research.research.trials import (
        deflated_sharpe,
        expected_max_sharpe,
        moment_stats,
    )

    returns = [0.01 if i % 3 else -0.004 for i in range(120)]
    stats = moment_stats(returns)
    one = deflated_sharpe(stats, 1, 0.0)
    many = deflated_sharpe(stats, 50, 0.01**2)
    assert one > 0.99 and many < one
    assert expected_max_sharpe(50, 0.0004) > expected_max_sharpe(5, 0.0004) > 0


def test_trial_registry_counts_distinct_parameter_sets(tmp_path: Path) -> None:
    from us_stock_research.research.trials import record_and_assess

    def artifact(sma: int, shift: float) -> dict[str, Any]:
        rets = [0.008 + shift * (-1) ** i * 0.01 for i in range(60)]
        return {
            "study": "s",
            "strategy": "trend_sma_v1",
            "parameters": {"sma_days": sma},
            "snapshot_id": "snap",
            "strategy_monthly_returns": rets,
        }

    path = tmp_path / "trials.jsonl"
    assert record_and_assess(path, artifact(100, 1.0), ["A"])["trials_registered"] == 1
    assert record_and_assess(path, artifact(100, 1.0), ["A"])["trials_registered"] == 1
    assert record_and_assess(path, artifact(200, 1.5), ["A"])["trials_registered"] == 2


def test_real_crash_is_a_warning_not_an_error() -> None:
    a = DailyBar(date(2024, 1, 2), 100, 101, 99, 100, 100, 1)
    b = DailyBar(date(2024, 1, 3), 50, 51, 49, 50, 50, 1)  # -50% but adj == raw
    report = audit_bars("AAPL", [a, b])
    assert not report.errors and any("large adjusted move" in w for w in report.warnings)


@pytest.mark.parametrize(
    "strategy", ["trend_sma_v1", "dual_momentum_v1", "buy_and_hold_v1", "vol_target_v1"]
)
def test_independent_engine_matches_production(
    data_dir: Path, tmp_path: Path, strategy: str
) -> None:
    pytest.importorskip("pandas")
    from us_stock_research.research.verify_engine import verify

    sid = create_snapshot(data_dir, ["SPY", "QQQ", "IEF", "SHY"], tmp_path / "snaps")
    c = contract(strategy=strategy, data__snapshot_id=sid)
    result = verify(c, load_snapshot(sid, tmp_path / "snaps"))
    assert result["passed"], result


def test_report_input_is_built_from_artifact(data_dir: Path, tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    from us_stock_research.research.report import returns_from_artifact

    sid = create_snapshot(data_dir, ["SPY", "QQQ", "IEF", "SHY"], tmp_path / "snaps")
    c = contract(data__snapshot_id=sid)
    result = run_backtest(c, load_snapshot(sid, tmp_path / "snaps"))
    strategy, benchmark = returns_from_artifact(result)
    assert len(strategy) == len(benchmark) == len(result["equity_curve"]["dates"]) - 1
    with pytest.raises(ValueError):
        returns_from_artifact({})


def test_tiingo_parse_and_adjusted_comparison() -> None:
    from conftest import synthetic_bars

    from us_stock_research.quality.crosscheck import compare, parse_tiingo

    bars = synthetic_bars(date(2024, 1, 2), 50, 0.001, 0.01, 0.0)
    rows = [{"date": f"{b.day.isoformat()}T00:00:00.000Z", "adjClose": b.adj_close} for b in bars]
    other = parse_tiingo(rows)
    assert other[bars[0].day] == bars[0].adj_close
    assert compare(bars, other, adjusted=True)["passed"]


def test_incremental_update_appends_and_detects_restatement(tmp_path: Path) -> None:
    from conftest import synthetic_bars

    from us_stock_research.collectors.update import plan, run_update
    from us_stock_research.storage import CsvStore

    full = synthetic_bars(date(2024, 1, 2), 80, 0.001, 0.01, 0.0)
    store = CsvStore(tmp_path)
    store.write_bars("AAA", full[:60])
    store.write_dividends("AAA", {})

    def fetch_same(symbol: str, start: date, end: date):  # type: ignore[no-untyped-def]
        return [b for b in full if b.day >= start], {}

    out = run_update(
        ["AAA"], store, fetch_same, date(2025, 1, 1), date(2000, 1, 1),
        execute=True, pause=0.0, sleep=lambda _: None,
    )  # fmt: skip
    assert out["appended_days"] == 20 and not out["refreshed"]
    assert len(store.read_bars("AAA")) == 80

    # A dividend restates history: every adjusted close before it is scaled by 0.99.
    restated = [
        DailyBar(b.day, b.open, b.high, b.low, b.close, b.adj_close * 0.99, b.volume) for b in full
    ]
    assert plan(full, restated[-5:])[0] == "refresh"
    assert plan(full, full[-5:])[0] == "append"
    assert plan(full[:10], full[-5:])[0] == "refresh"  # no overlap

    def fetch_restated(symbol: str, start: date, end: date):  # type: ignore[no-untyped-def]
        return [b for b in restated if b.day >= start], {}

    out = run_update(
        ["AAA"], store, fetch_restated, date(2025, 1, 1), date(2000, 1, 1),
        execute=True, pause=0.0, sleep=lambda _: None,
    )  # fmt: skip
    assert [r["symbol"] for r in out["refreshed"]] == ["AAA"]
    assert store.read_bars("AAA")[0].adj_close == pytest.approx(full[0].adj_close * 0.99)
    assert store.symbols() == ["AAA"]


def test_fred_sec_sp500_parsers_and_point_in_time() -> None:
    from us_stock_research.collectors.fred import parse_fredgraph
    from us_stock_research.collectors.sec_fundamentals import (
        parse_companyfacts,
        point_in_time,
        ticker_map,
    )
    from us_stock_research.collectors.universe import parse_sp500_meta

    days, values = parse_fredgraph(
        "observation_date,DGS10\n2024-01-02,3.95\n2024-01-03,.\n2024-01-04,4.0\n"
    )
    assert days == [date(2024, 1, 2), date(2024, 1, 4)] and values == [3.95, 4.0]
    with pytest.raises(ValueError):
        parse_fredgraph("<html>blocked</html>")

    payload = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2023-12-31",
                                "val": 100,
                                "filed": "2024-02-01",
                                "form": "10-K",
                            },
                            {
                                "end": "2023-12-31",
                                "val": 101,
                                "filed": "2024-06-01",
                                "form": "10-K/A",
                            },
                            {
                                "end": "2024-03-31",
                                "val": 120,
                                "filed": "2024-05-01",
                                "form": "10-Q",
                            },
                        ]
                    }
                }
            }
        }
    }
    rows = parse_companyfacts(payload)
    assert len(rows) == 3
    assert point_in_time(rows, "Assets", date(2024, 1, 1)) is None  # not yet filed
    assert point_in_time(rows, "Assets", date(2024, 3, 1))["value"] == 100
    assert point_in_time(rows, "Assets", date(2024, 5, 15))["value"] == 120
    assert point_in_time(rows, "Assets", date(2024, 12, 1))["value"] == 120  # newest period wins
    assert ticker_map({"0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple"}}) == {
        "AAPL": 320193
    }
    text = (
        "Symbol,Security,GICS Sector,GICS Sub-Industry,Date added,CIK\n"
        "BRK.B,Berkshire,Financials,Multi,1976,1067983\n"
    )
    cols = parse_sp500_meta(text)
    assert cols[0] == ["BRK-B"] and cols[2] == ["Financials"] and cols[5] == ["1067983"]


def test_catalog_rows_and_markdown(tmp_path: Path) -> None:
    from conftest import synthetic_bars

    from us_stock_research.catalog import build_rows, render_markdown
    from us_stock_research.storage import CsvStore

    store = CsvStore(tmp_path)
    store.write_bars("SPY", synthetic_bars(date(2024, 1, 2), 30, 0.001, 0.01, 0.0))
    store.write_bars("ZZZ", synthetic_bars(date(2024, 1, 2), 30, 0.001, 0.01, 0.0))
    store.write_dividends("SPY", {date(2024, 1, 10): 1.0})
    rows = build_rows(
        store,
        {"SPY": "etf"},
        date(2024, 6, 1),
        {"ZZZ": ("Zed Corp", "Industrials")},
        {"SPY": (1, 0)},
    )
    by = {r["symbol"]: r for r in rows}
    assert (
        by["SPY"]["kind"] == "etf"
        and by["SPY"]["dividends"] == 1
        and by["SPY"]["quality_errors"] == 1
    )
    assert by["ZZZ"]["kind"] == "stock" and by["ZZZ"]["sector"] == "Industrials"
    md = render_markdown(rows, {"DGS10": (date(2024, 5, 1), 100)}, ["AAPL"])
    assert "DGS10" in md and "SPY" in md and "Industrials" in md


def test_reviewed_exceptions_and_quarantine(tmp_path: Path) -> None:
    from us_stock_research.quality.ohlcv import load_exceptions

    a = DailyBar(date(2024, 1, 2), 100, 101, 99, 100, 100, 1)
    b = DailyBar(date(2024, 1, 3), 60, 61, 59, 60, 100, 1)  # raw -40% but adjusted flat
    assert audit_bars("XYZ", [a, b]).errors
    ok = audit_bars("XYZ", [a, b], {"2024-01-03": "spin-off, second source agrees"})
    assert not ok.errors and any(w.startswith("reviewed:") for w in ok.warnings)

    cfg = tmp_path / "ex.yml"
    cfg.write_text(
        'accepted:\n  XYZ: [{date: "2024-01-03", reason: r}]\nquarantine:\n  BAD: reason\n'
    )
    accepted, quarantine = load_exceptions(cfg)
    assert accepted == {"XYZ": {"2024-01-03": "r"}} and quarantine == {"BAD": "reason"}
    assert load_exceptions(tmp_path / "missing.yml") == ({}, {})


def test_tiingo_iex_parse() -> None:
    from us_stock_research.collectors.tiingo_intraday import parse_iex

    rows = [{"date": "2024-06-03T13:30:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
    bar = parse_iex(rows)[0]
    assert bar["ts"].hour == 13 and bar["volume"] == 0.0 and bar["close"] == 1.5
