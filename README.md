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

## 已内置策略

| 名称 | 规则 |
|---|---|
| `buy_and_hold_v1` | 风险资产等权，月末再平衡 |
| `trend_sma_v1` | 每个风险资产：月末收盘高于 N 日均线则持有，否则该份额转入现金资产（如 SHY） |
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
