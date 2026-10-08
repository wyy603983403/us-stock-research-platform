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
        text = path.read_text()
        if "kind: leveraged_trend" in text:
            from us_stock_research.research.leveraged_trend import load_lt_contract

            assert load_lt_contract(path)["risk"]["max_worst_12m_loss"] <= 0.50
        elif "kind: trend_ensemble" in text:
            from us_stock_research.research.trend_ensemble import load_te_contract

            assert load_te_contract(path)["risk"]["max_worst_12m_loss"] <= 0.50
        elif "kind: sleeve_mix" in text:
            from us_stock_research.research.sleeve_mix import load_mix_contract

            assert load_mix_contract(path)["risk"]["max_worst_12m_loss"] <= 0.50
        elif "kind: factor_sleeve" in text:
            from us_stock_research.research.factor_sleeve import load_fs_contract

            assert load_fs_contract(path)["risk"]["max_worst_12m_loss"] <= 0.50
        elif "kind: cross_section" not in text:  # validated by the xs engine test
            load_contract(path)
    risk = load_risk(ROOT / "configs/risk/default.yml")
    assert risk.trading_enabled is False
    assert risk.max_worst_12m_loss <= 0.50


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


def test_intraday_year_chunks_and_resume_and_rate_limit(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from us_stock_research.collectors import tiingo_intraday as ti

    assert ti.year_chunks(date(2023, 12, 30), date(2025, 1, 2)) == [
        (date(2023, 12, 30), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 1, 2)),
    ]

    class FakeStore:
        def __init__(self) -> None:
            self.data: dict[str, list[tuple[Any, ...]]] = {}

        def has(self, kind: str, key: str) -> bool:
            return key in self.data

        def read(self, kind: str, key: str, columns: str = "*") -> list[tuple[Any, ...]]:
            return self.data[key]

        def write(self, kind: str, key: str, schema: Any, columns: Any, order_by: str) -> None:
            self.data[key] = list(zip(*columns, strict=True))

    calls: list[tuple[str, date, date]] = []

    def bar(d: date) -> dict[str, Any]:
        ts = datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC)
        return {"ts": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}

    def fetch(sym: str, a: date, b: date) -> list[dict[str, Any]]:
        calls.append((sym, a, b))
        if sym == "BAD":
            raise ti.RateLimited("quota")
        return [bar(a)]

    store = FakeStore()
    out = ti.download_intraday(
        ["AAA", "BAD", "CCC"],
        store,
        fetch,
        date(2024, 6, 1),
        date(2025, 3, 1),
        execute=True,
        pause=0,
        sleep=lambda _s: None,
        chunks=ti.year_chunks,
    )
    assert out["stopped_by_rate_limit"] and "BAD" in out["stopped_by_rate_limit"]
    assert "CCC" not in store.data and len(store.data["AAA"]) == 2
    calls.clear()
    ti.download_intraday(
        ["AAA"],
        store,
        fetch,
        date(2024, 6, 1),
        date(2025, 3, 1),
        execute=True,
        pause=0,
        sleep=lambda _s: None,
        chunks=ti.year_chunks,
    )
    assert calls == [("AAA", date(2025, 1, 1), date(2025, 3, 1))]
    calls.clear()
    ti.download_intraday(
        ["AAA"],
        store,
        fetch,
        date(2024, 6, 1),
        date(2024, 8, 10),
        execute=True,
        pause=0,
        sleep=lambda _s: None,
        restart=True,
    )
    assert [c[1] for c in calls] == [date(2024, 6, 1), date(2024, 7, 1), date(2024, 8, 1)]
    assert len(store.data["AAA"]) == 3  # restart discarded the earlier bars


def test_tiingo_fetch_splits_truncated_windows() -> None:
    from us_stock_research.collectors import tiingo_intraday as ti

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return self.rows

    class Client:
        def __init__(self) -> None:
            self.windows: list[tuple[str, str]] = []

        def get(self, url: str, params: dict[str, str]) -> Resp:
            a, b = params["startDate"], params["endDate"]
            self.windows.append((a, b))
            days = (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
            row = {"date": f"{a}T14:30:00.000Z", "open": 1, "high": 1, "low": 1, "close": 1}
            n = ti.MAX_ROWS if days > 2 else 1
            return Resp([dict(row, volume=1)] * n)

    client = Client()
    bars = ti.fetch_chunk(client, "SPY", date(2024, 6, 1), date(2024, 6, 8), "1min", "tok")  # type: ignore[arg-type]
    assert len(client.windows) > 1 and len(bars) == len(client.windows) - sum(
        1 for a, b in client.windows if (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 > 2
    )
    assert ti.month_chunks(date(2024, 12, 15), date(2025, 1, 3)) == [
        (date(2024, 12, 15), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 1, 3)),
    ]


def test_alpaca_parse() -> None:
    from us_stock_research.collectors.alpaca_intraday import parse_alpaca

    bars = parse_alpaca(
        [{"t": "2024-06-03T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "n": 3}]
    )
    assert bars[0]["ts"].isoformat() == "2024-06-03T13:30:00+00:00"
    assert bars[0]["volume"] == 100.0


def test_alpaca_symbol_mapping_and_retry() -> None:
    from us_stock_research.collectors import alpaca_intraday as al

    assert al.to_alpaca("brk-b") == "BRK.B"
    assert al.minute_symbols(["SPY", "^GSPC", "GC=F", "DX-Y.NYB", "BRK-B"]) == ["SPY", "BRK-B"]

    class Resp:
        def __init__(self, code: int, payload: dict[str, Any]) -> None:
            self.status_code, self.payload, self.text = code, payload, ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self.payload

    bar = {"t": "2024-06-03T13:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    queue = [
        Resp(429, {}),
        Resp(200, {"bars": [bar], "next_page_token": "p2"}),
        Resp(200, {"bars": [bar], "next_page_token": None}),
    ]
    seen: list[dict[str, Any]] = []

    class Client:
        def get(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> Resp:
            seen.append(dict(params))
            return queue.pop(0)

    waits: list[float] = []
    bars = al.fetch_chunk(
        Client(),  # type: ignore[arg-type]
        "BRK-B",
        date(2024, 6, 3),
        date(2024, 6, 3),
        "k",
        "s",
        sleep=waits.append,
    )
    assert len(bars) == 2 and waits == [60] and seen[-1]["page_token"] == "p2"


def test_intraday_session_dst_and_early_close() -> None:
    from datetime import datetime

    from us_stock_research.quality import intraday as qi

    assert qi.session_utc(date(2024, 1, 10)) == (
        datetime(2024, 1, 10, 14, 30),
        datetime(2024, 1, 10, 21, 0),
    )
    assert qi.session_utc(date(2024, 7, 10))[0] == datetime(2024, 7, 10, 13, 30)
    assert qi.early_close(date(2024, 11, 29)) and qi.session_minutes(date(2024, 11, 29)) == 210
    assert qi.early_close(date(2024, 7, 3)) and qi.early_close(date(2024, 12, 24))
    assert not qi.early_close(date(2021, 12, 24))  # observed Christmas holiday
    assert not qi.early_close(date(2024, 7, 5))
    assert qi.session_minutes(date(2024, 3, 11)) == 390  # first Monday after DST switch


def _minute_rows(day: date, price: float, minutes: int = 390, start_hour: int = 13) -> list[Any]:
    from datetime import datetime, timedelta

    t0 = datetime(day.year, day.month, day.day, start_hour, 30)
    return [
        (t0 + timedelta(minutes=i), price, price * 1.001, price * 0.999, price, 100.0)
        for i in range(minutes)
    ]


def test_intraday_audit_flags_missing_extra_ohlc_and_splits() -> None:
    from datetime import datetime

    from us_stock_research.quality import intraday as qi

    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)]
    rows: list[Any] = []
    for d in days:
        rows += _minute_rows(d, 100.0)
    rows.append((datetime(2024, 6, 3, 11, 0), 100.0, 100.0, 100.0, 100.0, 1.0))  # pre-market
    closes = {days[0]: 100.0, days[1]: 100.5, days[2]: 50.0, days[3]: 100.0}
    ok = qi.audit_intraday("X", rows, closes)
    assert ok["passed"], ok["reasons"]
    assert ok["outside_session"] == 1 and ok["close_split_factor_days"] == 1
    assert ok["mean_coverage"] == 1.0
    assert qi._split_like(16.0) and qi._split_like(1 / 20) and qi._split_like(1.5)
    assert not qi._split_like(1.1) and not qi._split_like(0.9)
    spun = qi.audit_intraday("X", rows, {d: 100.0 / 1.3057 for d in days})
    assert spun["passed"] and spun["close_adjustment_days"] == 4

    bad = [r for r in rows if r[0].date() != days[1]]  # a missing trading day
    bad += _minute_rows(date(2024, 6, 8), 100.0, 5)  # Saturday
    bad.append((datetime(2024, 6, 6, 15, 0), 100.0, 99.0, 98.0, 100.0, 1.0))  # high < close
    closes[days[3]] = 90.0  # 11% gap, not a split factor
    out = qi.audit_intraday("X", bad, closes)
    text = " ".join(out["reasons"])
    assert not out["passed"]
    assert "missing" in text and "holidays/weekends" in text and "OHLC" in text
    assert "daily close" in text


def test_intraday_source_compare_detects_label_shift() -> None:
    from datetime import timedelta

    from us_stock_research.quality import intraday as qi

    day = date(2024, 6, 3)
    a = [
        (r[0], r[1], r[2], r[3], 100.0 + i * 0.5, r[5])
        for i, r in enumerate(_minute_rows(day, 100.0))
    ]
    same = qi.compare_sources(a, list(a))
    assert same["passed"] and same["common_minutes"] == 390
    shifted = [(r[0] + timedelta(minutes=1), *r[1:]) for r in a]
    out = qi.compare_sources(a, shifted)
    assert not out["passed"] and any("shifted" in x for x in out["reasons"])


def test_parse_splits() -> None:
    from us_stock_research.collectors.splits import parse_splits

    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": -14400},
                    "events": {
                        "splits": {
                            "1": {"date": 1598880600, "numerator": 4, "denominator": 1},
                            "2": {"date": 1402320600, "numerator": 7, "denominator": 1},
                        }
                    },
                }
            ]
        }
    }
    assert parse_splits(payload) == [(date(2014, 6, 9), 7.0, 1.0), (date(2020, 8, 31), 4.0, 1.0)]
    assert parse_splits({"chart": {"result": [{"meta": {}}]}}) == []


def test_parse_french_factor_files() -> None:
    from us_stock_research.collectors.factors import parse_french_csv

    daily = (
        "This file was created by CMPT_ME_BEME_RETS_DAILY using the 202508 CRSP database.\n"
        "The Tbill return is from Ibbotson and Associates, Inc.\n\n"
        "              ,Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "19630701,   -0.67,    0.02,   -0.35,    0.03,    0.13,    0.012\n"
        "19630702,    0.79,   -0.28,    0.28,   -0.08,   -0.21,    0.012\n"
    )
    header, days, values = parse_french_csv(daily)
    assert header == ["mkt_rf", "smb", "hml", "rmw", "cma", "rf"]
    assert days == [date(1963, 7, 1), date(1963, 7, 2)]
    assert values[0][0] == pytest.approx(-0.0067)
    monthly = (
        "Missing data are indicated by -99.99 or -999.\n\n"
        "          ,Mom   \n"
        "192701,    0.57\n"
        "192702,  -99.99\n\n"
        " Annual Factors: January-December \n"
        "          ,Mom   \n"
        "1928,   25.00\n"
    )
    header, days, values = parse_french_csv(monthly)
    assert header == ["mom"] and days == [date(1927, 1, 1), date(1927, 2, 1)]
    assert values[0][0] == pytest.approx(0.0057) and values[1][0] != values[1][0]  # NaN
    six = (
        "  Average Value Weighted Returns -- Daily\n"
        ",SMALL LoPRIOR,ME1 PRIOR2,SMALL HiPRIOR,BIG LoPRIOR,ME2 PRIOR2,BIG HiPRIOR\n"
        "19260105,   0.10,  0.20,  0.30,  0.40,  0.50,  0.60\n\n"
        "  Average Equal Weighted Returns -- Daily\n"
        ",SMALL LoPRIOR,ME1 PRIOR2,SMALL HiPRIOR,BIG LoPRIOR,ME2 PRIOR2,BIG HiPRIOR\n"
        "19260105,   9.00,  9.00,  9.00,  9.00,  9.00,  9.00\n"
    )
    header, days, values = parse_french_csv(six)  # value-weighted table only
    assert header[-1] == "big_hiprior" and header[1] == "me1_prior2"
    assert values == [[pytest.approx(x / 100) for x in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)]]


def test_sp500_membership_intervals() -> None:
    from us_stock_research.collectors.sp500_history import intervals, members_on, parse_history

    text = (
        "date,tickers\n"
        '2000-01-03,"AAA,BRK.B,OLD"\n'
        '2005-06-01,"AAA,BRK.B,NEW"\n'
        '2010-01-04,"AAA,BRK.B,NEW,OLD"\n'
    )
    history = intervals(parse_history(text))
    assert ("OLD", date(2000, 1, 3), date(2005, 6, 1)) in history
    assert ("OLD", date(2010, 1, 4), None) in history
    assert ("BRK-B", date(2000, 1, 3), None) in history
    assert members_on(history, date(2007, 1, 1)) == {"AAA", "BRK-B", "NEW"}


def test_former_member_download_validates_window_and_waits_on_quota() -> None:
    from us_stock_research.calendar import trading_days
    from us_stock_research.collectors import sp500_history as sh
    from us_stock_research.collectors.tiingo_intraday import RateLimited

    class Store:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

        def has(self, kind: str, key: str) -> bool:
            return key in self.data

        def read(self, kind: str, key: str, columns: str = "*") -> list[tuple[Any, ...]]:
            return [(r[0],) for r in self.data[key]]

        def write(self, kind: str, key: str, schema: Any, cols: Any, order: str) -> None:
            self.data[key] = list(zip(*cols, strict=True))

    window = trading_days(date(2003, 1, 2), date(2003, 12, 31))

    def rows(days: list[date]) -> list[list[Any]]:
        return [[d, 1.0, 1.0, 1.0, 1.0, 1.0, 10.0, 0.0, 1.0] for d in days]

    later = trading_days(date(2020, 1, 2), date(2020, 3, 31))  # re-used ticker, wrong company
    yahoo = {"REUSED": rows(later), "LIVE": rows(window)}
    tiingo_calls: list[str] = []
    quota = {"hits": 1}

    def tiingo(symbol: str) -> list[list[Any]]:
        tiingo_calls.append(symbol)
        if symbol == "GONE" and quota["hits"]:
            quota["hits"] -= 1
            raise RateLimited("hourly allocation")
        return rows(window) if symbol == "GONE" else []

    targets = [
        (s, date(2003, 1, 2), date(2004, 1, 2)) for s in ("GONE", "LIVE", "MISSING", "REUSED")
    ]
    store, waits = Store(), []
    out = sh.collect_delisted(
        targets,
        store,
        [("yahoo", lambda s: yahoo.get(s, [])), ("tiingo", tiingo)],
        date(2026, 9, 29),
        execute=True,
        pause=0,
        wait_minutes=61,
        max_waits=3,
        sleep=waits.append,
        skip={"SKIPPED": "not_found"},
    )
    res = out["results"]
    assert res["GONE"]["status"] == "stored" and res["GONE"]["source"] == "tiingo"
    assert res["LIVE"]["source"] == "yahoo" and "LIVE" not in tiingo_calls
    assert res["MISSING"]["status"] == "not_found"
    assert res["REUSED"]["status"] == "rejected_window" and "REUSED" not in store.data
    assert out["waits"] == 1 and 61 * 60 in waits
    assert sorted(store.data) == ["GONE", "LIVE"]


