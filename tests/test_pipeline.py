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
        if "kind: cross_section" not in path.read_text():  # validated by the xs engine test
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
            assert xs.load_xs_contract(path_)["selection"]["top_n"] in (50, 100)


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
            rows += _annual(f"{k}-{year}", cik, sic, date(year, 12, 31), filed, ni,
                            rng.uniform(-3, 20), assets, equity, rng.uniform(1, 3))  # fmt: skip
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
