#!/usr/bin/env bash
# 服务器（usrtrade）：纽约时间交易日 09:50 由 usr-submit.timer 调用。把最新一份实盘清单交给嘉信接口提交。
# 默认什么都不做：configs/schwab_api.yml 的 orders_enabled 为 false 时 usr-schwab 只打印“未提交：…”。
set -uo pipefail
cd "$(dirname "$0")/.."
LOG=logs/submit_$(date +%Y%m%d).log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T %Z') 提交检查"
STUDY=$(.venv/bin/python -c "import yaml;print(yaml.safe_load(open('configs/operating.yml'))['study'])")
LATEST=$(ls -1 orders/live/$STUDY/*.json 2>/dev/null | grep -v rejected | tail -n 1)
[ -n "$LATEST" ] || { echo "没有实盘清单"; exit 0; }
OUT=$(.venv/bin/usr-schwab submit --intent "$LATEST" 2>&1 | tail -n 1)
echo "$OUT"
case "$OUT" in 未提交：自动下单未打开*|未提交：*不是该清单的执行日*|未提交：*已经提交过*|没有实盘清单) ;;
  *) bash scripts/notify.sh "嘉信自动下单" "$OUT" ;;
esac