def test_intraday_market_bad_days_and_exceptions() -> None:
    from us_stock_research.quality import intraday as qi

    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    audits: dict[str, Any] = {}
    for i in range(10):
        rows: list[Any] = []
        for d in days:
            n = 60 if (d == days[1] and i < 5) else 390  # half the market stops early on day 2
            rows += _minute_rows(d, 100.0, n)
        audits[f"S{i}"] = qi.audit_intraday(f"S{i}", rows, None)
    assert audits["S0"]["truncated_days"] == ["2024-06-04"]
    assert qi.market_bad_days(audits) == {"2024-06-04": "5/10"}

    bad, start = qi.load_intraday_exceptions(
        Path("configs/intraday_exceptions.yml"), "alpaca", "SW"
    )
    assert date(2024, 12, 23) in bad and start == date(2024, 7, 8)
    kept = qi.regular_session(_minute_rows(date(2024, 12, 23), 1.0, 5), exclude=bad)
    assert kept == []


def test_factor_attribution_recovers_loadings() -> None:
    import random

    from us_stock_research.research.attribution import FACTORS, attribute, monthly_returns

    rng = random.Random(7)
    factors: dict[tuple[int, int], dict[str, float]] = {}
    returns: dict[tuple[int, int], float] = {}
    for i in range(240):
        m = (2000 + i // 12, i % 12 + 1)
        f = {name: rng.gauss(0.005, 0.04) for name in FACTORS}
        f["rf"] = 0.002
        factors[m] = f
        returns[m] = 0.002 + 0.001 + 0.5 * f["mkt_rf"] + 0.3 * f["mom"] + rng.gauss(0, 0.002)
    out = attribute(returns, factors)
    assert abs(out["loadings"]["mkt_rf"] - 0.5) < 0.02
    assert abs(out["loadings"]["mom"] - 0.3) < 0.02
    assert abs(out["loadings"]["hml"]) < 0.02
    assert abs(out["alpha_annual"] - 0.012) < 0.004 and out["alpha_t"] > 3
    assert out["r2"] > 0.95
    curve = monthly_returns(["2020-01-02", "2020-01-31", "2020-02-28", "2020-03-31"], [1, 2, 3, 6])
    assert curve == {(2020, 2): 0.5, (2020, 3): 1.0}


def test_order_intent_rehearsal_reduce_only_and_breaker(data_dir: Path, tmp_path: Path) -> None:
    import json as _json

    from us_stock_research.storage import CsvStore
    from us_stock_research.trading import order_intent as oi

    c = contract()
    store = CsvStore(data_dir)
    as_of = date(2019, 6, 28)
    clean = lambda s, b: []  # noqa: E731
    cash = oi.Holdings(cash_usd=100_000.0)
    intent = oi.generate(c, store, cash, as_of, quality=clean)
    assert intent["mode"] == "rehearsal" and intent["trading_enabled"] is False
    assert intent["signal_day"] == "2019-06-28" and not intent["reduce_only"]
    assert abs(sum(intent["target_weights"].values()) - 1) < 1e-9
    assert intent["orders"] and all(o["side"] == "BUY" for o in intent["orders"])
    assert sum(o["est_value_usd"] for o in intent["orders"]) <= 100_000

    held = oi.Holdings(cash_usd=0.0, positions={"QQQ": 500.0, "IEF": 10.0})
    bad = oi.generate(c, store, held, as_of, quality=lambda s, b: ["bad"] if s == "IEF" else [])
    assert bad["reduce_only"] and all(o["side"] == "SELL" for o in bad["orders"])

    peak = oi.Holdings(cash_usd=10_000.0, peak_nav_usd=1_000_000.0)
    tripped = oi.generate(c, store, peak, as_of, quality=clean)
    assert any("circuit breaker" in r for r in tripped["reduce_only_reasons"])
    assert tripped["orders"] == []

    with pytest.raises(ValueError):
        oi.generate(c, store, cash, date(2019, 6, 20), quality=clean)  # not month end

    path = oi.write_outputs(intent, tmp_path / "orders")
    assert _json.loads(path.read_text())["study"] == c.name
    assert "人工复核" in path.with_suffix(".md").read_text()
    oi.write_outputs(intent, tmp_path / "orders")
    log = (tmp_path / "orders" / "audit_log.jsonl").read_text().splitlines()
    assert len(log) == 2  # append-only


def test_cross_section_point_in_time_engine() -> None:
    from us_stock_research.calendar import trading_days
    from us_stock_research.research import cross_section as xs

    days = trading_days(date(2018, 6, 1), date(2021, 3, 31))
    n = len(days)

    def path(daily: float, stop: int | None = None) -> list[float | None]:
        out: list[float | None] = []
        p = 100.0
        for i in range(n):
            p *= 1 + daily
            out.append(None if stop is not None and i >= stop else p)
        return out

    cut = days.index(date(2020, 6, 15))
    prices = xs.Prices(
        days,
        {
            "WIN": path(0.002),  # strongest momentum
            "MID": path(0.001),
            "LOSE": path(-0.001),
            "GONE": path(0.003, stop=cut),  # best momentum, then delisted mid-month
            "LATE": path(0.004),  # strongest, but only joins the index in 2021
            "SPY": path(0.001),
        },
    )
    history = [
        ("WIN", date(2000, 1, 1), None),
        ("MID", date(2000, 1, 1), None),
        ("LOSE", date(2000, 1, 1), None),
        ("GONE", date(2000, 1, 1), date(2020, 7, 1)),
        ("LATE", date(2021, 1, 15), None),
    ]
    contract = {
        "name": "t",
        "universe": {"start": date(2020, 1, 1), "end": date(2021, 3, 31), "min_history_days": 273},
        "signal": {"name": "momentum_12_1", "lookback_days": 252, "skip_days": 21},
        "selection": {"top_n": 2},
        "execution_lag_days": 1,
        "transaction_cost_bps": 10,
    }
    out = xs.run(contract, prices, history, haircut=-0.3)
    months = {m["date"][:7]: m for m in out["months"]}
    assert months["2020-01"]["top"] == ["GONE", "WIN"]
    assert months["2020-05"]["early_exits"] == 1  # GONE ends inside June's holding period
    assert "GONE" not in months["2020-07"]["top"] and months["2020-07"]["members"] == 3
    assert "LATE" not in months["2020-12"]["top"]  # not a member yet: no look-ahead
    assert months["2021-01"]["members"] == 4 and months["2021-01"]["coverage"] == 1.0
    assert months["2020-01"]["turnover_one_way"] == 0.5  # from cash: half of |dw| = 1
    i = days.index(date(2020, 1, 31)) + 1
    j = days.index(date(2020, 2, 28)) + 1
    r = [xs.holding_return(prices, s, i, j, -0.3)[0] for s in ("GONE", "WIN")]
    assert abs(months["2020-01"]["strategy"] - (sum(r) / 2 - 0.001)) < 1e-12
    # May's holding period contains GONE's delisting: sold at the last close, then a 30% haircut
    assert months["2020-05"]["strategy"] < months["2020-04"]["strategy"] - 0.1
    low = dict(contract, signal={"name": "low_volatility", "lookback_days": 252})
    assert xs.run(low, prices, history)["months"][0]["picks"] == 2
    summary = xs.summarize(
        dict(
            contract,
            inference={
                "resamples": 200,
                "block_size_months": 3,
                "confidence_level": 0.9,
                "random_seed": 1,
            },
        ),
        out,
    )
    assert summary["strategy"]["months"] == len(out["months"])
    assert summary["coverage_min"] == months["2020-06"]["coverage"] == 0.75  # GONE unpriced
    for path_ in (ROOT / "research").glob("*/study.yml"):
        if "kind: cross_section" in path_.read_text():
            assert xs.load_xs_contract(path_)["selection"]["top_n"] in (30, 50, 100)


def test_ticker_aliases_borrow_successor_history() -> None:
    from us_stock_research.research import cross_section as xs

    aliases = xs.load_aliases(ROOT / "configs/ticker_aliases.yml")
    assert aliases["FB"] == "META" and "WRK" not in aliases
    prices = xs.Prices([date(2020, 1, 2)], {"META": [1.0], "OLD": [2.0]})
    assert xs.apply_aliases(prices, {"FB": "META", "OLD": "META", "X": "NONE"}) == ["FB->META"]
    assert prices.series["FB"] is prices.series["META"] and prices.series["OLD"] == [2.0]


def test_delisted_bars_split_adjust_tiingo_raw_prices() -> None:
    from us_stock_research.collectors.sp500_history import delisted_bars

    rows = [
        (date(2020, 1, 2), 100.0, 101.0, 99.0, 100.0, 50.0, 10.0, 1.0),
        (date(2020, 1, 3), 51.0, 52.0, 50.0, 51.0, 51.0, 10.0, 2.0),  # 2-for-1 ex-date
        (date(2020, 1, 6), 52.0, 53.0, 51.0, 52.0, 52.0, 10.0, 1.0),
    ]
    bars = delisted_bars(rows)
    assert [b.close for b in bars] == [50.0, 51.0, 52.0]
    assert audit_bars("X", bars).passed


def test_research_windows_cover_membership_plus_lookback() -> None:
    from us_stock_research.collectors.sp500_history import research_windows

    w = research_windows(
        [("X", date(2010, 1, 4), date(2012, 1, 3)), ("X", date(2015, 1, 2), date(2016, 1, 4))]
    )
    assert w["X"][0] < date(2008, 11, 1) and w["X"][1] == date(2016, 1, 4)


def test_reviewed_gap_can_be_accepted_by_first_date() -> None:
    from us_stock_research.quality.ohlcv import QualityReport, _apply_reviewed

    report = QualityReport(symbol="X", rows=1, first=None, last=None)
    report.errors = ["17 NYSE trading days missing (2017-08-07 .. 2017-08-31)", "2020-01-02: x"]
    out = _apply_reviewed(report, {"2017-08-07": "reviewed"})
    assert out.errors == ["2020-01-02: x"] and "reviewed" in out.warnings[0]


def test_last_closed_session() -> None:
    from datetime import datetime

    from us_stock_research.quality.intraday import last_closed_session

    # Beijing 07:30 on 2026-10-01 = 2026-09-30 23:30 UTC: that day's session closed at 20:00 UTC
    assert last_closed_session(datetime(2026, 9, 30, 23, 30)) == date(2026, 9, 30)
    assert last_closed_session(datetime(2026, 9, 30, 15, 0)) == date(2026, 9, 29)  # mid-session
    assert last_closed_session(datetime(2026, 10, 4, 12, 0)) == date(2026, 10, 2)  # Sunday
    assert last_closed_session(datetime(2024, 11, 29, 18, 30)) == date(2024, 11, 29)  # 13:00 ET


def test_equal_weight_reference_comparison() -> None:
    from us_stock_research.calendar import trading_days
    from us_stock_research.research import cross_section as xs

    days = trading_days(date(2020, 1, 1), date(2021, 12, 31))
    ref = [100.0 * (1.001**i) for i in range(len(days))]
    prices = xs.Prices(days, {"RSP": ref})  # type: ignore[dict-item]
    ends = xs.month_end_indices(days, date(2020, 1, 1), date(2021, 12, 31))
    months = []
    for t, t2 in zip(ends, ends[1:], strict=False):
        r = ref[min(t2 + 1, len(days) - 1)] / ref[t + 1] - 1
        months.append({"date": days[t].isoformat(), "coverage": 1.0, "equal_weight": r})
    out = xs.compare_to_reference(months, prices, "RSP", 1, 0.9)
    assert out["months"] == len(months) - 1 and abs(out["mean_monthly_diff"]) < 1e-12


def test_independent_cross_section_matches_engine() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import cross_section as xs
    from us_stock_research.research import verify_xsec as vx

    rng = random.Random(3)
    days = trading_days(date(2018, 1, 2), date(2021, 6, 30))
    series: dict[str, list[float | None]] = {}
    for k in range(12):
        p, out = 50.0 + k, []
        stop = len(days) - 200 * (k % 3 == 0) if k % 4 == 0 else None
        for i in range(len(days)):
            p *= 1 + rng.gauss(0.0004 * (k - 5), 0.015)
            gap = rng.random() < 0.01  # sprinkle missing days to exercise stale lookups
            out.append(None if gap or (stop is not None and i >= stop) else p)
        series[f"S{k}"] = out
    prices = xs.Prices(days, series)
    history = [(f"S{k}", date(2015, 1, 1), None) for k in range(10)]
    history += [("S10", date(2020, 3, 2), None), ("S11", date(2015, 1, 1), date(2020, 1, 2))]
    base = {
        "name": "t",
        "universe": {"start": date(2019, 3, 1), "end": date(2021, 5, 31), "min_history_days": 273},
        "selection": {"top_n": 3},
        "execution_lag_days": 1,
        "transaction_cost_bps": 10,
    }
    as_dicts = {
        s: {d: v for d, v in zip(days, vals, strict=True) if v is not None}
        for s, vals in series.items()
    }
    for sig in (
        {"name": "momentum_12_1", "lookback_days": 252, "skip_days": 21},
        {"name": "low_volatility", "lookback_days": 252},
    ):
        c = dict(base, signal=sig)
        span = (date(2019, 6, 1), date(2020, 6, 1))
        for cut in (0.0, -0.3):
            a = xs.run(c, prices, history, haircut=cut, excluded={"S5"}, blocked={"S2": [span]})[
                "months"
            ]
            b = vx.backtest(
                c, as_dicts, history, haircut=cut, excluded={"S5"}, blocked=[("S2", *span)]
            )
            result = vx.compare(a, b)
            assert result["match"], (sig["name"], cut, result)


def _zip(files: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_sec_bulk_parsers_and_quarters() -> None:
    from us_stock_research.collectors import sec_bulk as sb

    assert sb.quarters("2009q3", "2010q2") == ["2009q3", "2009q4", "2010q1", "2010q2"]
    assert sb.latest_quarter(date(2026, 9, 30)) == "2026q2"
    assert sb.latest_quarter(date(2026, 2, 1)) == "2025q4"
    assert sb.parse_date("31-MAR-2024") == date(2024, 3, 31)
    assert sb.parse_date("20240331") == date(2024, 3, 31) and sb.parse_date("") is None

    sub = (
        "adsh\tcik\tname\tsic\tform\tperiod\tfy\tfp\tfiled\n"
        "A1\t320193\tAPPLE INC\t3571\t10-Q\t20240331\t2024\tQ2\t20240503\n"
        "A2\t1\tFUND\t\tN-CSR\t20240331\t2024\tQ2\t20240503\n"
    )
    num = (
        "adsh\ttag\tversion\tddate\tqtrs\tuom\tsegments\tcoreg\tvalue\tfootnote\n"
        "A1\tAssets\tus-gaap/2023\t20240331\t0\tUSD\t\t\t337411000000\t\n"
        "A1\tAssets\tus-gaap/2023\t20240331\t0\tUSD\tBusinessSegments=X;\t\t1\t\n"  # segment
        "A1\tRevenues\t0000320193-24-000069\t20240331\t1\tUSD\t\t\t5\t\n"  # custom tag
        "A1\tEntityCommonStockSharesOutstanding\tdei/2023\t20240419\t0\tshares\t\t\t15\t\n"
        "A1\tFooBar\tus-gaap/2023\t20240331\t0\tUSD\t\t\t9\t\n"  # not in TAGS
        "A2\tAssets\tus-gaap/2023\t20240331\t0\tUSD\t\t\t7\t\n"  # not a 10-K/10-Q
    )
    rows = sb.parse_fsds(_zip({"sub.txt": sub, "num.txt": num}))
    assert [(r[9], r[13]) for r in rows] == [
        ("Assets", 337411000000.0),
        ("EntityCommonStockSharesOutstanding", 15.0),
    ]
    assert rows[0][3] == 3571 and rows[0][8] == date(2024, 5, 3)

    ins = sb.parse_insider(
        _zip(
            {
                "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tISSUERCIK\tISSUERNAME\t"
                "ISSUERTRADINGSYMBOL\nX1\t02-JAN-2008\t1001\tLEHMAN BROTHERS\tleh\n",
                "REPORTINGOWNER.tsv": "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\t"
                "RPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\nX1\t77\tFULD\tOfficer\tCEO\n",
                "NONDERIV_TRANS.tsv": "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\t"
                "TRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\t"
                "DIRECT_INDIRECT_OWNERSHIP\nX1\t28-DEC-2007\tP\t1000\t62.5\tA\t5000\tD\n",
            }
        )
    )
    assert ins == [
        [
            "X1",
            date(2008, 1, 2),
            1001,
            "LEHMAN BROTHERS",
            "LEH",
            77,
            "FULD",
            "Officer",
            "CEO",
            date(2007, 12, 28),
            "P",
            1000.0,
            62.5,
            "A",
            5000.0,
            "D",
        ]
    ]


def test_cik_map_picks_the_company_using_the_ticker_during_membership() -> None:
    from us_stock_research.collectors.cik_map import choose

    filings = {
        "AAL": [(6201, date(2016, 5, 1), "AMERICAN AIRLINES")] * 3,
        "Q": [(68622, date(2008, 1, 5), "QWEST")] * 4 + [(9999, date(2025, 12, 1), "QNITY")] * 2,
        "DUP": [(1, date(2012, 1, 1), "A")] * 2 + [(2, date(2012, 2, 1), "B")] * 2,
    }
    intervals = [
        ("AAL", date(2015, 3, 23), date(2024, 9, 23)),
        ("Q", date(2000, 7, 6), date(2011, 4, 1)),
        ("Q", date(2025, 11, 3), None),
        ("DUP", date(2011, 1, 1), None),
        ("NONE", date(2010, 1, 1), None),
        ("OLD", date(1996, 1, 2), date(2005, 1, 1)),
    ]
    filings["DUP"] = [  # two filers alternating, each run of two: no clear owner
        (1 + (m // 2) % 2, date(2012, m + 1, 1), "AB"[(m // 2) % 2]) for m in range(8)
    ]
    filings["ETN"] = [(1001, date(2011, 5, 1), "EATON CORP")] * 3 + [
        (2002, date(2013, 5, 1), "EATON CORP PLC")
    ] * 3  # re-domiciled: new CIK from late 2012
    filings["BTU"] = [(3003, date(2012, 1, 1), "PEABODY")] * 2
    filings["FOXA"] = [(4004, date(2020, 1, 1), "FOX CORP")] * 2
    filings["BK"] = [(5005, date(2025, 1, 1), "BANK OF NEW YORK MELLON")] * 2
    intervals += [
        ("ETN", date(2000, 1, 1), None),
        ("BTUUQ", date(2010, 1, 1), date(2015, 1, 1)),
        ("FOX", date(2019, 3, 1), None),
        ("BNY", date(2024, 12, 1), None),
    ]
    filings["SATS"] = [(6006, date(2026, 6, 1), "ECHOSTAR")]
    filings["ECHO"] = [(7007, date(2021, 11, 1), "ECHO GLOBAL LOGISTICS")] * 5
    intervals += [("ECHO", date(2026, 6, 24), None), ("FRC", date(2019, 1, 2), date(2023, 5, 4))]
    rows, stats = choose(
        intervals,
        filings,
        {"NONE": 42, "FRC": None, "DXC@1996-01-02": 23082},
        date(2009, 1, 1),
        {"BK": "BNY", "SATS": "ECHO"},
        data_end=date(2026, 6, 18),
    )
    got = {(r[0], r[1]): (r[3], r[5]) for r in rows}
    assert got[("AAL", date(2015, 3, 23))] == (6201, "insider")
    assert got[("Q", date(2000, 7, 6))] == (68622, "insider")
    assert got[("Q", date(2025, 11, 3))] == (9999, "insider")
    assert got[("DUP", date(2011, 1, 1))] == (None, "ambiguous")
    assert got[("NONE", date(2010, 1, 1))] == (42, "override")
    assert got[("ETN", date(2000, 1, 1))] == (1001, "insider+split")
    assert got[("ETN", date(2013, 5, 1))] == (2002, "insider+split")
    assert got[("BTUUQ", date(2010, 1, 1))] == (3003, "bankrupt:BTU")
    assert got[("FOX", date(2019, 3, 1))] == (4004, "class:FOXA")
    assert got[("BNY", date(2024, 12, 1))] == (5005, "alias:BK")
    assert got[("ECHO", date(2026, 6, 24))] == (6006, "recent-alias:SATS")  # not the old ECHO
    assert got[("FRC", date(2019, 1, 2))] == (None, "override:no_sec_filer")
    assert stats["before_since"] == 1


def test_identity_blocks_reused_tickers_and_dated_alias_splices() -> None:
    from us_stock_research.research import cross_section as xs
    from us_stock_research.research.identity import build

    d = date

    history = [
        ("TT", date(2002, 5, 13), date(2008, 6, 6)),  # American Standard / Trane Inc.
        ("TT", date(2020, 3, 3), None),  # Trane Technologies (ex Ingersoll-Rand)
        ("RIG", date(1999, 12, 31), date(2008, 12, 19)),
        ("RIG", date(2010, 1, 1), None),
        ("CNC", date(1997, 1, 15), date(2002, 7, 25)),  # Conseco; Centene trades from 2001-12
        ("CNC", date(2016, 3, 30), None),
        ("ETN", date(2000, 1, 1), None),  # re-domiciled 2012: two CIKs, one continuous series
        ("IR", date(2010, 11, 17), None),
    ]
    segments = {
        "TT": [
            (date(2002, 5, 13), date(2008, 6, 6), 1, "TRANE INC"),
            (date(2020, 3, 3), None, 2, "TT"),
        ],
        "RIG": [(date(2006, 1, 1), date(2008, 12, 19), 10, "A"), (date(2010, 1, 1), None, 11, "B")],
        "ETN": [
            (date(2000, 1, 1), date(2012, 12, 1), 20, "EATON"),
            (date(2012, 12, 1), None, 21, "PLC"),
        ],
        "IR": [
            (date(2010, 11, 17), date(2020, 3, 2), 30, "OLD"),
            (date(2020, 3, 2), None, 31, "NEW"),
        ],
    }
    first = {"TT": date(2000, 1, 3), "RIG": date(2000, 1, 3), "CNC": date(2001, 12, 13)}
    first |= {"ETN": date(2000, 1, 3), "IR": date(2017, 5, 12)}
    blocked = build(history, segments, first, {"RIG@1999-12-31"}, set())
    keys = {(s, a) for s, a, _b, _r in blocked}
    assert ("TT", date(2002, 5, 13)) in keys  # different CIK than today's TT
    assert ("RIG", date(1999, 12, 31)) not in keys  # reviewed: same company
    assert ("CNC", date(1997, 1, 15)) in keys  # series starts mid-interval
    assert not any(s == "ETN" for s, *_ in blocked)  # reorganisation, continuous series
    etn96 = [("ETN", d(1996, 1, 2), None)]
    etn_segs = {"ETN": [(d(1996, 1, 2), d(2012, 12, 1), 20, "E"), (d(2012, 12, 1), None, 21, "P")]}
    assert build(etn96, etn_segs, {"ETN": d(2000, 1, 3)}, set(), set()) == []  # pre-2000 start
    fox = build(
        [("FOXA", d(2004, 12, 20), None)],
        {"FOXA": [(d(2004, 12, 20), d(2019, 3, 20), 1, "21CF"), (d(2019, 3, 20), None, 2, "FOX")]},
        {"FOXA": d(2019, 3, 12)},
        set(),
        set(),
    )
    assert {(a, b) for _s, a, b, _r in fox} == {(d(2004, 12, 20), d(2019, 3, 20))}
    manual = [("CB", d(1996, 1, 2), d(2016, 1, 19), "reviewed")]
    assert build([], {}, {}, set(), set(), manual) == manual
    assert ("IR", date(2010, 11, 17)) in keys  # old IR part not covered by today's IR series
    assert not any(s == "IR" for s, *_ in build(history, segments, first, set(), {"IR"}))

    days = [date(2020, 2, 27), date(2020, 2, 28), date(2020, 3, 2), date(2020, 3, 3)]
    prices = xs.Prices(days, {"TT": [1.0, 2.0, 3.0, 4.0], "IR": [None, 9.0, 9.5, 10.0]})
    xs.apply_aliases(prices, {"IR@2020-03-02": "TT"})
    assert prices.series["IR"] == [1.0, 2.0, 9.5, 10.0]
    assert xs.is_blocked({"TT": [(date(2002, 5, 13), date(2008, 6, 6))]}, "TT", date(2005, 1, 3))
    assert not xs.is_blocked(
        {"TT": [(date(2002, 5, 13), date(2008, 6, 6))]}, "TT", date(2021, 1, 4)
    )


def test_parse_chart_keeps_first_bar_of_a_repeated_day() -> None:
    def bar(ts: int, close: float) -> tuple[int, float]:
        return ts, close

    rows = [bar(1727697600, 101.45), bar(1727740800, 101.478)]  # both 2024-09-30 at UTC-4
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": -14400},
                    "timestamp": [r[0] for r in rows],
                    "indicators": {
                        "quote": [
                            {
                                "open": [r[1] for r in rows],
                                "high": [r[1] for r in rows],
                                "low": [r[1] for r in rows],
                                "close": [r[1] for r in rows],
                                "volume": [0, 0],
                            }
                        ],
                    },
                }
            ],
            "error": None,
        }
    }
    bars = parse_chart(payload, allow_missing_adj=True)
    assert len(bars) == 1 and bars[0].close == 101.45


