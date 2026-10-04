#!/usr/bin/env bash
# 服务器每日流水线（usrtrade 用户，由 systemd 定时器调用；可重复运行）：
#   策略：vt_plus_defensive（用户 2026-10-04 批准；50% 标普趋势 + 波动率目标，50% TLT/IEF/GLD 趋势）
#   数据（SPY/SSO/BIL/TLT/IEF/GLD + FRED）→ 演练记账 →（模拟盘打开时：对账）→ 出单 →（模拟盘打开时：发收盘竞价单）→ 状态 → 通知
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date +%Y%m%d)"
LOG=logs/daily_$STAMP.log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T %Z') 开始"
.venv/bin/usr-update SPY SSO BIL TLT IEF GLD --execute --report "artifacts/update_$STAMP.json" >/dev/null || true
.venv/bin/python - <<PY
import json
r = json.load(open("artifacts/update_$STAMP.json"))
print(f"数据 {r['date']}：失败 {len(r['failed'])}，未拿到最新 {len(r.get('behind', []))}，质量错误 {len(r['quality_errors'])}")
PY
.venv/bin/usr-collect-macro --execute >/dev/null 2>&1 || echo "FRED 部分失败"
STUDY=vt_plus_defensive
CONTRACT=research/vt-plus-defensive/study.yml
MODEL_START=2026-10-02                      # 模型重放起点 = 本组合首份订单的信号日（不要改）
LEDGER=portfolio/rehearsal/$STUDY.yml
.venv/bin/usr-rehearsal-fill --study $STUDY --cost-bps 10 2>&1 | tail -n 2
PAPER_ON=$(.venv/bin/python -c "import yaml;print(bool((yaml.safe_load(open('configs/paper_broker.yml')) or {}).get('enabled')))")
if [ "$PAPER_ON" = "True" ]; then
  .venv/bin/usr-paper --sync 2>&1 | tail -n 5
  LEDGER=portfolio/paper/$STUDY.yml
fi
HOLD=""; [ -f "$LEDGER" ] && HOLD="--holdings $LEDGER"
LASTDAY=$(.venv/bin/python -c "from datetime import UTC, datetime
from us_stock_research.quality.intraday import last_closed_session
print(last_closed_session(datetime.now(UTC)).isoformat())")
LT_OUT=$(.venv/bin/usr-mix-intent --contract $CONTRACT --as-of "$LASTDAY" --model-start $MODEL_START \
  --breaker 0.40 $HOLD 2>&1 | tail -n 1) || LT_OUT="生成失败"
echo "$LT_OUT"
if [ "$PAPER_ON" = "True" ]; then .venv/bin/usr-paper --submit 2>&1 | tail -n 12; fi
ST_OUT=$(.venv/bin/usr-status --contract $CONTRACT --model-start $MODEL_START --holdings "$LEDGER" \
  --update-report "artifacts/update_$STAMP.json" 2>&1); ST_RC=$?
echo "$ST_OUT"
NOTE=""
case "$LT_OUT" in *"订单 0 笔"*) ;; *"订单"*) NOTE="有新订单：$LT_OUT";; *) NOTE="订单生成异常：$LT_OUT";; esac
case "$LT_OUT" in *"只减仓"*) NOTE="只减仓：$LT_OUT";; esac
[ "$ST_RC" = "3" ] && NOTE="${NOTE:+$NOTE
}$(echo "$ST_OUT" | grep '需要关注')"
# 每个交易日都发一条简报（只发一次：第二次运行仅在有异常时发）
FIRST=artifacts/.notified_$STAMP
if [ -n "$NOTE" ] || [ ! -f "$FIRST" ]; then
  bash scripts/notify.sh "美股交易系统 $LASTDAY" "${NOTE:-一切正常}
$(echo "$ST_OUT" | head -n 1)"
  touch "$FIRST"
fi
echo "== $(date '+%F %T %Z') 完成"
