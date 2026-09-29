#!/usr/bin/env bash
# 补充数据（可反复运行，已完成的自动跳过）：
#   1 拆股记录（雅虎）  2 Fama-French 因子  3 新增宏观序列 + VIX 期限结构
#   4 标普 500 历史成分 + 前成分股日线（先雅虎，再 Tiingo；Tiingo 限额用完会等一小时再继续，可能跑一整夜）
#   5 更新数据目录
#   bash scripts/mac_data_extras.sh
# 日志在 logs/extras_*.log，汇总在 artifacts/extras/。插着电源、别合盖。
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
ROOT="$(grep '^USR_STORAGE_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "数据卷未挂载：$ROOT"; exit 1; fi
.venv/bin/pip install -q -e .
mkdir -p artifacts/extras logs
STAMP="$(date +%Y%m%d_%H%M)"
LOG="logs/extras_${STAMP}.log"
run() { echo "== $1"; shift; caffeinate -i "$@" 2>>"$LOG" | tail -n 25; }

run "1/5 拆股记录" .venv/bin/usr-collect-splits --execute --report artifacts/extras/splits.json
run "2/5 Fama-French 因子" .venv/bin/usr-collect-factors --execute --report artifacts/extras/factors.json
run "3/5 宏观序列（FRED）" .venv/bin/usr-collect-macro --execute --report artifacts/extras/macro.json
run "3/5 VIX 期限结构（雅虎）" .venv/bin/usr-collect-universe macro_indices --execute \
  --report artifacts/extras/macro_indices.json
run "4/5 标普 500 历史成分 + 前成分股日线" .venv/bin/usr-collect-sp500-history --delisted --execute \
  --report artifacts/extras/sp500_history.json
run "5/5 数据目录" .venv/bin/usr-catalog
echo "完成。回到 Claude 说一声即可。"