QV_SPEC = {
    "max_age_days": 550,
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "assets": ["Assets"],
    "equity": ["StockholdersEquity"],
    "shares": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "CommonStockSharesOutstanding",
    ],
}
QV_SIGNAL = {
    "name": "quality_value",
    "quality": ["roa", "cfoa"],
    "value": ["earnings_yield", "book_to_market", "cash_flow_yield"],
}


def _annual(adsh, cik, sic, period, filed, ni, cfo, assets, equity, shares):  # type: ignore[no-untyped-def]
    rows = [
        (adsh, cik, sic, period, filed, "NetIncomeLoss", 4, "USD", ni),
        (adsh, cik, sic, period, filed, "NetCashProvidedByUsedInOperatingActivities", 4, "USD",
         cfo),
        (adsh, cik, sic, period, filed, "Assets", 0, "USD", assets),
        (adsh, cik, sic, period, filed, "StockholdersEquity", 0, "USD", equity),
        (adsh, cik, sic, period, filed, "WeightedAverageNumberOfDilutedSharesOutstanding", 4,
         "shares", shares),
    ]  # fmt: skip
    return [r for r in rows if r[-1] is not None]


def test_point_in_time_annual_reports() -> None:
    from us_stock_research.research import fundamentals as fu

    tags = fu.tag_lists(QV_SPEC)
    rows = _annual(
        "A1", 7, 3571, date(2020, 12, 31), date(2021, 2, 20), 10.0, 12.0, 100.0, 50.0, 5.0
    )
    rows += _annual(
        "A2", 7, 3571, date(2021, 12, 31), date(2022, 2, 25), 20.0, 25.0, 120.0, 60.0, 6.0
    )
    # amendment of 2021 restating only net income; a stray quarterly-length value is ignored
    rows += [("A3", 7, 3571, date(2021, 12, 31), date(2022, 4, 1), "NetIncomeLoss", 4, "USD", 18.0)]
    rows += [("A2", 7, 3571, date(2021, 12, 31), date(2022, 2, 25), "Assets", 4, "USD", 1.0)]
    reports = fu.parse_rows(rows, tags)[7]
    assert fu.resolve(reports, date(2021, 2, 19), tags, 550) is None  # nothing filed yet
    got = fu.resolve(reports, date(2022, 1, 31), tags, 550)
    assert got and got["period"] == date(2020, 12, 31) and got["net_income"] == 10.0
    got = fu.resolve(reports, date(2022, 3, 31), tags, 550)
    assert got and got["net_income"] == 20.0 and got["assets"] == 120.0
    got = fu.resolve(reports, date(2022, 4, 29), tags, 550)
    assert got and got["net_income"] == 18.0 and got["operating_cash_flow"] == 25.0
    assert got["filed"] == date(2022, 2, 25)  # shares come from the original filing
    assert fu.resolve(reports, date(2023, 7, 31), tags, 550) is None  # older than 550 days

    # market value: Yahoo close is adjusted for a later 2:1 split; shares are pre-split
    splits = [(date(2022, 6, 1), 2.0)]
    assert fu.split_ratio(splits, date(2022, 2, 25), date(2022, 6, 30)) == 2.0
    assert fu.split_ratio(splits, date(2022, 6, 1), None) == 1.0
    days = [date(2022, 5, 31), date(2022, 6, 1)]
    actual = fu.actual_prices({"X": [50.0, 51.0]}, days, {"X"}, {"X": splits})
    assert actual["X"] == [100.0, 51.0]

    ranks = fu.percentile_ranks({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0})
    assert ranks == {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0}
    per = {
        "a": {"roa": 0.1, "cfoa": None, "earnings_yield": 0.05, "book_to_market": None,
              "cash_flow_yield": 0.06},
        "b": {"roa": 0.2, "cfoa": 0.3, "earnings_yield": 0.01, "book_to_market": None,
              "cash_flow_yield": None},  # one value metric only: not scored
        "c": {"roa": None, "cfoa": None, "earnings_yield": 0.02, "book_to_market": 0.5,
              "cash_flow_yield": 0.03},  # no quality metric: not scored
    }  # fmt: skip
    assert set(fu.combine(per, QV_SIGNAL)) == {"a"}
    assert fu.metrics({"net_income": 1.0, "assets": -1.0, "equity": -2.0}, 10.0) == {
        "roa": None, "cfoa": None, "earnings_yield": 0.1, "book_to_market": None,
        "cash_flow_yield": None,
    }  # fmt: skip


def test_market_aliases_and_split_records() -> None:
    from us_stock_research.collectors.splits import merge_checked
    from us_stock_research.research import fundamentals as fu

    days = [date(2020, 2, 27), date(2020, 2, 28), date(2020, 3, 2), date(2020, 3, 3)]
    actual = {"TT": [1.0, 2.0, 3.0, 4.0], "IR": [None, None, 30.0, 40.0], "NEW": [5.0] * 4}
    splits = {"TT": [(date(2020, 2, 28), 2.0)], "IR": [(date(2020, 3, 3), 3.0)], "NEW": []}
    known = {"TT", "IR", "NEW"}
    fu.apply_aliases_market(actual, splits, known, days, {"IR@2020-03-02": "TT", "OLD": "NEW"})
    assert actual["IR"] == [1.0, 2.0, 30.0, 40.0]
    assert splits["IR"] == [(date(2020, 2, 28), 2.0), (date(2020, 3, 3), 3.0)]
    assert actual["OLD"] == [5.0] * 4 and "OLD" in known
    gone = {"GPS": [7.0], "GAP": [7.0]}
    gone_splits: dict[str, list[tuple[date, float]]] = {"GAP": [(date(2020, 1, 2), 2.0)]}
    gone_known = {"GAP"}
    fu.apply_aliases_market(gone, gone_splits, gone_known, days[:1], {"GPS": "GAP"})
    assert "GPS" in gone_known and gone_splits["GPS"] == gone_splits["GAP"]
    rows = merge_checked([("A", date(2026, 1, 1), 0), ("B", date(2026, 1, 1), 1)], {"B": 2},
                         date(2026, 10, 1))  # fmt: skip
    assert rows == [("A", date(2026, 1, 1), 0), ("B", date(2026, 10, 1), 2)]


