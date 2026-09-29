# 当前交接（2026-09-29）

目标：美股自动化交易系统，按 `docs/trading-roadmap.md` 分阶段推进；`trading_enabled: false`，未接券商下单。

## 数据基座（Mac：`/Volumes/mysql/duckdb-pilot/us-stock-research`，Parquet）

- 日线 + 分红：596 只（80 ETF、13 宏观/指数/期货、503 只标普成分股），591 只通过质量门禁，5 只隔离（DHR ETN HPQ EXPE KDP，见 `configs/quality_exceptions.yml`）。
- 第二来源核对：Tiingo 复权收盘价，核心 ETF 与被标记的 28 只股票通过。
- 宏观：FRED 14 条序列；基本面：SEC EDGAR 503 家（按 `filed` 时点）；元数据：`parquet/meta/`，目录 `artifacts/catalog.md`。
- 每日增量：`usr-update`（重叠窗口比对，发现复权修订则整段重下）；launchd 定时任务脚本已写，用户尚未安装。
- 分钟线（仅 IEX 单一交易所，成交量不可代表全市场）：
  - Alpaca `intraday_1min_alpaca`：SPY、QQQ 已下，2020-07-27 至 2026-09-28，1550 个交易日，约 60 万根/只，含盘前盘后。
  - Tiingo `intraday_1min`：下载器已写（`usr-collect-intraday`），尚未正式下载；探测显示 2018-06 起有数据，2016-06 无。免费额度未核实。

## 研究

- `etf-trend-baseline`、`vol-target-baseline` 已冻结并回测：回撤优于 SPY，收益不优于 SPY（超额收益置信区间下限不为正）。
- 试验登记 `research/trials.jsonl`（Deflated Sharpe 门槛 0.95）；独立引擎复核误差 ~1e-15。
- 已知缺陷：标普成分股只有当前成员，存在幸存者偏差。

## 下一步

1. 分钟线质量检查：每日根数、缺口、半日市、夏令时、筛正常时段（9:30–16:00 美东）。
2. Tiingo 与 Alpaca 分钟线交叉核对。
3. 待用户选择：修幸存者偏差（历史成分股）、基本面因子研究、路线图阶段 1（月度下单意向文件，不下单）、安装每日更新定时任务。
