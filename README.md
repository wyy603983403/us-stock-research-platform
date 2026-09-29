# US Stock Research Platform

私人美股研究仓库，结构参照 [crypto-research-platform](https://github.com/wyy603983403/crypto-research-platform)：
先写研究合同 → 取数 → 质量门禁 → 内容寻址快照 → 只读回测 → 风险门禁 → 人工审查。

> 本仓库只做数据研究与回测，不提供投资建议，不连接券商，不自动下单，不保存任何密钥。

## 研究原则

- **本金优先**：任意滚动 12 个月亏损不超过本金的 25%（`configs/risk/default.yml`）；最大回撤只报告、不设硬上限。
- **先登记后回测**：参数写进 `study.yml` 后才能跑；看完结果再改参数就是新版本研究。
- **有基准**：每项研究必须对比基准（默认 SPY 买入持有），并给出块自助法置信区间。
- **失败也留档**：未通过的研究写进 [docs/rejected.md](docs/rejected.md)。

## 架构

```text
Yahoo 日线（复权）/ 以后可加第二来源
          ↓
  collectors（默认只预览，--execute 才写入 data/daily/*.csv）
          ↓
 quality gate → snapshots（sha256 内容寻址）→ read-only backtest → artifacts/
          ↓
   风险门禁（最差 12 个月亏损 ≤ 25%）与人工审查
```

## 快速开始

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env

# 1. 预览下载（不写文件），确认无误后加 --execute
usr-collect-daily SPY QQQ IWM EFA IEF GLD SHY --start 2004-01-01
usr-collect-daily SPY QQQ IWM EFA IEF GLD SHY --start 2004-01-01 --execute

# 2. 质量门禁
usr-audit-ohlcv SPY QQQ IWM EFA IEF GLD SHY --output artifacts/quality/etf_trend_baseline.json

# 3. 快照（输出 daily-bundle-v1:sha256:...）
usr-snapshot SPY QQQ IWM EFA IEF GLD SHY

# 4. 人工把 snapshot_id、quality_report 写进 research/etf-trend-baseline/study.yml，status 改为 frozen

# 5. 回测与晋级评估（只读，绝不自动晋级）
usr-backtest --contract research/etf-trend-baseline/study.yml --output artifacts/etf_trend_baseline/backtest.json
usr-assess-research --contract research/etf-trend-baseline/study.yml --artifact artifacts/etf_trend_baseline/backtest.json
```

也可用 `scripts/run_baseline.sh` 一次完成第 1–3 步；Mac 首次使用运行 `bash scripts/mac_bootstrap.sh`（推送、建环境、测试、取数、快照一步到位）。

### 券商成本对比

`configs/brokers.yml` 登记了嘉信国际、盈透固定费率、币安美股三种成本（佣金、每单最低、每股费用、股息预扣税）。
同一研究合同、同一快照，只换成本模型：

```bash
usr-compare-brokers --contract research/etf-trend-baseline/study.yml \
  --portfolio-usd 10000 100000 --output artifacts/etf_trend_baseline/brokers.json
```

股息预扣税按分红除权日扣减（需要采集器保存的 `*.dividends.csv`）；基准 SPY 同样扣税，保证对比公平。

本地验证：`pytest && ruff check . && ruff format --check . && mypy src`

## 数据存储：CSV 或 Parquet

默认把日线存成 `data/daily/*.csv`。在 `.env` 里设置 `USR_STORAGE_ROOT` 后，改为 zstd 压缩的
Parquet（用 DuckDB 读写），并把数据快照也放到同一根目录下：

```text
<USR_STORAGE_ROOT>/parquet/daily/SPY.parquet
<USR_STORAGE_ROOT>/parquet/dividends/SPY.parquet
<USR_STORAGE_ROOT>/snapshots/<sha256>/
```

例如放到外置数据卷：`USR_STORAGE_ROOT=/Volumes/mysql/duckdb-pilot/us-stock-research`（Mac 上
`mac_bootstrap.sh` 检测到 `/Volumes/mysql/duckdb-pilot` 时会自动写入 `.env`）。
两种格式内容完全相同、浮点数无损往返，快照编号只取决于数据本身，所以换格式不会让已冻结研究合同失效。
当前 7 只 ETF 的数据只有几 MB，Parquet 的意义主要在于和系统盘隔离；扩展到几百只个股或分钟线后才明显省空间。
卷没挂载时，取数和回测会直接报路径错误，不会悄悄写到别处。

## 批量下载常用数据

`configs/universes/` 登记了三份清单：`etf_core`（80 只宽基/行业/国际/债券/商品/因子 ETF）、
`macro_indices`（标普/纳指/VIX/美债收益率/美元指数/黄金原油期货）、`mega_caps`（98 只当前大盘股）；
另可加 `sp500`（现取当前标普 500 成分股）。Mac 上运行：

```bash
bash scripts/mac_download_universe.sh          # 三份默认清单
bash scripts/mac_download_universe.sh sp500    # 另加标普 500
```

断点续传、限速重试、失败只记录不中断，下载后自动质量门禁。
注意：清单只含**今天仍存在**的公司，有幸存者偏差；不要用它估算"选股"的历史收益，
个股研究需要含退市股的历史成分数据（另行采购或建库）。指数、收益率、期货不是可交易价格，
质量门禁对它们的价格异常只给警告。

## 每日增量更新

```bash
bash scripts/mac_daily_update.sh            # 给库里所有标的补最新交易日
bash scripts/mac_install_schedule.sh        # 可选：装成每天北京时间 07:30 的定时任务
```

分红或拆股会让雅虎重述整段复权历史；直接追加新行会在接缝处产生假收益。所以每次先重取 10 天重叠区与库里对比：
一致就追加，被重述就整段重下（报告里逐只列出原因）。已冻结研究用的是内容寻址快照，更新不会改变它们；
新数据只能通过新快照和新版本研究进入。

## 可信度检查（晋级前必做）

- **交易日历**：`usr-audit-ohlcv` 对照 NYSE 日历检查缺失/多余交易日（已与 7 只 ETF 22 年真实数据逐日吻合）。
- **第二数据源**：`usr-crosscheck SPY QQQ … --output artifacts/quality/crosscheck.json`
  比对 Stooq（或 `--file SPY=path.csv` 提供任意来源）的收盘日收益，容差 0.5 个百分点。
- **多重检验**：每次 `usr-backtest` 把参数组合登记到 `research/trials.jsonl`（进 Git），
  晋级评估使用 Deflated Sharpe：试得越多，门槛越高，需 ≥ 0.95。
- **引擎自检**：`usr-verify-engine` 用 pandas 独立重写三种策略的净值逻辑，与生产引擎逐日对比
  （真实 ETF 数据上三种策略差异 < 1e-14）。
- **报告**：`usr-report` 用 quantstats 生成收益、回撤、月度热力图 HTML；`bash scripts/mac_report.sh` 一步完成。
- **幸存者偏差**：合同字段 `data.universe_kind`；`stocks_current_constituents` 永远不能晋级。

自动化交易的分阶段路线见 [docs/trading-roadmap.md](docs/trading-roadmap.md)。

## 已内置策略

| 名称 | 规则 |
|---|---|
| `buy_and_hold_v1` | 风险资产等权，月末再平衡 |
| `trend_sma_v1` | 每个风险资产：月末收盘高于 N 日均线则持有，否则该份额转入现金资产（如 SHY） |
| `vol_target_v1` | 风险资产等权组合，按 `vol_lookback_days` 实现波动缩放到 `vol_target` 年化目标，不加杠杆，其余转入现金资产 |
| `dual_momentum_v1` | 按过去 N 日收益排名取前 k 名，且须跑赢现金资产，否则转入现金 |

回测时点：月末收盘出信号，`execution_lag_days` 天后收盘成交，成本按单边换手 × 基点计。

## 研究合同

- `research/etf-trend-baseline/study.yml`：7 只 ETF 的 200 日均线趋势开关，对比 SPY。状态 `draft`，快照待生成。

## 目录

```text
src/us_stock_research/   采集、质量门禁、快照、回测、晋级评估、风险配置
research/                研究合同（study.yml）
configs/                 数据源与风险配置
docs/                    研究计划、结果与否决记录
scripts/                 一键脚本
tests/                   合成数据测试
data/  artifacts/        本地数据与产物（不进 Git）
```

交接与进度见 [CURRENT_HANDOFF.md](CURRENT_HANDOFF.md) 和 [STATUS.md](STATUS.md)。