def test_quality_value_engine_matches_independent() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import cross_section as xs
    from us_stock_research.research import fundamentals as fu
    from us_stock_research.research import verify_xsec as vx

    rng = random.Random(11)
    days = trading_days(date(2017, 1, 3), date(2021, 6, 30))
    series: dict[str, list[float | None]] = {}
    rows: list[tuple] = []  # type: ignore[type-arg]
    segments: dict[str, list[tuple[date, date | None, int | None]]] = {}
    for k in range(16):
        p, out = 30.0 + k, []
        for _ in days:
            p *= 1 + rng.gauss(0.0003, 0.015)
            out.append(None if rng.random() < 0.01 else p)
        sym = f"S{k}"
        series[sym] = out
        cik = 100 + k
        segments[sym] = [(date(2000, 1, 1), None, cik if k != 15 else None)]
        sic = 6021 if k == 3 else 2000 + k  # S3 is a bank
        for year in range(2016, 2021):
            if k == 7 and year == 2018:
                continue  # missing a year: becomes too old during 2019-2020
            assets = rng.uniform(50, 150)
            ni = rng.choice([None, rng.uniform(-5, 15)]) if k == 9 else rng.uniform(-5, 15)
            equity = rng.uniform(-10, 60) if k % 5 == 0 else rng.uniform(10, 60)
            filed = date(year + 1, 2, 10 + k)
            shares = None if k in (10, 11) else rng.uniform(1, 3)  # S10/S11: balance sheet only
            rows += _annual(f"{k}-{year}", cik, sic, date(year, 12, 31), filed, ni,
                            rng.uniform(-3, 20), assets, equity, shares)  # fmt: skip
            if k in (10, 11):
                rows += [
                    (
                        f"{k}-{year}",
                        cik,
                        sic,
                        date(year, 12, 31),
                        filed,
                        "CommonStockSharesOutstanding",
                        0,
                        "shares",
                        rng.uniform(1, 3),
                    )
                ]
            if k == 4:  # an amendment restating net income two months later
                rows += [(f"{k}-{year}A", cik, sic, date(year, 12, 31), date(year + 1, 4, 20),
                          "NetIncomeLoss", 4, "USD", rng.uniform(0, 10))]  # fmt: skip
    prices = xs.Prices(days, series)
    actual = {s: [v * 10 if v else v for v in vals] for s, vals in series.items()}
    splits = {"S2": [(date(2019, 5, 1), 2.0)], "S6": [(date(2020, 3, 10), 3.0)]}
    known = {f"S{k}" for k in range(16)} - {"S8"}  # S8: splits never checked
    history = [(f"S{k}", date(2015, 1, 1), None) for k in range(16)]
    c = {
        "name": "qv",
        "universe": {"start": date(2018, 3, 1), "end": date(2021, 5, 31), "min_history_days": 253,
                     "exclude_sic": [6000, 6999]},
        "fundamentals": QV_SPEC,
        "signal": QV_SIGNAL,
        "selection": {"top_n": 4},
        "execution_lag_days": 1,
        "transaction_cost_bps": 10,
    }  # fmt: skip
    tags = fu.tag_lists(QV_SPEC)
    scorer = fu.QualityValue(c, days, fu.parse_rows(rows, tags), segments, actual, splits, known)
    independent = vx.Fundamentals(
        c,
        rows,
        segments,
        {s: {d: v for d, v in zip(days, vals, strict=True) if v} for s, vals in actual.items()},
        splits,
        known,
    )
    as_dicts = {
        s: {d: v for d, v in zip(days, vals, strict=True) if v} for s, vals in series.items()
    }
    a = xs.run(c, prices, history, excluded={"S5"}, scorer=scorer)["months"]
    b = vx.backtest(c, as_dicts, history, excluded={"S5"}, fundamentals=independent)
    result = vx.compare(a, b)
    assert result["match"], result
    stats = a[-1]["fundamentals"]
    assert stats["financial"] == 1 and 8 <= stats["scored"] <= 13
    assert all("S3" not in m["top"] for m in a)
    assert xs.fundamentals_coverage(stats) == stats["scored"] / (stats["candidates"] - 1)


def test_company_facts_become_annual_rows() -> None:
    from us_stock_research.collectors.sec_fundamentals import parse_wide
    from us_stock_research.research import fundamentals as fu

    spec = dict(QV_SPEC, shares=["WeightedAverageNumberOfDilutedSharesOutstanding",
                                 "EntityCommonStockSharesOutstanding",
                                 "CommonStockSharesOutstanding"])  # fmt: skip
    tags = fu.tag_lists(spec)

    def fact(tag, start, end, val, filed="2024-02-20", form="10-K", accn="A24", tax="us-gaap"):  # type: ignore[no-untyped-def]
        return {"start": start, "end": end, "val": val, "filed": filed, "form": form,
                "accn": accn, "fy": 2023, "fp": "FY"}  # fmt: skip

    payload = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {"units": {"USD": [
                    fact("", "2023-01-01", "2023-12-31", 10.0),
                    fact("", "2022-01-01", "2022-12-31", 8.0),  # comparative year
                    fact("", "2023-10-01", "2023-12-31", 3.0),  # a quarter inside the 10-K
                    fact("", "2023-01-01", "2023-09-30", 7.0, "2023-11-01", "10-Q", "Q3"),
                ]}},
                "Assets": {"units": {"USD": [fact("", None, "2023-12-31", 100.0),
                                             fact("", None, "2022-12-31", 90.0)]}},
                "CommonStockSharesOutstanding": {"units": {"shares": [
                    fact("", None, "2023-12-31", 5.0)]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    fact("", None, "2024-02-01", 4.0), fact("", None, "2024-02-01", 1.0),
                    fact("", None, "2023-10-20", 9.0, "2023-11-01", "10-Q", "Q3"),
                ]}},
            },
        }
    }  # fmt: skip
    wide = parse_wide(payload)
    assert len(wide) == 10 and wide[0][0] == "dei"
    facts = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[10]) for r in wide]
    rows = fu.normalize_facts(7, facts, tags, [(date(2010, 1, 1), 3571)])  # type: ignore[arg-type]
    got = {(r[5], r[6]): r[8] for r in rows}
    assert got == {
        ("NetIncomeLoss", 4): 10.0,
        ("Assets", 0): 100.0,
        ("CommonStockSharesOutstanding", 0): 5.0,
        ("EntityCommonStockSharesOutstanding", 0): 4.0,  # cover date, largest class value
    }
    assert {r[3] for r in rows} == {date(2023, 12, 31)} and {r[2] for r in rows} == {3571}
    reports = fu.parse_rows(rows, tags)[7]
    items = fu.resolve(reports, date(2024, 3, 1), tags, 550)
    assert items and items["shares"] == 4.0 and items["filed"] == date(2024, 2, 20)
    assert fu.sic_on([(date(2015, 1, 1), 100), (date(2020, 1, 1), 200)], date(2014, 1, 1)) == 100
    assert fu.sic_on([(date(2015, 1, 1), 100), (date(2020, 1, 1), 200)], date(2021, 1, 1)) == 200


def test_rehearsal_ledger_books_fills_dividends_and_refuses_stale_lists(tmp_path: Path) -> None:
    from us_stock_research.trading import rehearsal as rh

    assert rh.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 1)
    assert rh.next_trading_day(date(2026, 7, 2)) == date(2026, 7, 6)  # July 3 holiday, weekend
    ledger = rh.load_ledger(tmp_path / "none.yml", 10_000.0)
    first = {
        "study": "s", "signal_day": "2026-08-31",
        "orders": [
            {"side": "BUY", "symbol": "AAA", "shares": 50, "current_shares": 0.0},
            {"side": "BUY", "symbol": "BBB", "shares": 100, "current_shares": 0.0},
        ],
    }  # fmt: skip
    ledger, rep = rh.book(ledger, first, date(2026, 9, 1), {"AAA": 100.0, "BBB": 60.0}, {}, 10.0)
    # AAA: 5000 + 5 cost; BBB: 4995 cash left -> only 83 shares at 60.06 each
    assert ledger["positions"]["AAA"] == 50 and ledger["positions"]["BBB"] == 83
    assert any("cut" in f.get("note", "") for f in rep["fills"])
    assert 0 <= ledger["cash_usd"] < 60.06
    second = {
        "study": "s", "signal_day": "2026-09-30",
        "orders": [
            {"side": "BUY", "symbol": "BBB", "shares": 10, "current_shares": 83.0},
            {"side": "SELL", "symbol": "AAA", "shares": 50, "current_shares": 50.0},
        ],
    }  # fmt: skip
    divs = {"AAA": {date(2026, 9, 15): 1.0, date(2026, 8, 20): 9.0}}  # only the later one counts
    cash_before = ledger["cash_usd"]
    ledger2, rep2 = rh.book(ledger, second, date(2026, 10, 1), {"AAA": 110.0, "BBB": 60.0},
                            divs, 0.0)  # fmt: skip
    assert rep2["dividends_usd"] == 50.0 and "AAA" not in ledger2["positions"]
    assert ledger2["positions"]["BBB"] == 93
    assert abs(ledger2["cash_usd"] - (cash_before + 50 + 5500 - 600)) < 0.01
    assert ledger2["filled"] == ["2026-08-31", "2026-09-30"]
    stale = dict(second, signal_day="2026-10-30")  # still assumes 50 AAA
    for bad, why in ((second, "already booked"), (stale, "regenerate")):
        try:
            rh.book(ledger2, bad, date(2026, 11, 2), {"AAA": 1.0, "BBB": 60.0}, {}, 0.0)
        except ValueError as exc:
            assert why in str(exc)
        else:
            raise AssertionError(f"{bad['signal_day']} was booked")
    path = tmp_path / "ledger.yml"
    path.write_text(rh.dump_ledger(ledger2))
    assert rh.load_ledger(path, 0.0)["positions"] == {"BBB": 93.0}


def test_insider_buying_engine_matches_independent() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import cross_section as xs
    from us_stock_research.research import insider as ins
    from us_stock_research.research import verify_xsec as vx

    assert ins.is_insider("Director,TenPercentOwner") and not ins.is_insider("TenPercentOwner")
    buys = [(date(2020, 1, 2), 1, 2e6), (date(2020, 3, 1), 2, 1e6), (date(2020, 3, 1), 1, 1e6)]
    assert ins.score(buys, date(2020, 1, 1), 182) == 0.0
    assert ins.score(buys, date(2020, 3, 1), 182) == 2 + 4 / 5  # two buyers, $4m
    assert ins.score(buys, date(2020, 7, 3), 182) == 2 + 2 / 3  # Jan 2 left the window
    rng = random.Random(5)
    days = trading_days(date(2017, 1, 3), date(2021, 6, 30))
    series: dict[str, list[float | None]] = {}
    raw: list[tuple] = []  # type: ignore[type-arg]
    segments: dict[str, list[tuple[date, date | None, int | None]]] = {}
    roles = ["Director", "Officer", "Director,Officer", "TenPercentOwner", "Other"]
    for k in range(20):
        p, out = 40.0 + k, []
        for _ in days:
            p *= 1 + rng.gauss(0.0003, 0.015)
            out.append(None if rng.random() < 0.01 else p)
        series[f"S{k}"] = out
        segments[f"S{k}"] = [(date(2000, 1, 1), None, 500 + k if k != 19 else None)]
        for _ in range(rng.randint(0, 25)):
            filed = days[rng.randrange(len(days))]
            code = rng.choice(["P", "P", "P", "S"])
            raw.append((500 + k, filed, rng.randint(1, 6), rng.uniform(100, 50_000),
                        rng.uniform(10, 90), rng.choice(roles), code, "A"))  # fmt: skip
    prices = xs.Prices(days, series)
    history = [(f"S{k}", date(2015, 1, 1), None) for k in range(20)]
    c = {
        "name": "ins",
        "universe": {"start": date(2018, 3, 1), "end": date(2021, 5, 31), "min_history_days": 253},
        "insider": {"window_days": 182},
        "signal": {"name": "insider_buying"},
        "selection": {"top_n": 4, "min_score": 0.000001},
        "execution_lag_days": 1,
        "transaction_cost_bps": 10,
    }
    engine_buys: dict[int, list[tuple[date, int, float]]] = {}
    for issuer, filed, owner, shares, price, rel, code, acq in sorted(raw, key=lambda r: r[:3]):
        if code == "P" and acq == "A" and ins.is_insider(rel):
            engine_buys.setdefault(issuer, []).append((filed, owner, shares * price))
    for v in engine_buys.values():
        v.sort()
    scorer = ins.InsiderBuying(c, days, engine_buys, segments)
    as_dicts = {
        s: {d: v for d, v in zip(days, vals, strict=True) if v} for s, vals in series.items()
    }
    a = xs.run(c, prices, history, excluded={"S5"}, scorer=scorer)["months"]
    b = vx.backtest(
        c, as_dicts, history, excluded={"S5"}, fundamentals=vx.InsiderSignal(c, raw, segments)
    )
    result = vx.compare(a, b)
    assert result["match"], result
    assert any(len(m["top"]) < 4 for m in a)  # months with fewer buyers than top_n hold fewer
    assert all(m["eligible"] == m["insider"]["candidates"] for m in a)  # benchmark = all members


