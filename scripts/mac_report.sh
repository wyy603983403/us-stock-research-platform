#!/usr/bin/env bash
# 引擎自检（独立实现对照）→ 重跑回测 → 生成 quantstats HTML 报告。
#   bash scripts/mac_report.sh                       # 默认研究：etf-trend-baseline
#   bash scripts/mac_report.sh research/xxx/study.yml
set -euo pipefail
cd "$(dirname "$0")/.."
CONTRACT="${1:-research/etf-trend-baseline/study.yml}"
NAME="$(basename "$(dirname "$CONTRACT")")"
OUT="artifacts/$(echo "$NAME" | tr - _)"
.venv/bin/pip install -q -e '.[dev,report]'

echo "== 引擎自检：独立 pandas 实现 vs 生产引擎 =="
.venv/bin/usr-verify-engine --contract "$CONTRACT" --output "$OUT/engine_check.json" \
  | grep -E '"(strategy|passed|max_relative_difference)"'

echo "== 回测 =="
.venv/bin/usr-backtest --contract "$CONTRACT" --output "$OUT/backtest.json" | tail -n 12

echo "== quantstats 报告 =="
.venv/bin/usr-report --artifact "$OUT/backtest.json" --output "$OUT/report.html"
open "$OUT/report.html" 2>/dev/null || echo "用浏览器打开 $OUT/report.html"
