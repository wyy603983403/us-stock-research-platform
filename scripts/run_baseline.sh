#!/usr/bin/env bash
# 一键跑 ETF 趋势基线：下载 → 质量门禁 → 快照 → 提示把快照 ID 写入合同。
# 回测需要人工把 snapshot_id / quality_report 填进 study.yml 后再运行（见 README）。
set -euo pipefail
cd "$(dirname "$0")/.."
SYMBOLS="SPY QQQ IWM EFA IEF GLD SHY"
usr-collect-daily $SYMBOLS --start 2004-01-01 --execute
usr-audit-ohlcv $SYMBOLS --output artifacts/quality/etf_trend_baseline.json
usr-snapshot $SYMBOLS
echo "把上面的 snapshot_id 和 artifacts/quality/etf_trend_baseline.json 写进 research/etf-trend-baseline/study.yml"