def test_leveraged_trend_engine_matches_independent() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.research import verify_lt as vl

    days = trading_days(date(2000, 1, 3), date(2004, 12, 31))
    rng = random.Random(9)
    prices, p = [], 100.0
    for i in range(len(days)):
        p *= 1 + rng.gauss(0.0006 if (i // 250) % 2 == 0 else -0.0008, 0.012)
        prices.append(p)
    yields = lt.forward_fill(days, {d: 1.0 + (i % 7) * 0.3 for i, d in enumerate(days[5::9])})
    c = {
        "name": "t",
        "data": {"start": date(2001, 1, 1), "end": date(2004, 6, 30)},
        "rule": {"sma_days": 200, "leverage": 2.0, "switch_cost_bps": 10},
        "inference": {"resamples": 200, "block_size_months": 6, "confidence_level": 0.95,
                      "random_seed": 1},
        "risk": {"max_worst_12m_loss": 0.5},
    }  # fmt: skip
    res = lt.run(c, days, prices, yields)
    assert vl.compare(res, vl.backtest(c, days, prices, yields))["match"]
    assert 0 < res["time_in_market"] < 1 and res["switches"] > 0
    assert res["months"][0] == "2001-01" and res["months"][-1] == "2004-06"
    # leverage 1 with no switching cost and always on equals the asset itself
    sim = lt.simulate(days, prices, yields, sma_days=1, leverage=1.0, switch_cost_bps=0,
                      always_on=True)  # fmt: skip
    assert all(abs(r - a) < 1e-15 for _, r, a, _ in sim)
    # cash days earn the previous day's yield / 252; the first day of a new position pays costs
    sim2 = lt.simulate(days, prices, yields, sma_days=200, leverage=2.0, switch_cost_bps=10)
    k = next(i for i in range(1, len(sim2)) if not sim2[i][3] and not sim2[i - 1][3])
    idx = days.index(sim2[k][0])
    assert abs(sim2[k][1] - yields[idx - 1] / 100 / 252) < 1e-15  # type: ignore[operator]
    # the model validation against a perfect 2x ETF built from the same model is exact
    etf, v = {}, 100.0
    on = lt.simulate(days, prices, yields, sma_days=1, leverage=2.0, switch_cost_bps=0,
                     always_on=True)  # fmt: skip
    etf[on[0][0]] = v
    for d, r, _, _ in on[1:]:
        v *= 1 + r
        etf[d] = v
    check = lt.validate_leverage_model(days, prices, yields, etf, 2.0)
    assert abs(check["annualized_diff"]) < 1e-12 and check["tracking_error"] < 1e-12


def test_leveraged_trend_daily_intent() -> None:
    from us_stock_research.calendar import trading_days
    from us_stock_research.trading.lt_intent import generate, trend_on
    from us_stock_research.trading.order_intent import Holdings

    assert trend_on([1.0] * 199 + [2.0], 200) and not trend_on([2.0] * 199 + [1.0], 200)
    days = trading_days(date(2025, 1, 2), date(2026, 3, 31))

    def series(f):  # type: ignore[no-untyped-def]
        return [DailyBar(d, f(i), f(i), f(i), f(i), f(i), 1000) for i, d in enumerate(days)]

    up = {"SPY": series(lambda i: 100 + i * 0.1), "SSO": series(lambda i: 50 + i * 0.1),
          "BIL": series(lambda i: 91.0)}  # fmt: skip
    c = {"name": "lt", "status": "registered", "data": {"signal_and_asset": "SPY"},
         "rule": {"sma_days": 200}}  # fmt: skip
    cash = Holdings(cash_usd=100_000.0, peak_nav_usd=100_000.0)
    it = generate(c, up, cash, days[-1], risk_on="SSO", risk_off="BIL")
    assert it["signal"]["trend_on"] and it["mode"] == "rehearsal" and not it["reduce_only"]
    assert [(o["side"], o["symbol"]) for o in it["orders"]] == [("BUY", "SSO")]
    held = Holdings(cash_usd=0.0, positions={"SSO": 1000.0}, peak_nav_usd=85_000.0)
    down = dict(up, SPY=series(lambda i: 200 - i * 0.1))
    it2 = generate(c, down, held, days[-1], risk_on="SSO", risk_off="BIL")
    assert not it2["signal"]["trend_on"]
    assert [(o["side"], o["symbol"]) for o in it2["orders"]] == [("SELL", "SSO"), ("BUY", "BIL")]
    # stale data: reduce-only, so the switch is a sale only
    stale = dict(down, BIL=series(lambda i: 91.0)[:-3])
    it3 = generate(c, stale, held, days[-1], risk_on="SSO", risk_off="BIL")
    assert it3["reduce_only"] and [o["side"] for o in it3["orders"]] == ["SELL"]
    # 15% below the peak: the breaker allows the sale but not the T-bill purchase
    deep = Holdings(cash_usd=0.0, positions={"SSO": 1000.0}, peak_nav_usd=1e5)
    it4 = generate(c, down, deep, days[-1], risk_on="SSO", risk_off="BIL")
    assert it4["reduce_only"] and "circuit breaker" in it4["reduce_only_reasons"][0]
    # already on target: nothing to do
    on_target = Holdings(cash_usd=10.0, positions={"SSO": 1000.0}, peak_nav_usd=1e5)
    assert generate(c, up, on_target, days[-1], risk_on="SSO", risk_off="BIL")["orders"] == []


def test_long_history_total_return_and_dividend_yield_table() -> None:
    from us_stock_research.collectors.multpl import parse_table
    from us_stock_research.research import leveraged_trend as lt

    page = (
        "<table><tr><th>Date</th><th>Value</th></tr>"
        '<tr class="odd"><td>Oct 1, 2026</td><td>\n<abbr title="Estimate">&#x2020;</abbr>\n'
        "1.06%</td></tr>"
        "<tr><td>Jun 30, 2026</td><td>\n1.10%\n</td></tr>"
        "<tr><td>Dec 31, 1954</td><td>4.39%</td></tr></table>"
    )
    assert parse_table(page) == [(date(1954, 12, 31), 4.39), (date(2026, 6, 30), 1.10)]
    days = [date(1955, 1, 3), date(1955, 1, 4), date(1955, 2, 1), date(1955, 2, 2)]
    closes = [100.0, 101.0, 101.0, 99.99]
    dy = {date(1954, 12, 31): 2.52, date(1955, 1, 31): 5.04}
    tr = lt.total_return_index(days, closes, dy)
    assert abs(tr[1] - (1.01 + 0.0001)) < 1e-12  # December yield in January
    assert abs(tr[2] / tr[1] - (1 + 0.0002)) < 1e-12  # January's month-end yield from Feb 1
    assert abs(tr[3] / tr[2] - (99.99 / 101 + 0.0002)) < 1e-12
    spy = dict(zip(days, [x * 3 for x in tr], strict=True))
    many = [date(1994, 1, 3) + __import__("datetime").timedelta(days=i) for i in range(400)]
    idx = [1.0 + i / 1000 for i in range(400)]
    check = lt.validate_total_return(many, idx, dict(zip(many, idx, strict=True)),
                                     date(1994, 1, 1), date(2025, 12, 31))  # fmt: skip
    assert abs(check["annualized_diff"]) < 1e-12 and check["tracking_error"] < 1e-12
    assert spy[days[0]] == 3.0
    hist = {"data": {"total_return": "price_plus_monthly_dividend_yield"},
            "risk": {"max_worst_12m_loss": 0.5}}  # fmt: skip
    summary = {"excess_vs_benchmark": {"interval": [0.001, 0.01]},
               "strategy": {"worst_rolling_12m_return": -0.3}}  # fmt: skip
    assert lt.evaluate(hist, summary, 0.99, {"annualized_diff": 0.004}) == []
    assert "全收益" in lt.evaluate(hist, summary, 0.99, {"annualized_diff": 0.006})[0]


def test_etf_dividend_yield_points() -> None:
    from us_stock_research.research import leveraged_trend as lt

    step = __import__("datetime").timedelta
    closes = {date(2000, 1, 3) + step(days=i): 100.0 for i in range(500)}
    divs = {date(2000, 6, 15): 0.25, date(2000, 12, 15): 0.25, date(2001, 3, 15): 0.30}
    y = lt.etf_dividend_yield(divs, closes, 0.5)
    assert y[date(1900, 1, 1)] == 0.5
    assert date(2000, 12, 31) not in y  # less than a year of ETF history
    assert abs(y[date(2001, 1, 31)] - 0.5) < 1e-12  # 0.25 + 0.25 over 365 days / 100
    assert abs(y[date(2001, 3, 31)] - 0.8) < 1e-12
    assert abs(y[date(2001, 5, 16)] - 0.8) < 1e-12  # last day in data is a month-end point
    assert all(d.month != (d + step(days=1)).month or d == max(closes)
               for d in y if d.year > 1900)  # fmt: skip
    tr = {"data": {"total_return": "price_plus_etf_dividend_yield", "validate_against": "QQQ"},
          "risk": {"max_worst_12m_loss": 0.5}}  # fmt: skip
    summary = {"excess_vs_benchmark": {"interval": [0.001, 0.01]},
               "strategy": {"worst_rolling_12m_return": -0.3}}  # fmt: skip
    assert lt.total_return(tr)
    assert "QQQ" in lt.evaluate(tr, summary, 0.99, {"annualized_diff": 0.006})[0]


def test_volatility_target_engine_matches_independent() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.research import verify_lt as vl

    days = trading_days(date(2000, 1, 3), date(2005, 12, 30))
    rng = random.Random(21)
    prices, p = [], 100.0
    for i in range(len(days)):
        calm = (i // 300) % 2 == 0
        p *= 1 + rng.gauss(0.0007 if calm else -0.0005, 0.007 if calm else 0.02)
        prices.append(p)
    yields = lt.forward_fill(days, {d: 2.0 + (i % 5) * 0.2 for i, d in enumerate(days[3::11])})
    c = {
        "name": "vt",
        "data": {"start": date(2001, 1, 1), "end": date(2005, 6, 30)},
        "rule": {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25,
                 "max_leverage": 2.0, "rebalance_band": 0.25, "trading_cost_bps": 10},
        "inference": {"resamples": 200, "block_size_months": 6, "confidence_level": 0.95,
                      "random_seed": 1},
        "risk": {"max_worst_12m_loss": 0.5},
    }  # fmt: skip
    res = lt.run(c, days, prices, yields)
    check = vl.compare(res, vl.backtest(c, days, prices, yields))
    assert check["match"], check
    sim = lt.simulate_vol_target(days, prices, yields, sma_days=200, vol_window=20,
                                 vol_target=0.25, max_leverage=2.0, band=0.25,
                                 cost_bps=10)  # fmt: skip
    exposures = {row[3] for row in sim}
    assert 0.0 in exposures and 2.0 in exposures and any(0 < e < 2 for e in exposures)
    assert 0 < res["average_exposure"] < 2
    sub_ = lt.run(c, days, prices, yields, start=date(2002, 1, 1), end=date(2002, 12, 31))
    assert sub_["months"][0] == "2002-01" and len(sub_["months"]) == 12


def test_volatility_target_daily_intent() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.trading import lt_intent as li
    from us_stock_research.trading.order_intent import Holdings

    assert li.exposure_weights(0.4, "SPY", "SSO", "BIL") == {"SPY": 0.4, "BIL": 0.6}
    assert li.exposure_weights(1.5, "SPY", "SSO", "BIL") == {"SSO": 0.5, "SPY": 0.5}
    assert li.exposure_weights(2.0, "SPY", "SSO", "BIL") == {"SSO": 1.0}
    assert li.exposure_weights(0.0, "SPY", "SSO", "BIL") == {"BIL": 1.0}
    h = Holdings(cash_usd=0.0, positions={"SSO": 10.0, "SPY": 10.0})
    assert abs(li.current_exposure(h, {"SSO": 50.0, "SPY": 50.0}, "SPY", "SSO") - 1.5) < 1e-12
    days = trading_days(date(2024, 1, 2), date(2026, 3, 31))
    rng = random.Random(4)
    closes, p = [], 100.0
    for _ in days:
        p *= 1 + rng.gauss(0.0006, 0.01)
        closes.append(p)
    rule = {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25, "max_leverage": 2.0,
            "rebalance_band": 0.25, "name": "vt"}  # fmt: skip
    # same target as the research engine on the day before it is applied
    sim = lt.simulate_vol_target(days, closes, [1.0] * len(days), sma_days=200, vol_window=20,
                                 vol_target=0.25, max_leverage=2.0, band=0.0,
                                 cost_bps=0)  # fmt: skip
    by_day = {row[0]: row[3] for row in sim}
    for i in range(250, len(days) - 2, 37):
        assert abs(li.vol_target_exposure(closes[: i + 1], rule) - by_day[days[i + 2]]) < 1e-12

    def bars(xs):  # type: ignore[no-untyped-def]
        return [DailyBar(d, x, x, x, x, x, 1) for d, x in zip(days, xs, strict=True)]

    data = {"SPY": bars(closes), "SSO": bars([50.0] * len(days)), "BIL": bars([91.0] * len(days))}
    c = {"name": "vt", "status": "promoted", "human_review": {"approved": True},
         "data": {"signal_and_asset": "^GSPC"}, "rule": rule}  # fmt: skip
    fresh = Holdings(cash_usd=100_000.0, peak_nav_usd=100_000.0)
    it = li.generate(c, data, fresh, days[-1], risk_on="SSO", risk_off="BIL", signal="SPY")
    assert it["mode"] == "live-candidate" and it["trading_enabled"] is False
    aim = it["signal"]["exposure"]["target"]
    assert abs(sum(it["target_weights"].values()) - 1) < 1e-6 and it["orders"]
    # holding exactly the target mix: inside the band, no orders
    w = li.exposure_weights(aim, "SPY", "SSO", "BIL")
    px = {"SPY": closes[-1], "SSO": 50.0, "BIL": 91.0}
    held = Holdings(cash_usd=0.0, positions={s: v * 100_000 / px[s] for s, v in w.items()},
                    peak_nav_usd=100_000.0)  # fmt: skip
    assert li.generate(c, data, held, days[-1], risk_on="SSO", risk_off="BIL",
                       signal="SPY")["orders"] == []  # fmt: skip


def test_alpaca_paper_submission_is_paper_only_idempotent_and_reconciled() -> None:
    from datetime import UTC, datetime

    import httpx

    from us_stock_research.trading import alpaca_paper as ap

    posted: list[dict] = []  # type: ignore[type-arg]
    known: dict[str, dict] = {}  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "paper-api.alpaca.markets"
        assert request.headers["APCA-API-KEY-ID"] == "k"
        if request.url.path == "/v2/orders:by_client_order_id":
            cid = request.url.params["client_order_id"]
            return httpx.Response(200, json=known[cid]) if cid in known else httpx.Response(404)
        if request.url.path == "/v2/orders" and request.method == "POST":
            body = __import__("json").loads(request.content)
            assert body["time_in_force"] == "cls" and body["type"] == "market"
            posted.append(body)
            known[body["client_order_id"]] = {"status": "accepted"}
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(request.url)

    client = ap.PaperClient("k", "s", transport=httpx.MockTransport(handler))
    intent = {"study": "vt", "signal_day": "2026-10-05", "mode": "live-candidate",
              "trading_enabled": False,
              "orders": [{"side": "BUY", "symbol": "SSO", "shares": 10},
                         {"side": "SELL", "symbol": "BIL", "shares": 5}]}  # fmt: skip
    holdings = {"positions": {"BIL": 5.0}}
    evening = datetime(2026, 10, 6, 0, 30, tzinfo=UTC)  # 20:30 New York (EDT)
    sent, expected = ap.submit(client, intent, holdings, now_utc=evening)
    assert [b["side"] for b in posted] == ["sell", "buy"] and expected == {"SSO": 10.0}
    again, _ = ap.submit(client, intent, holdings, now_utc=evening)
    assert len(posted) == 2 and not any(s["resubmitted"] for s in again)  # idempotent
    for bad, why in (
        (dict(intent, mode="rehearsal"), "approved"),
        (dict(intent, trading_enabled=True), "live"),
    ):
        try:
            ap.submit(client, bad, holdings, now_utc=evening)
        except ValueError as exc:
            assert why in str(exc)
        else:
            raise AssertionError("submitted")
    after_close = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)  # 16:00 New York
    assert not ap.submission_window_ok(after_close) and ap.submission_window_ok(evening)
    winter = datetime(2026, 12, 7, 23, 30, tzinfo=UTC)  # 18:30 EST: still rejected
    assert not ap.submission_window_ok(winter)
    assert ap.reconcile({"SSO": 10.0}, {"SSO": 10.0}) == []
    assert ap.reconcile({"SSO": 10.0}, {"SSO": 7.0, "BIL": 1.0}) == [
        "BIL: expected 0, paper account holds 1",
        "SSO: expected 10, paper account holds 7",
    ]
    h = ap.holdings_from_account({"equity": "105000", "cash": "12.5"}, {"SSO": 10.0},
                                 {"peak_nav_usd": 110000.0})  # fmt: skip
    assert h["peak_nav_usd"] == 110000.0 and h["cash_usd"] == 12.5
    conf = Path("configs/paper_broker.yml")
    if ap.load_config(conf)["enabled"]:  # only on by a dated user decision
        assert "用户 2026-10-07 决定提前打开" in conf.read_text()


def test_daily_status_tracks_model_and_flags_attention(tmp_path: Path) -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.trading import status as st
    from us_stock_research.trading.order_intent import Holdings

    days = trading_days(date(2025, 1, 2), date(2026, 9, 30))
    rng = random.Random(8)
    closes, p = [], 100.0
    for _ in days:
        p *= 1 + rng.gauss(0.0005, 0.009)
        closes.append(p)
    rule = {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25, "max_leverage": 2.0,
            "rebalance_band": 0.25, "trading_cost_bps": 10}  # fmt: skip
    yields = {d: 4.0 for d in days}
    nav_file = tmp_path / "x.nav.csv"
    start = days[-60]
    model = st.model_growth(rule, days, closes, lt.forward_fill(days, yields), start)
    st.append_nav(nav_file, start, 100_000.0, 1.0)
    history = st.append_nav(nav_file, days[-1], 100_000.0 * model, 1.2)
    assert [d for d, _ in history] == [start, days[-1]]
    assert st.append_nav(nav_file, days[-1], 100_000.0 * model, 1.2) == history  # idempotent
    h = Holdings(cash_usd=0.0, positions={"SSO": 100.0}, peak_nav_usd=100_000.0)
    s = st.build({"rule": rule}, {"stage2": {"max_cumulative_tracking_gap": 0.03}},
                 list(zip(days, closes, strict=True)), {"SSO": 50.0, "SPY": closes[-1]}, h,
                 history, yields, one_x="SPY", leveraged="SSO", breaker=0.40,
                 data_issues=[])  # fmt: skip
    assert abs(s["tracking"]["gap"]) < 1e-9 and not s["attention"]
    off = history[:-1] + [(days[-1], 100_000.0 * model * 0.9)]
    s2 = st.build({"rule": rule}, {}, list(zip(days, closes, strict=True)),
                  {"SSO": 50.0, "SPY": closes[-1]},
                  Holdings(cash_usd=0.0, positions={"SSO": 1.0}, peak_nav_usd=200_000.0),
                  off, yields, one_x="SPY", leveraged="SSO", breaker=0.40,
                  data_issues=["BIL stale"])  # fmt: skip
    joined = " ".join(s2["attention"])
    assert "BIL stale" in joined and "偏差" in joined and "熔断" in joined
    assert "交易系统状态" in st.render(s2, h, "演练账本")


def test_dashboard_page_renders_series_and_escapes_data(tmp_path: Path) -> None:
    from us_stock_research.calendar import trading_days
    from us_stock_research.trading import status as st
    from us_stock_research.trading.dashboard import render_html

    days = trading_days(date(2025, 1, 2), date(2026, 9, 30))
    closes = [100 + i * 0.1 for i in range(len(days))]
    rule = {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25, "max_leverage": 2.0,
            "rebalance_band": 0.25, "trading_cost_bps": 10}  # fmt: skip
    nav = [(days[-3], 100_000.0, 2.0), (days[-2], 100_500.0, 2.0), (days[-1], 101_000.0, 2.0)]
    series = st.chart_series(rule, days, closes, [4.0] * len(days), nav)
    assert len(series["curves"]) == 3 and series["curves"][0]["portfolio"] == 100.0
    assert series["curves"][0]["model"] == 100.0 and len(series["signal_series"]) == 260
    assert series["signal_series"][-1]["sma"] is not None
    payload = {"day": "2026-09-30", "source": "演练账本", "generated_at": "2026-10-01T00:00",
               "signal": {"close": 1.0, "sma": 1.0, "distance_to_sma": 0.0, "target_exposure": 2.0,
                          "current_exposure": 2.0, "trend_on": True},
               "portfolio": {"nav": 1.0, "peak": 1.0, "drawdown": 0.0, "breaker": 0.4},
               "tracking": None, "attention": ["</script><b>x"], **series}  # fmt: skip
    page = render_html(payload)
    assert "__DATA__" not in page and "</script><b>" not in page and "<\\/script>" in page
    folder = tmp_path / "orders"
    folder.mkdir()
    (folder / "2026-09-30.json").write_text(
        '{"signal_day": "2026-09-30", "mode": "live-candidate", "orders": [{"side": "BUY", '
        '"symbol": "SSO", "shares": 3, "ref_price": 70.0, "est_value_usd": 210.0}]}'
    )
    assert st.recent_orders(folder)[0]["symbol"] == "SSO"


def test_sleeve_mix_matches_independent_and_costs() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import sleeve_mix as sm
    from us_stock_research.research import verify_mix as vm

    days = trading_days(date(2003, 1, 2), date(2008, 12, 31))
    rng = random.Random(7)
    inputs = {}
    for sym, drift in (("TLT", 0.0002), ("IEF", 0.0001), ("GLD", 0.0003)):
        p = [100.0]
        for _ in days[1:]:
            p.append(p[-1] * (1 + drift + rng.gauss(0, 0.01)))
        inputs[sym] = (days, p, [4.0] * len(days))
    start, end = date(2004, 1, 1), date(2008, 12, 31)
    series = sm.defensive_series(inputs, 200, 10.0)
    defensive = sm.defensive_monthly(series, start, end)
    vt = {m: rng.gauss(0.01, 0.05) for m in defensive}
    mix = sm.combine(vt, defensive, 0.5, 10.0)
    ind = vm.mix_monthly(vt_monthly=vt, inputs=inputs, sma=200, bps=10.0, weight=0.5,
                         start=start, end=end)  # fmt: skip
    assert set(ind) == set(mix) and len(mix) == 60
    assert max(abs(ind[m] - mix[m]) for m in mix) < 1e-12
    # no drift -> no rebalance cost; buy-and-hold variant never pays switch costs
    flat = sm.combine({"2005-01": 0.02}, {"2005-01": 0.02}, 0.5, 10.0)
    assert abs(flat["2005-01"] - 0.02) < 1e-15
    held = sm.defensive_series(inputs, 200, 10.0, use_trend=False)["GLD"]
    gld = inputs["GLD"][1]
    assert abs(held[-1][1] - (gld[-1] / gld[-2] - 1)) < 1e-15
    test = sm.paired_sharpe_bootstrap(list(mix.values()), list(mix.values()), 200, 6, 0.95, 1)
    assert test["interval"] == [0.0, 0.0] and test["sharpe_difference"] == 0.0


def test_update_retries_throttling_within_budget(tmp_path: Path) -> None:
    import httpx
    from conftest import synthetic_bars

    from us_stock_research.collectors.update import run_update
    from us_stock_research.storage import CsvStore

    store = CsvStore(tmp_path)
    bars = synthetic_bars(date(2026, 9, 1), 20, 0.001, 0.01, 0.0)
    for sym in ("OK", "BAD", "SLOW"):
        store.write_bars(sym, bars)
        store.write_dividends(sym, {})
    calls: dict[str, int] = {}
    req = httpx.Request("GET", "https://example.test")

    def fetch(symbol: str, a: date, b: date):  # type: ignore[no-untyped-def]
        calls[symbol] = calls.get(symbol, 0) + 1
        code = 429 if symbol != "BAD" else 404
        if symbol == "OK" and calls[symbol] >= 3:
            return [b for b in bars if b.day >= a], {}
        raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))

    waits: list[float] = []
    out = run_update(["OK", "BAD", "SLOW"], store, fetch, date(2026, 10, 2), date(2000, 1, 1),
                     execute=False, pause=0.0, sleep=waits.append, retries=3, backoff=30.0,
                     retry_budget=200.0)  # fmt: skip
    assert calls == {"OK": 3, "BAD": 1, "SLOW": 2}  # 404 is not retried; budget stops SLOW
    assert [w for w in waits if w] == [30.0, 90.0, 30.0]
    assert out["retried"] == {"OK": 2, "SLOW": 1} and "SLOW" in out["failed"]
    assert out["retry_wait_seconds"] == 150.0


