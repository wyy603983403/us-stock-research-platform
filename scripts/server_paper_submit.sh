#!/usr/bin/env bash
# 服务器（usrtrade）：纽约时间交易日 15:38 由 usr-paper.timer 调用（configs/paper_broker.yml order_timing: before_close）。
# 把最新一份通过独立复核的清单以市价单提交到 Alpaca **模拟**账户（收盘前 5-25 分钟；休市或提前收盘的日子不发）。
# 成功只写 artifacts/paper_last_submit.txt（第二天早上的通知里带上）；被拒、对账不一致或出错立即通知。
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs artifacts
LOG=logs/paper_submit_$(date +%Y%m%d).log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T %Z') 模拟盘收盘前提交"
PAPER_ON=$(.venv/bin/python -c "import yaml;print(bool((yaml.safe_load(open('configs/paper_broker.yml')) or {}).get('enabled')))")
[ "$PAPER_ON" = "True" ] || { echo "模拟盘未打开"; exit 0; }
if [ -f portfolio/STOP_TRADING ]; then echo "紧急停止中，不发模拟单"; exit 0; fi
OUT=$(.venv/bin/usr-paper --submit --near-close 2>&1 | tail -n 20)
echo "$OUT"
DONE=$(echo "$OUT" | grep -m1 '^模拟单已提交' || true)
if [ -n "$DONE" ]; then
  echo "纽约 $(TZ=America/New_York date '+%m-%d %H:%M') ${DONE}" > artifacts/paper_last_submit.txt
fi
case "$OUT" in *拒绝*|*Traceback*|*Error*|*对账不一致*)
  bash scripts/notify.sh "⚠️ 模拟盘收盘前提交" "$(echo "$OUT" | grep -m3 -E '拒绝|Error|对账不一致|Traceback')";;
esac
exit 0
