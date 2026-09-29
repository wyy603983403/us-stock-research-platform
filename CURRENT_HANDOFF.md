# 当前交接（2026-09-29）

目标：美股自动化交易系统，按 `docs/trading-roadmap.md` 分阶段推进；`trading_enabled: false`，未接券商下单。

## 数据基座（Mac：`/Volumes/mysql/duckdb-pilot/us-stock-research`，Parquet）

- 日线 + 分红：596 只（80 ETF、13 宏观/指数/期货、503 只标普成分股），591 只通过质量门禁，5 只隔离（DHR ETN HPQ EXPE KDP，见 `configs/quality_exceptions.yml`）。
- 第二来源核对：Tiingo 复权收盘价，核心 ETF 与被标记的 28 只股票通过。
- 宏观：FRED 14 条序列；基本面：SEC EDGAR 503 家（按 `filed` 时点）；元数据：`parquet/meta/`，目录 `artifacts/catalog.md`。
- 每日增量：`usr-update`（重叠窗口比对，发现复权修订则整段重下）；launchd 定时任务脚本已写，用户尚未安装。
- 分钟线（仅 IEX 单一交易所，成交量不可代表全市场）：
  - Alpaca `intraday_1min_alpaca`：SPY、QQQ 已下，2020-07-27 至 2026-09-28，1550 个交易日，约 60 万根/只，含盘前盘后。
  - Tiingo `intraday_1min`：每次请求最多返回 1 万根（只留最后部分），已改为按月分段 + 截断自动拆分；首次按年下载的 SPY/QQQ 不完整，需 `--restart` 重下。免费额度未核实。
  - 分工：Alpaca 下全部（`scripts/mac_download_intraday.sh alpaca`，`--all-stored`），Tiingo 只下 `configs/universes/intraday_core.yml` 的 15 只核心 ETF。

## 研究

- `etf-trend-baseline`、`vol-target-baseline` 已冻结并回测：回撤优于 SPY，收益不优于 SPY（超额收益置信区间下限不为正）。
- 试验登记 `research/trials.jsonl`（Deflated Sharpe 门槛 0.95）；独立引擎复核误差 ~1e-15。
- 已知缺陷：标普成分股只有当前成员，存在幸存者偏差。

## 下一步

1. 分钟线质量检查与两源核对已写（`usr-audit-intraday`，`quality/intraday.py`）：缺失交易日、节假日/周末出现的 K 线、OHLC 顺序、收盘价对比日线（拆股比例单独计数）、分钟覆盖率、Tiingo/Alpaca 逐分钟比对（含 ±1 分钟标签错位检测）。已在真实数据上跑（2026-09-29，Alpaca 已下 202 只）：201 只通过；FERG 缺 36 个交易日（2021 年在美上市前 IEX 成交稀少）。Alpaca 源有全市场缺失日：2025-03-10（几乎全部）、2021-04-19、2021-10-25、2022-03-08（约 2/3 标的）。分钟覆盖率中位数 0.54（IEX 单一交易所，冷门股大量分钟无成交）。雅虎日线收盘价对分拆做了固定比例回调（BDX CMCSA DD DHR DTE EXC FDX），分钟线为原始成交价，检查已按“连续恒定比例”识别。SPY/QQQ 两源逐分钟一致率 99.97%/100%，中位差 <1 bp，K 线时间标记一致。
2. 研究用正常时段筛选：`quality.intraday.regular_session`。首批 Alpaca 下载截止到 2026-09-29（含当天未收盘的盘前数据），研究时剔除；之后默认截止到前一天。
3. 在用户 Mac 的隔离 Linux 环境里可直接跑检查：用 `/Volumes/mysql/duckdb-pilot/linux-py310`（duckdb/pyarrow，Python 3.10）+ `datetime.UTC` 垫片。该环境无网络、无 pydantic，回测/研究合同类命令仍需在用户终端运行。
4. 数据目录已含分钟线（`usr-catalog`），日线复查 2026-09-29：591/596 通过，隔离 5 只不变。
5. 补充数据（2026-09-29 晚写好，`scripts/mac_data_extras.sh`，用户通宵运行，次日早上检查）：拆股记录 `parquet/splits/`；Fama-French 五因子+动量（日/月）`parquet/factors/`；新增 11 个 FRED 序列与 ^VIX9D ^VIX3M ^VIX6M ^VVIX；标普 500 历史成分（fja05680/sp500，1996 起）`meta/sp500_history`，前成分股日线 `parquet/daily_delisted/`（先雅虎、后 Tiingo，按成分期覆盖率 ≥90% 验收，代码复用的拒收），覆盖表 `meta/sp500_coverage`。
6. 待用户选择：修幸存者偏差（历史成分股）、基本面因子研究、路线图阶段 1（月度下单意向文件，不下单）、安装每日更新定时任务。