def test_mix_intent_model_and_orders() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.trading import mix_intent as mi
    from us_stock_research.trading.order_intent import Holdings

    days = trading_days(date(2025, 1, 2), date(2026, 3, 31))
    rng = random.Random(11)

    def walk(drift: float, vol: float) -> list[float]:
        out, p = [], 100.0
        for _ in days:
            p *= 1 + rng.gauss(drift, vol)
            out.append(p)
        return out

    series = {"SPY": walk(0.0008, 0.006), "SSO": walk(0.0016, 0.012),
              "BIL": walk(0.00015, 0.0), "TLT": walk(-0.001, 0.008),
              "IEF": walk(0.0003, 0.003), "GLD": walk(0.001, 0.009)}  # fmt: skip
    bars = {s: [DailyBar(d, x, x, x, x, x, 1) for d, x in zip(days, xs, strict=True)]
            for s, xs in series.items()}  # fmt: skip
    vt_rule = {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25, "max_leverage": 2.0,
               "rebalance_band": 0.25}  # fmt: skip
    mix = {"name": "mix", "status": "promoted", "human_review": {"approved": True},
           "data": {"defensive_assets": ["TLT", "IEF", "GLD"]},
           "rule": {"sma_days": 200, "aggressive_weight": 0.5}}  # fmt: skip
    vt = {"rule": vt_rule}
    start = date(2025, 12, 1)
    adj = {s: dict(zip(days, xs, strict=True)) for s, xs in series.items()}
    path = mi.model_path(adj, days, start, vt_rule, mix["rule"], ["TLT", "IEF", "GLD"])
    assert path[0]["events"] == ["start"] and path[0]["day"] == start
    for row in path:
        assert abs(sum(row["weights"].values()) - 1) < 1e-5
        if mi.month_end(row["day"]):
            assert "month_end" in row["events"] and abs(row["aggressive_share"] - 0.5) < 1e-12
    assert not path[0]["slots_on"]["TLT"] and path[0]["slots_on"]["GLD"]  # TLT falling
    # fresh account: orders to the model weights, about half in the aggressive sleeve
    fresh = Holdings(cash_usd=100_000.0, peak_nav_usd=100_000.0)
    it = mi.generate(mix, vt, bars, fresh, start, start)
    assert it["mode"] == "live-candidate" and it["orders"] and "start" in it["signal"]["triggers"]
    held = {o["symbol"]: float(o["shares"]) for o in it["orders"] if o["side"] == "BUY"}
    spent = sum(o["est_value_usd"] for o in it["orders"])
    filled = Holdings(cash_usd=100_000.0 - spent, positions=held, peak_nav_usd=100_000.0)
    # next quiet day: no model event and no drift -> no orders
    quiet = next(r for r in path[1:] if not r["events"])
    it2 = mi.generate(mix, vt, bars, filled, quiet["day"], start)
    assert it2["signal"]["max_drift"] < 0.05 and it2["orders"] == []
    assert "订单 0 笔" in mi.summary_line(it2)
    # month end: rebalance orders appear
    me = next(r for r in path if "month_end" in r["events"] and r["day"] > start)
    it3 = mi.generate(mix, vt, bars, filled, me["day"], start)
    assert "month_end" in it3["signal"]["triggers"]


def test_verify_intent_independent_replay_matches() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.trading import mix_intent as mi
    from us_stock_research.trading import verify_intent as vi
    from us_stock_research.trading.order_intent import Holdings

    days = trading_days(date(2025, 1, 2), date(2026, 6, 30))
    rng = random.Random(23)
    series = {}
    for sym, drift, vol in (
        ("SPY", 0.0005, 0.012),
        ("SSO", 0.001, 0.024),
        ("BIL", 0.00015, 0.0),
        ("TLT", 0.0, 0.009),
        ("IEF", 0.0001, 0.004),
        ("GLD", 0.0004, 0.01),
    ):
        p, xs = 100.0, []
        for _ in days:
            p *= 1 + rng.gauss(drift, vol)
            xs.append(p)
        series[sym] = xs  # fmt: skip
    adj = {s: dict(zip(days, xs, strict=True)) for s, xs in series.items()}
    vt_rule = {"sma_days": 200, "vol_window_days": 20, "vol_target": 0.25, "max_leverage": 2.0,
               "rebalance_band": 0.25}  # fmt: skip
    slots = ["TLT", "IEF", "GLD"]
    start = date(2025, 11, 3)
    path = mi.model_path(adj, days, start, vt_rule, {"sma_days": 200, "aggressive_weight": 0.5},
                         slots)  # fmt: skip
    events = sum(bool(r["events"]) for r in path)
    assert events > 5  # the sample exercises switches and month ends
    for row in path[::7] + [path[-1]]:
        rec = vi.replay(adj, start, row["day"], sma_days=200, vol_days=20, vol_target=0.25,
                        cap=2.0, band=0.25, aggressive_weight=0.5, slots=slots)  # fmt: skip
        keys = set(rec["weights"]) | set(row["weights"])
        assert max(abs(rec["weights"].get(k, 0) - row["weights"].get(k, 0)) for k in keys) < 1e-5
    bars = {s: [DailyBar(d, x, x, x, x, x, 1) for d, x in zip(days, xs, strict=True)]
            for s, xs in series.items()}  # fmt: skip
    mix = {
        "name": "mix",
        "status": "promoted",
        "human_review": {"approved": True},
        "data": {"defensive_assets": slots},
        "rule": {"sma_days": 200, "aggressive_weight": 0.5},
    }
    holdings = Holdings(cash_usd=100_000.0, peak_nav_usd=100_000.0)
    it = mi.generate(mix, {"rule": vt_rule}, bars, holdings, start, start)
    rec = vi.replay(adj, start, start, sma_days=200, vol_days=20, vol_target=0.25, cap=2.0,
                    band=0.25, aggressive_weight=0.5, slots=slots)  # fmt: skip
    prices = {s: xs[days.index(start)] for s, xs in series.items()}
    exp = vi.expected_orders(rec["weights"], {}, 100_000.0, prices)
    assert vi.check(it, rec, exp) == []
    bad = dict(it, orders=[dict(o, shares=o["shares"] + 1) for o in it["orders"]])
    assert vi.check(bad, rec, exp)
    assert "结论：一致" in vi.render(rec, it, [])
    # live cash buffer: buys sized on 99% of NAV, stated in the list and re-applied by the check
    small = Holdings(cash_usd=10_000.0, peak_nav_usd=10_000.0)
    itb = mi.generate(mix, {"rule": vt_rule}, bars, small, start, start, cash_buffer=0.01)
    spent = sum(o["est_value_usd"] for o in itb["orders"] if o["side"] == "BUY")
    assert itb["cash_buffer"] == 0.01 and spent <= 9_900.0
    assert itb["target_weights"] == it["target_weights"]  # targets unchanged, sizing only
    expb = vi.expected_orders(rec["weights"], {}, 10_000.0, prices, 0.01)
    assert vi.check(itb, rec, expb) == []
    assert vi.check(itb, rec, vi.expected_orders(rec["weights"], {}, 10_000.0, prices)) or (
        expb == vi.expected_orders(rec["weights"], {}, 10_000.0, prices)
    )
    assert vi.check(dict(itb, cash_buffer=0.2), rec, expb)  # an implausible buffer is flagged
    with pytest.raises(ValueError):
        mi.generate(mix, {"rule": vt_rule}, bars, small, start, start, cash_buffer=0.2)


def test_stage1_review_log_and_progress(tmp_path: Path) -> None:
    from datetime import datetime

    import pytest

    from us_stock_research.trading import reviews as rv

    assert rv.add_months(date(2026, 10, 2), 3) == date(2027, 1, 2)
    assert rv.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    folder, log = tmp_path / "orders/mix", tmp_path / "reviews/mix.jsonl"
    folder.mkdir(parents=True)
    for d in ("2026-10-02", "2026-10-30"):
        (folder / f"{d}.json").write_text("{}")
        (folder / f"{d}.md").write_text("- [ ] 复核人 / 日期：\n")
    (folder / "2026-10-30.md").write_text("- [ ] 复核人 / 日期：\n**结论：一致**\n")
    with pytest.raises(ValueError):
        rv.record(folder, log, date(2026, 10, 2), "用户")  # no independent check yet
    with pytest.raises(ValueError):
        rv.record(folder, log, date(2026, 10, 9), "用户")  # no such list
    rv.record(folder, log, date(2026, 10, 30), "用户", now=datetime(2026, 10, 31))
    assert "[x] 复核人 / 日期：用户 / 2026-10-31" in (folder / "2026-10-30.md").read_text()
    p = rv.progress(folder, log, 3, date(2027, 1, 5))
    assert p["lists"] == 2 and p["reviewed"] == 1 and p["pending"] == ["2026-10-02"]
    assert p["gate_date"] == "2027-01-02" and not p["met"]
    (folder / "2026-10-02.md").write_text("**结论：一致**\n")
    rv.record(folder, log, date(2026, 10, 2), "用户")
    assert rv.progress(folder, log, 3, date(2027, 1, 5))["met"]
    assert not rv.progress(folder, log, 3, date(2026, 12, 1))["met"]
    (folder / "2026-11-02.json.rejected").write_text("{}")
    assert not rv.progress(folder, log, 3, date(2027, 1, 5))["met"]


def test_replica_index_trades_next_close_like_live() -> None:
    from us_stock_research.trading import mix_intent as mi

    d = [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    adj = {"SSO": dict(zip(d, [70.0, 72.0, 73.44, 72.0], strict=True)),
           "BIL": dict(zip(d, [91.0, 91.0, 91.0, 91.0], strict=True))}  # fmt: skip
    w = {"SSO": 0.5, "BIL": 0.5}
    path = [{"day": d[0], "weights": w, "events": ["start"]},
            {"day": d[1], "weights": w, "events": []},
            {"day": d[2], "weights": w, "events": []},
            {"day": d[3], "weights": w, "events": []}]  # fmt: skip
    idx = mi.replica_index(path, adj)
    assert idx[d[0]] == 1.0 and idx[d[1]] == 1.0  # signal day, then the fill at 10-05's close
    assert abs(idx[d[2]] - (0.5 * 73.44 / 72 + 0.5)) < 1e-12  # first day invested
    assert abs(idx[d[3]] - (0.5 * 72 / 72 + 0.5)) < 1e-12  # holdings drift, no rebalance


def test_update_alpaca_backup_and_crosscheck(tmp_path: Path) -> None:
    import httpx
    from conftest import synthetic_bars

    from us_stock_research.collectors.alpaca_daily import parse_daily
    from us_stock_research.collectors.update import run_update
    from us_stock_research.storage import CsvStore

    rows = [{"t": "2026-10-02T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]
    assert parse_daily(rows)[0].day == date(2026, 10, 2) and parse_daily(rows)[0].adj_close == 1.5
    bars = synthetic_bars(date(2026, 9, 1), 23, 0.001, 0.01, 0.0)  # ends on 2026-10-01
    today = date(2026, 10, 2)
    store = CsvStore(tmp_path)
    for sym in ("DOWN", "OK"):
        store.write_bars(sym, bars)
        store.write_dividends(sym, {})
    req = httpx.Request("GET", "https://example.test")
    extra = DailyBar(today, 10, 11, 9, 10.5, 10.5, 5)

    def fetch(symbol: str, a: date, b: date):  # type: ignore[no-untyped-def]
        if symbol == "DOWN":
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(500, request=req))
        return [x for x in bars if x.day >= a] + [extra], {}

    def alt(symbol: str, a: date, b: date) -> list[DailyBar]:
        close = 10.5 if symbol == "DOWN" else 10.6  # OK: 0.95% above Yahoo -> mismatch
        return [DailyBar(today, 10, 11, 9, close, close, 7)]

    out = run_update(["DOWN", "OK"], store, fetch, today, date(2000, 1, 1), execute=True,
                     pause=0.0, sleep=lambda _: None, retries=1, backoff=1.0, alt=alt)  # fmt: skip
    assert out["failed"] == {} and out["fallback"] == {"DOWN": 1} and out["behind"] == []
    assert store.read_bars("DOWN")[-1].day == today and store.read_bars("DOWN")[-1].close == 10.5
    assert out["crosscheck_mismatch"] == ["OK"]
    assert abs(out["crosscheck"]["OK"]["diff"] - (10.6 / 10.5 - 1)) < 1e-12


def test_paper_stage2_rehearsal_six_etfs(tmp_path: Path) -> None:
    import json as js
    import os

    import httpx
    import yaml

    import us_stock_research.config as cfg
    from us_stock_research.trading import alpaca_paper as ap

    broker = {"cash": 100_000.0, "positions": {}, "orders": {}}  # type: ignore[var-annotated]
    price = {"SSO": 72.0, "BIL": 91.4, "TLT": 77.3, "IEF": 89.1, "GLD": 382.0, "SPY": 779.0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "paper-api.alpaca.markets"
        p = request.url.path
        if p == "/v2/account":
            eq = broker["cash"] + sum(q * price[s] for s, q in broker["positions"].items())
            return httpx.Response(200, json={"equity": str(eq), "cash": str(broker["cash"])})
        if p == "/v2/positions":
            return httpx.Response(
                200,
                json=[{"symbol": s, "qty": str(q)} for s, q in broker["positions"].items() if q],
            )
        if p == "/v2/orders:by_client_order_id":
            o = broker["orders"].get(request.url.params["client_order_id"])
            return httpx.Response(200, json=o) if o else httpx.Response(404)
        if p == "/v2/orders" and request.method == "POST":
            b = js.loads(request.content)
            broker["orders"][b["client_order_id"]] = dict(b, status="accepted")
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(p)  # fmt: skip

    def close_auction() -> None:  # every accepted MOC order fills at the close
        for o in broker["orders"].values():
            if o["status"] == "accepted":
                q = int(o["qty"]) * (1 if o["side"] == "buy" else -1)
                broker["positions"][o["symbol"]] = broker["positions"].get(o["symbol"], 0) + q
                broker["cash"] -= q * price[o["symbol"]]
                o["status"] = "filled"

    conf = tmp_path / "paper.yml"
    hold = tmp_path / "portfolio/paper/mix.yml"
    conf.write_text(yaml.safe_dump({"enabled": True, "study": "mix", "holdings": str(hold)}))
    orders = tmp_path / "orders/mix"
    orders.mkdir(parents=True)
    day1 = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
    day2 = date.today().isoformat()
    intent = {
        "study": "mix",
        "signal_day": day1,
        "mode": "live-candidate",
        "trading_enabled": False,
        "orders": [
            {"side": "BUY", "symbol": s, "shares": n}
            for s, n in (("SSO", 347), ("BIL", 182), ("TLT", 215), ("IEF", 187), ("GLD", 43))
        ],
    }
    (orders / f"{day1}.json").write_text(js.dumps(intent))  # fmt: skip
    saved = (ap.PaperClient, ap.submission_window_ok, cfg.load_settings, dict(os.environ))
    ap.PaperClient = lambda k, s: saved[0](k, s, transport=httpx.MockTransport(handler))  # type: ignore[assignment,misc]
    ap.submission_window_ok = lambda now: True  # type: ignore[assignment]
    cfg.load_settings = lambda: None  # type: ignore[assignment]
    os.environ.update(ALPACA_PAPER_KEY_ID="k", ALPACA_PAPER_SECRET_KEY="s")
    run = ["--config", str(conf), "--orders-dir", str(tmp_path / "orders")]
    try:
        assert ap.main(["--sync", *run]) == 0
        assert ap.main(["--submit", *run]) == 0
        assert len(broker["orders"]) == 5
        close_auction()
        assert ap.main(["--sync", *run]) == 0  # positions match what was sent: no break
        h = yaml.safe_load(hold.read_text())
        assert h["positions"]["GLD"] == 43 and day1 in h["submitted_signal_days"]
        assert ap.main(["--submit", *run]) == 0 and len(broker["orders"]) == 5  # not resent
        (orders / f"{day2}.json.rejected").write_text("{}")  # today's list set aside
        assert ap.main(["--submit", *run]) == 0 and len(broker["orders"]) == 5
        broker["positions"]["TLT"] -= 10  # broker differs from expectation -> stop
        assert ap.main(["--sync", *run]) == 1
        (orders / f"{day2}.json").write_text(js.dumps(dict(intent, signal_day=day2)))
        assert ap.main(["--submit", *run]) == 1 and len(broker["orders"]) == 5
    finally:
        ap.PaperClient, ap.submission_window_ok, cfg.load_settings = saved[:3]  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(saved[3])


def test_live_manual_ledger(tmp_path: Path) -> None:
    import pytest

    from us_stock_research.trading import live_ledger as ll
    from us_stock_research.trading.order_intent import load_holdings

    led, fills = tmp_path / "live/schwab.yml", tmp_path / "live/fills.csv"
    with pytest.raises(ValueError):
        ll.init(led, 20_000, date(2026, 10, 30), cap=10_000)  # above the user's cap
    ll.init(led, 10_000, date(2026, 10, 30), cap=10_000)
    with pytest.raises(ValueError):
        ll.init(led, 5_000, date(2026, 10, 30))  # never overwrite
    out = ll.fill(led, fills, day=date(2026, 11, 2), symbol="SSO", side="BUY", qty=69,
                  price=72.0, fee=1.0, close=71.8)  # fmt: skip
    assert abs(out["slippage_bps"] - (72.0 / 71.8 - 1) * 1e4) < 1e-9  # paid above the close
    assert out["ledger"]["cash_usd"] == round(10_000 - 69 * 72.0 - 1.0, 2)
    with pytest.raises(ValueError):
        ll.fill(led, fills, day=date(2026, 11, 2), symbol="SSO", side="SELL", qty=70, price=72.0)
    ll.fill(led, fills, day=date(2026, 11, 3), symbol="SSO", side="SELL", qty=9, price=73.0,
            close=73.5)  # fmt: skip
    ll.adjust_cash(led, 3.21, "BIL dividend")
    h = load_holdings(led, 0.0)  # the order generator reads it like any ledger
    assert h.positions == {"SSO": 60.0}
    assert abs(h.cash_usd - round(10_000 - 69 * 72 - 1 + 9 * 73 + 3.21, 2)) < 1e-9
    assert len(fills.read_text().strip().splitlines()) == 3  # header + 2 fills


def test_trend_ensemble_engine_and_independent_check() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research import leveraged_trend as lt
    from us_stock_research.research import trend_ensemble as te
    from us_stock_research.research import verify_ensemble as ve

    days = trading_days(date(1998, 1, 2), date(2004, 12, 31))
    rng = random.Random(5)
    prices, p = [], 100.0
    for _ in days:
        p *= 1 + rng.gauss(0.0003, 0.013)
        prices.append(p)
    yields: list[float | None] = [None] * 3 + [4.0 + rng.random() for _ in days[3:]]
    rule = {"signals": {"sma_days": [50, 100, 200], "momentum_days": 252}, "vol_target": 0.25,
            "vol_window_days": 20, "max_leverage": 2.0, "rebalance_band": 0.25,
            "trading_cost_bps": 10}  # fmt: skip
    single = te.simulate_targets(days, prices, yields, te.ensemble_targets(
        prices, yields, sma_days=[200], momentum_days=None, vol_window=20, vol_target=0.25,
        max_leverage=2.0), band=0.25, cost_bps=10)  # fmt: skip
    ref = lt.simulate_vol_target(
        days,
        prices,
        yields,
        sma_days=200,
        vol_window=20,
        vol_target=0.25,
        max_leverage=2.0,
        band=0.25,
        cost_bps=10,
    )
    assert len(single) == len(ref)
    assert max(abs(a[1] - b[1]) for a, b in zip(single, ref, strict=True)) < 1e-12
    rows = te.run_ensemble(rule, days, prices, yields)
    fracs = {round(r[3] / max(r[3], 1e-9), 6) for r in rows}
    assert len({round(x, 2) for _, _, _, x in rows}) > 4 and fracs  # graded exposure used
    c = {"rule": rule, "data": {"start": date(1999, 6, 1), "end": date(2004, 12, 31)}}
    win = te.window(rows, c["data"]["start"], c["data"]["end"])
    ind = ve.monthly_returns(c, days, prices, yields)
    assert set(ind) == set(win["months"])
    assert max(abs(ind[m] - r) for m, r in zip(win["months"], win["strategy"], strict=True)) < 1e-12


def test_factor_sleeve_mix_matches_independent_and_two_sleeve_case() -> None:
    import random
    from datetime import timedelta

    from us_stock_research.research import factor_sleeve as fs
    from us_stock_research.research import sleeve_mix as sm
    from us_stock_research.research import verify_factor_sleeve as vfs

    rng = random.Random(7)
    days = [date(2004, 1, 1) + timedelta(days=i) for i in range(900)]
    days = [d for d in days if d.weekday() < 5]
    inputs = {}
    for s in ("TLT", "GLD"):
        p = [100.0]
        for _ in days[1:]:
            p.append(p[-1] * (1 + rng.gauss(0.0003, 0.01)))
        inputs[s] = (days, p, [4.0] * len(days))
    ports = {n: [(d, rng.gauss(0.0004, 0.012)) for d in days] for n in ("big_hiop", "big_hiprior")}
    start, end = date(2005, 1, 1), date(2006, 6, 30)
    defensive = sm.defensive_monthly(sm.defensive_series(inputs, 60, 10), start, end)
    vt = {m: rng.gauss(0.01, 0.04) for m in defensive}
    factor = fs.sleeve_monthly(fs.daily_factor_sleeve(ports, 0.005), sorted(ports), start, end)
    w = {"aggressive": 0.4, "defensive": 0.4, "factor": 0.2}
    mix = fs.combine_many({"aggressive": vt, "defensive": defensive, "factor": factor}, w, 10)
    ind = vfs.mix_monthly(vt_monthly=vt, inputs=inputs, sma=60, bps=10, portfolios=ports,
                          drag=0.005, weights=w, start=start, end=end)  # fmt: skip
    assert set(ind) == set(mix) and len(mix) == 18
    assert max(abs(ind[m] - mix[m]) for m in mix) < 1e-14
    two = fs.combine_many({"a": vt, "d": defensive}, {"a": 0.5, "d": 0.5}, 10)
    ref = sm.combine(vt, defensive, 0.5, 10)
    assert max(abs(two[m] - ref[m]) for m in ref) < 1e-15
    # the drag costs about 0.5% a year
    gross = fs.sleeve_monthly(ports, sorted(ports), start, end)
    yearly = [(1 + gross[m]) / (1 + factor[m]) - 1 for m in gross]
    assert 0.004 < sum(yearly) / len(yearly) * 12 < 0.006
    assert fs.full_months(date(2013, 7, 18), date(2005, 1, 1), end)[0] == date(2013, 8, 1)


def test_stocks_ledger_checks_and_alerts(tmp_path: Path) -> None:
    from us_stock_research.trading import stocks as st

    path = tmp_path / "stocks.yml"
    limits = {"max_position_share": 0.25, "drop_from_cost": 0.20, "section_drawdown": 0.30}
    led = st.init(path, 10_000.0, date(2026, 10, 8))
    with pytest.raises(ValueError):
        st.init(path, 1.0, date(2026, 10, 8))
    # plan: 30 x $100 = 30% of the section -> warning with the room left (25 shares)
    chk = st.check_trade(led, {}, symbol="AAA", side="BUY", qty=30, price=100.0, limits=limits)
    assert chk["weight_after"] == pytest.approx(0.30) and "最多再买约 25 股" in chk["warnings"][0]
    ok = st.check_trade(led, {}, symbol="AAA", side="BUY", qty=20, price=100.0, limits=limits)
    assert ok["warnings"] == [] and ok["blocking"] == []
    assert st.check_trade(led, {}, symbol="AAA", side="SELL", qty=1, price=100.0,
                          limits=limits)["blocking"]  # fmt: skip
    st.record(led, day=date(2026, 10, 9), symbol="AAA", side="BUY", qty=20, price=100.0, fee=1.0)
    st.record(led, day=date(2026, 10, 9), symbol="BBB", side="BUY", qty=10, price=50.0)
    assert led["cash_usd"] == pytest.approx(10_000 - 2001 - 500)
    realized = st.record(led, day=date(2026, 10, 12), symbol="AAA", side="SELL", qty=5,
                         price=120.0, fee=1.0)  # fmt: skip
    assert realized == pytest.approx(5 * 120 - 1 - 2001 * 5 / 20)
    assert led["positions"]["AAA"]["qty"] == 15
    st.save(path, led)
    led = st.load(path)
    # AAA down 25% from cost, BBB up a lot -> drop alert; BBB weight above 25%
    v = st.valuation(led, {"AAA": 75.0, "BBB": 400.0})
    notes = st.alerts(led, v, limits)
    assert any("AAA 比成本跌" in n for n in notes) and any("BBB 占板块" in n for n in notes)
    st.split(led, "BBB", 2)
    v2 = st.valuation(led, {"AAA": 75.0, "BBB": 200.0})
    assert v2["nav"] == pytest.approx(v["nav"])
    led["peak_nav_usd"] = v["nav"] / 0.6
    assert any("板块净值比最高点低" in n for n in st.alerts(led, v, limits))
    assert "个股板块：净值" in st.summary_line(led, v, notes, 0.05)
    assert "| AAA |" in st.render_status(led, v, notes, 0.05, date(2026, 10, 12))


def test_stocks_screen_rules() -> None:
    from us_stock_research.trading import stocks as st

    cfg = {"lookback_days": 252, "skip_days": 21, "trend_sma_days": 200, "vol_days": 60,
           "drop_top_vol_share": 0.10, "top_n": 3, "max_per_sector": 2}  # fmt: skip

    def path(growth: float, wiggle: float, n: int = 300) -> list[float]:
        return [100 * (1 + growth) ** i * (1 + wiggle * (-1) ** i) for i in range(n)]

    series = {
        "UP1": path(0.003, 0.001), "UP2": path(0.002, 0.001), "UP3": path(0.0025, 0.001),
        "DOWN": path(-0.001, 0.001), "WILD": path(0.004, 0.05), "SHORT": path(0.01, 0.0, 100),
        **{f"F{i}": path(0.0001 * i, 0.002) for i in range(1, 10)},
    }  # fmt: skip
    meta = {s: (s.lower(), "Tech" if s.startswith("UP") else "Other") for s in series}
    res = st.screen(series, meta, cfg, held={"UP2"})
    syms = [r["symbol"] for r in res["candidates"]]
    assert "SHORT" not in syms and "DOWN" not in syms and "WILD" not in syms  # history/trend/vol
    assert syms[:2] == ["UP1", "UP3"] and "UP2" not in syms  # sector cap 2 keeps the top two
    assert res["eligible"] == len(series) - 1
    m = st.screen_metrics(series["UP1"], cfg)
    assert m is not None and m["momentum_12_1"] == pytest.approx(
        series["UP1"][-22] / series["UP1"][-253] - 1
    )
    md = st.render_screen(res, date(2026, 10, 9), m)
    assert "不是买入建议" in md and "| 1 | UP1 |" in md


def test_alpaca_fetch_many_pages_and_symbols() -> None:
    import httpx

    from us_stock_research.collectors.alpaca_daily import fetch_many

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params.get("feed") == "sip":
            return httpx.Response(403, json={})
        bar = {"t": "2026-10-07T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 2.5, "v": 9}
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"bars": {"BRK.B": [bar]}, "next_page_token": "x"})
        bar2 = dict(bar, t="2026-10-08T04:00:00Z", c=3.0)
        return httpx.Response(200, json={"bars": {"BRK.B": [bar2], "AAPL": [bar]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = fetch_many(client, ["BRK-B", "AAPL"], date(2026, 10, 1), date(2026, 10, 8), "k", "s")
    assert [b.close for b in out["BRK-B"]] == [2.5, 3.0] and len(out["AAPL"]) == 1
    assert calls[0]["symbols"] == "BRK.B,AAPL" and calls[-1]["feed"] == "iex"


def test_schwab_tokens_and_refresh(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    import httpx

    from us_stock_research.trading import schwab_api as sw

    url = sw.authorize_url("KEY", "https://127.0.0.1", state="s")
    assert "client_id=KEY" in url and "redirect_uri=https%3A%2F%2F127.0.0.1" in url
    assert sw.code_from_redirect("https://127.0.0.1/?code=C0.abc%40&session=x") == "C0.abc@"
    with pytest.raises(ValueError):
        sw.code_from_redirect("https://127.0.0.1/?session=x")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        seen.append(body["grant_type"])
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(
            200, json={"access_token": f"A{len(seen)}", "refresh_token": "R", "expires_in": 1800}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    t0 = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    tokens = sw.exchange_code(
        http, "KEY", "SEC", "https://127.0.0.1", "https://127.0.0.1/?code=X", t0
    )
    path = tmp_path / "tok.json"
    sw.save_tokens(path, tokens)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert sw.access_token(http, "KEY", "SEC", path, t0 + timedelta(minutes=5)) == "A1"
    assert sw.access_token(http, "KEY", "SEC", path, t0 + timedelta(hours=1)) == "A2"
    assert seen == ["authorization_code", "refresh_token"]
    assert sw.refresh_days_left(sw.load_tokens(path), t0 + timedelta(days=5)) == pytest.approx(2)
    with pytest.raises(sw.AuthExpired):
        sw.access_token(http, "KEY", "SEC", path, t0 + timedelta(days=7, minutes=1))


def test_schwab_snapshot_reconcile_and_fill_routing(tmp_path: Path) -> None:
    from us_stock_research.trading import live_ledger, stocks
    from us_stock_research.trading import schwab_api as sw

    body = {
        "securitiesAccount": {
            "positions": [
                {"instrument": {"symbol": "SSO", "assetType": "EQUITY"}, "longQuantity": 69.0},
                {"instrument": {"symbol": "NVDA", "assetType": "EQUITY"}, "longQuantity": 5.0},
            ],
            "currentBalances": {"cashBalance": 1234.5},
        }
    }
    snap = sw.parse_account(body)
    assert snap == {"cash": 1234.5, "positions": {"SSO": 69.0, "NVDA": 5.0}}
    syms = {"SPY", "SSO", "BIL", "TLT", "IEF", "GLD"}
    live = {"positions": {"SSO": 69.0}}
    st = {"positions": {"NVDA": {"qty": 4.0, "cost_usd": 700.0}}}
    diffs = sw.reconcile(snap, live, st, syms)
    assert diffs == ["NVDA：账户 5 股，个股账本 4 股"]
    assert sw.strategy_matches(snap, live, syms)
    assert not sw.strategy_matches(snap, {"positions": {"SSO": 68}}, syms)
    order = {
        "orderId": 111,
        "status": "FILLED",
        "closeTime": "2026-11-02T15:00:00+0000",
        "orderLegCollection": [
            {"instruction": "BUY", "quantity": 10, "instrument": {"symbol": "SSO"}}
        ],
        "orderActivityCollection": [
            {"executionLegs": [{"quantity": 6, "price": 71.0}, {"quantity": 4, "price": 71.5}]}
        ],
    }
    legs = sw.filled_legs(order)
    assert legs[0]["qty"] == 10 and legs[0]["price"] == pytest.approx(71.2)
    nv = dict(
        order,
        orderId=222,
        orderLegCollection=[
            {"instruction": "SELL", "quantity": 1, "instrument": {"symbol": "NVDA"}}
        ],
        orderActivityCollection=[{"executionLegs": [{"quantity": 1, "price": 200.0}]}],
    )
    live_path, fills = tmp_path / "live.yml", tmp_path / "fills.csv"
    live_ledger.init(live_path, 5000.0, date(2026, 10, 30))
    st_path = tmp_path / "stocks.yml"
    stocks.init(st_path, 2000.0, date(2026, 10, 30))
    s = stocks.load(st_path)
    stocks.record(s, day=date(2026, 10, 30), symbol="NVDA", side="BUY", qty=2, price=150.0)
    stocks.save(st_path, s)
    cfg = {"ledger": str(st_path), "fills": str(tmp_path / "sf.csv")}
    lines = sw.record_fills(
        legs + sw.filled_legs(nv),
        date(2026, 11, 2),
        live_path=live_path,
        live_fills=fills,
        stocks_cfg=cfg,
        strategy_symbols=syms,
    )
    assert len(lines) == 2 and lines[0].startswith("实盘 BUY SSO 10")
    led = yaml.safe_load(live_path.read_text())
    assert led["positions"] == {"SSO": 10.0} and led["cash_usd"] == pytest.approx(5000 - 712)
    assert stocks.load(st_path)["positions"]["NVDA"]["qty"] == 1
    again = sw.record_fills(
        legs + sw.filled_legs(nv),
        date(2026, 11, 2),
        live_path=live_path,
        live_fills=fills,
        stocks_cfg=cfg,
        strategy_symbols=syms,
    )
    assert again == [] and yaml.safe_load(live_path.read_text())["positions"] == {"SSO": 10.0}
    # the stock section refuses strategy tickers
    chk = stocks.check_trade(
        stocks.load(st_path), {}, symbol="SPY", side="BUY", qty=1, price=700.0, limits={}
    )
    assert chk["blocking"]


def test_schwab_submission_gates_and_order_flow(tmp_path: Path) -> None:
    import json as _json
    from datetime import UTC, datetime, timedelta

    from us_stock_research.trading import schwab_api as sw

    folder = tmp_path / "orders/live/vt_plus_defensive"
    folder.mkdir(parents=True)
    intent = {
        "mode": "live-candidate",
        "signal_day": "2026-10-30",
        "orders": [
            {
                "side": "SELL",
                "symbol": "TLT",
                "shares": 10,
                "ref_price": 90.0,
                "est_value_usd": 900.0,
            },
            {
                "side": "BUY",
                "symbol": "SSO",
                "shares": 20,
                "ref_price": 70.0,
                "est_value_usd": 1400.0,
            },
        ],
    }
    ip = folder / "2026-10-30.json"
    ip.write_text(_json.dumps(intent))
    (folder / "2026-10-30.md").write_text("...\n**结论：一致**\n")
    cfg = {
        "orders_enabled": False,
        "user_decision": "",
        "stop_file": "portfolio/STOP_TRADING",
        "session_window_et": ["09:45", "15:30"],
        "max_orders_per_day": 8,
        "max_order_usd": 10000,
        "limit_offset_bps": 10,
        "max_quote_deviation": 0.03,
        "sell_fill_wait_minutes": 1,
    }
    monday_1000_et = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    r = sw.submission_gates(cfg, ip, monday_1000_et, root=tmp_path)
    assert any("未打开" in x for x in r) and any("用户决定" in x for x in r)
    on = dict(cfg, orders_enabled=True, user_decision="用户 2026-11-20 批准（测试）")
    assert sw.submission_gates(on, ip, monday_1000_et, root=tmp_path) == []
    assert any(
        "执行日" in x
        for x in sw.submission_gates(on, ip, monday_1000_et + timedelta(days=1), root=tmp_path)
    )
    assert any(
        "时段" in x
        for x in sw.submission_gates(
            on, ip, datetime(2026, 11, 2, 21, 0, tzinfo=UTC), root=tmp_path
        )
    )
    (tmp_path / "portfolio").mkdir()
    (tmp_path / "portfolio/STOP_TRADING").touch()
    assert any("停止" in x for x in sw.submission_gates(on, ip, monday_1000_et, root=tmp_path))
    (tmp_path / "portfolio/STOP_TRADING").unlink()
    (folder / "2026-10-30.md").write_text("**结论：不一致，请勿执行**")
    assert any("复核" in x for x in sw.submission_gates(on, ip, monday_1000_et, root=tmp_path))
    (folder / "2026-10-30.md").write_text("**结论：一致**")
    (folder / "submitted.jsonl").write_text(_json.dumps({"signal_day": "2026-10-30"}) + "\n")
    assert any("已经提交" in x for x in sw.submission_gates(on, ip, monday_1000_et, root=tmp_path))
    # limit prices: capped offset from the quote; refused when the quote is far from the list
    assert sw.limit_price("BUY", 70.0, {"askPrice": 70.5}, on) == pytest.approx(70.57)
    assert sw.limit_price("SELL", 90.0, {"bidPrice": 89.8}, on) == pytest.approx(89.71)
    assert sw.limit_price("BUY", 70.0, {"askPrice": 75.0}, on) is None

    class Fake:
        def __init__(self) -> None:
            self.placed: list[tuple[str, str, int, float]] = []

        def quotes(self, symbols: list[str]) -> dict[str, dict[str, float]]:
            return {"TLT": {"bidPrice": 89.9}, "SSO": {"askPrice": 70.2}}

        def place_limit(self, h: str, sym: str, side: str, qty: int, px: float) -> str:
            self.placed.append((sym, side, qty, px))
            return str(len(self.placed))

        def order(self, h: str, oid: str) -> dict[str, str]:
            return {"status": "FILLED"}

        def snapshot(self, h: str) -> dict[str, Any]:
            return {"cash": 1000.0, "positions": {}}

    fake = Fake()
    rows = sw.submit(fake, "H", intent, on, wait=lambda s: None)  # type: ignore[arg-type]
    assert [p[:2] for p in fake.placed] == [("TLT", "SELL"), ("SSO", "BUY")]
    assert fake.placed[1][2] == 14  # cash $1000 / 70.27 -> 14 shares, not 20
    assert rows[1]["shares_sent"] == 14
    assert sw.limit_order("SSO", "BUY", 14, 70.27)["price"] == "70.27"


def test_ml_rank_pieces() -> None:
    import math
    import random

    import numpy as np

    from us_stock_research.research import ml_features as mf
    from us_stock_research.research import ml_rank as mr
    from us_stock_research.research import verify_ml as vm

    rng = random.Random(5)
    x = [[rng.gauss(0, 1) for _ in range(5)] for _ in range(260)]
    y = [0.3 + 1.5 * a[0] - 0.7 * a[1] + 0.2 * a[4] + rng.gauss(0, 0.1) for a in x]
    ours = vm.regress(y, x)
    ref = mf.ols_coefs(np.array(y), np.array(x))
    assert ref is not None and max(abs(a - b) for a, b in zip(ours, ref, strict=True)) < 1e-9
    assert mf.ols_coefs(np.array(y[:150]), np.array(x[:150])) is None  # < 200 rows
    rows = [{"symbol": s, "ret": r, "f": {"mom_12_1": m, "vol_60": v}}
            for s, r, m, v in (("A", 0.05, 0.3, 0.01), ("B", -0.02, 0.1, 0.03),
                               ("C", 0.01, None, 0.02))]  # fmt: skip
    sc = mr.linear_scores(rows, {"technical": {"mom_12_1": "+", "vol_60": "-"}})
    assert sc["A"] == pytest.approx(1.0) and sc["B"] == pytest.approx(0.0)
    assert sc["C"] == pytest.approx(0.5)  # only vol_60, middle rank
    assert mr.label(rows) == [1.0, 0.0, 0.5]
    month = {"market_state": {"vix": 20.0}}
    mat = mr.design(rows, month, ("mom_12_1",), ("vix",))
    assert mat[0] == [1.0, 20.0] and math.isnan(mat[2][0])
    test = [{"date": "2020-01-31", "spy": 0.01, "priced": 9, "members": 10, "eligible": 3,
             "with_fundamentals": 3},
            {"date": "2020-02-28", "spy": 0.0, "priced": 10, "members": 10, "eligible": 3,
             "with_fundamentals": 3}]  # fmt: skip
    panel = {"2020-01-31": rows, "2020-02-28": rows}
    scores = {
        "2020-01-31": {"A": 2.0, "B": 1.0, "C": 0.0},
        "2020-02-28": {"A": 0.0, "B": 2.0, "C": 1.0},
    }
    out = mr.portfolio(test, panel, scores, 2, 10.0)
    assert out[0]["picks"] == ["A", "B"]
    assert out[0]["strategy"] == pytest.approx(0.5 * 0.05 + 0.5 * -0.02 - 10e-4)
    held = {
        "A": 0.5 * 1.05 / (0.5 * 1.05 + 0.5 * 0.98),
        "B": 0.5 * 0.98 / (0.5 * 1.05 + 0.5 * 0.98),
    }
    traded = abs(held["A"] - 0) + abs(held["B"] - 0.5) + 0.5
    assert out[1]["strategy"] == pytest.approx(0.5 * -0.02 + 0.5 * 0.01 - traded * 1e-3)
    assert out[0]["coverage"] == pytest.approx(0.9)


def test_vt_only_operating_config() -> None:
    import random

    from us_stock_research.calendar import trading_days
    from us_stock_research.research.sleeve_mix import load_mix_contract
    from us_stock_research.trading import mix_intent as mi
    from us_stock_research.trading import verify_intent as vi
    from us_stock_research.trading.order_intent import Holdings

    ops = yaml.safe_load((ROOT / "configs/operating.yml").read_text())
    contract = load_mix_contract(ROOT / ops["contract"])
    assert contract["name"] == ops["study"] == "vt_only"
    assert contract["rule"]["aggressive_weight"] == 1.0 and contract["human_review"]["approved"]
    paper = yaml.safe_load((ROOT / "configs/paper_broker.yml").read_text())
    assert paper["study"] == ops["study"]
    days = trading_days(date(2025, 1, 2), date(2026, 6, 30))
    rng = random.Random(29)
    series = {}
    for sym, drift, vol in (
        ("SPY", 0.0006, 0.008),
        ("SSO", 0.0012, 0.016),
        ("BIL", 0.00015, 0.0),
        ("TLT", 0.0, 0.012),
        ("IEF", 0.0, 0.006),
        ("GLD", 0.0, 0.015),
    ):
        p, xs_ = 100.0, []
        for _ in days:
            p *= 1 + rng.gauss(drift, vol)
            xs_.append(p)
        series[sym] = xs_
    adj = {s: dict(zip(days, v, strict=True)) for s, v in series.items()}
    vt_rule = {
        "sma_days": 200,
        "vol_window_days": 20,
        "vol_target": 0.25,
        "max_leverage": 2.0,
        "rebalance_band": 0.25,
    }
    slots = ["TLT", "IEF", "GLD"]
    start = date(2025, 11, 3)
    path = mi.model_path(adj, days, start, vt_rule, contract["rule"], slots)
    for row in path:
        assert set(row["weights"]) <= {"SPY", "SSO", "BIL"}
        assert not any(e.split()[0] in slots for e in row["events"])  # no defensive switches
        assert row["aggressive_share"] == pytest.approx(1.0)
    bars = {
        s: [DailyBar(d, x, x, x, x, x, 1) for d, x in zip(days, v, strict=True)]
        for s, v in series.items()
    }
    mix = {**contract, "data": {**contract["data"], "defensive_assets": slots}}
    it = mi.generate(mix, {"rule": vt_rule}, bars, Holdings(cash_usd=100_000.0), start, start)
    assert it["orders"] and {o["symbol"] for o in it["orders"]} <= {"SPY", "SSO", "BIL"}
    assert "无防守部分" in mi.summary_line(it)
    rec = vi.replay(
        adj,
        start,
        start,
        sma_days=200,
        vol_days=20,
        vol_target=0.25,
        cap=2.0,
        band=0.25,
        aggressive_weight=1.0,
        slots=slots,
    )
    prices = {s: v[days.index(start)] for s, v in series.items()}
    assert vi.check(it, rec, vi.expected_orders(rec["weights"], {}, 100_000.0, prices)) == []
