#!/usr/bin/env bash
# 服务器每日流水线（usrtrade 用户，由 systemd 定时器调用；可重复运行）：
#   策略：configs/operating.yml（2026-10-08 起 vt_only = 只跑标普趋势 + 波动率目标；此前 vt_plus_defensive 50/50）
#   数据（SPY/SSO/BIL/TLT/IEF/GLD + FRED）→ 演练记账 →（模拟盘打开时：对账）→ 出单 →（模拟盘打开时：发收盘竞价单）→ 状态 → 通知
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date +%Y%m%d)"
LOG=logs/daily_$STAMP.log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T %Z') 开始"
.venv/bin/usr-update SPY SSO BIL TLT IEF GLD --alpaca-backup --execute --report "artifacts/update_$STAMP.json" >/dev/null || true
.venv/bin/python - <<PY
import json
r = json.load(open("artifacts/update_$STAMP.json"))
print(f"数据 {r['date']}：失败 {len(r['failed'])}，未拿到最新 {len(r.get('behind', []))}，质量错误 {len(r['quality_errors'])}"
      f"，备用源补齐 {sum(1 for n in r.get('fallback', {}).values() if n)}，核对 {len(r.get('crosscheck', {}))} 只"
      f"（不一致 {len(r.get('crosscheck_mismatch', []))}）")
PY
.venv/bin/usr-collect-macro --execute >/dev/null 2>&1 || echo "FRED 部分失败"
OPS=$(.venv/bin/python -c "import yaml;c=yaml.safe_load(open('configs/operating.yml'));print(c['study'],c['contract'],c['model_start'])")
read -r STUDY CONTRACT MODEL_START <<< "$OPS"   # 正在运行的策略与模型重放起点（configs/operating.yml）
[ -n "$MODEL_START" ] || { echo "读取 configs/operating.yml 失败"; bash scripts/notify.sh "美股交易系统" "⚠️ 读取 configs/operating.yml 失败，今天未运行"; exit 1; }
LEDGER=portfolio/rehearsal/$STUDY.yml
PAPER_ON=$(.venv/bin/python -c "import yaml;print(bool((yaml.safe_load(open('configs/paper_broker.yml')) or {}).get('enabled')))")
# 模拟盘打开后持仓以模拟账户为准，演练账本冻结（不再记账）
[ "$PAPER_ON" = "True" ] || .venv/bin/usr-rehearsal-fill --study $STUDY --cost-bps 10 2>&1 | tail -n 2
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
# 新订单清单：另一套代码独立复核，核对表写进清单 .md，结果附在通知里
VERIFY=""
if [ -f "orders/$STUDY/$LASTDAY.json" ] && ! grep -q "自动独立复核" "orders/$STUDY/$LASTDAY.md" 2>/dev/null; then
  VERIFY=$(.venv/bin/usr-verify-intent --intent "orders/$STUDY/$LASTDAY.json" --contract $CONTRACT $HOLD 2>&1 | tail -n 1)
  echo "$VERIFY"
  case "$VERIFY" in 独立复核：一致) ;; *)
    # 复核不一致或复核本身出错：清单改名搁置，不记账、不发模拟单，等人工处理
    mv "orders/$STUDY/$LASTDAY.json" "orders/$STUDY/$LASTDAY.json.rejected"
    echo "已搁置 orders/$STUDY/$LASTDAY.json（复核未通过）";;
  esac
fi
STOPPED=""; [ -f portfolio/STOP_TRADING ] && STOPPED=1   # 紧急停止（手机“停止”或 scripts/control.sh stop）
if [ "$PAPER_ON" = "True" ] && [ -z "$STOPPED" ]; then .venv/bin/usr-paper --submit 2>&1 | tail -n 12; fi
# 嘉信接口（只读，configs/schwab_api.yml read_enabled）：先把今天的成交记入账本，再对账；授权快到期时提醒
SW_READ=$(.venv/bin/python -c "import yaml;print(bool((yaml.safe_load(open('configs/schwab_api.yml')) or {}).get('read_enabled')))" 2>/dev/null)
SW_FILLS=""; SW_SNAP=""
if [ "$SW_READ" = "True" ]; then
  SW_FILLS=$(.venv/bin/usr-schwab record-fills --day "$LASTDAY" 2>&1 | tail -n 1); echo "$SW_FILLS"
  SW_SNAP=$(.venv/bin/usr-schwab snapshot 2>&1 | tail -n 1); echo "$SW_SNAP"
fi
SW_TOKEN=$(.venv/bin/usr-schwab status --line 2>/dev/null | tail -n 1)
# 实盘（嘉信国际账户，用户在 App 手动下单）：账本存在且信号日不早于 configs/live.yml 的 start_signal_day 时出清单
LIVE_OUT=""; LIVE_VERIFY=""; LIVE_ST=""
LIVE_START=$(.venv/bin/python -c "import yaml;print((yaml.safe_load(open('configs/live.yml')) or {}).get('start_signal_day') or '')" 2>/dev/null)
LIVE_BUFFER=$(.venv/bin/python -c "import yaml;print((yaml.safe_load(open('configs/live.yml')) or {}).get('cash_buffer') or 0)" 2>/dev/null)
if [ -f portfolio/live/schwab.yml ] && [ -n "$LIVE_START" ] && [[ ! "$LASTDAY" < "$LIVE_START" ]]; then
  LIVE_OUT=$(.venv/bin/usr-mix-intent --contract $CONTRACT --as-of "$LASTDAY" --model-start $MODEL_START \
    --breaker 0.40 --cash-buffer "${LIVE_BUFFER:-0}" --holdings portfolio/live/schwab.yml --out-dir orders/live 2>&1 | tail -n 1) || LIVE_OUT="生成失败"
  echo "实盘：$LIVE_OUT"
  if [ -f "orders/live/$STUDY/$LASTDAY.json" ] && ! grep -q "自动独立复核" "orders/live/$STUDY/$LASTDAY.md" 2>/dev/null; then
    LIVE_VERIFY=$(.venv/bin/usr-verify-intent --intent "orders/live/$STUDY/$LASTDAY.json" --contract $CONTRACT \
      --holdings portfolio/live/schwab.yml 2>&1 | tail -n 1)
    echo "实盘$LIVE_VERIFY"
    case "$LIVE_VERIFY" in 独立复核：一致) ;; *)
      mv "orders/live/$STUDY/$LASTDAY.json" "orders/live/$STUDY/$LASTDAY.json.rejected";;
    esac
  fi
  LIVE_ST=$(.venv/bin/usr-status --contract $CONTRACT --model-start $MODEL_START --holdings portfolio/live/schwab.yml \
    --output artifacts/live/status.md --orders-dir orders/live 2>&1 | head -n 1)
fi
# 个股板块（用户自己挑、手动下单）：每日估值与提醒；每周五信号日生成一次候选清单
STK_OUT=$(.venv/bin/usr-stocks monitor 2>&1 | tail -n 1)
echo "$STK_OUT"
STK_SCREEN=""
if [ "$(date -d "$LASTDAY" +%u)" = "5" ] && [ ! -f "artifacts/stocks/screen_$LASTDAY.md" ]; then
  STK_SCREEN=$(.venv/bin/usr-stocks screen --day "$LASTDAY" 2>&1 | tail -n 1)
  echo "$STK_SCREEN"
fi
ST_OUT=$(.venv/bin/usr-status --contract $CONTRACT --model-start $MODEL_START --holdings "$LEDGER" \
  --update-report "artifacts/update_$STAMP.json" 2>&1); ST_RC=$?
echo "$ST_OUT"
NOTE=""
case "$LT_OUT" in *"订单 0 笔"*) ;; *"订单"*) NOTE="有新订单：$LT_OUT";; *) NOTE="订单生成异常：$LT_OUT";; esac
case "$LT_OUT" in *"只减仓"*) NOTE="只减仓：$LT_OUT";; esac
[ -n "$VERIFY" ] && NOTE="${NOTE:+$NOTE
}$VERIFY"
case "$VERIFY" in ""|独立复核：一致) ;; *) NOTE="⚠️ 独立复核未通过，该清单已搁置（不记账、不下模拟单）
$NOTE";; esac
case "$LIVE_OUT" in ""|*"订单 0 笔"*) ;; *)
  NOTE="${NOTE:+$NOTE
}【实盘·嘉信手动】$LIVE_OUT（以最新清单为准，之前未执行的清单作废；$([ "$SW_READ" = "True" ] && echo "成交会自动记账" || echo "成交后用 scripts/live.sh fill 记录")）";; esac
case "$LIVE_VERIFY" in ""|独立复核：一致) ;; *) NOTE="⚠️ 实盘清单独立复核未通过，已搁置，请勿下单
$NOTE";; esac
[ -n "$LIVE_ST" ] && NOTE="${NOTE:+$NOTE
}实盘账户：$LIVE_ST"
case "$SW_FILLS" in *实盘*|*个股*|*未记录*) NOTE="${NOTE:+$NOTE
}$SW_FILLS";; esac
case "$SW_SNAP" in *不一致*|*出错*|*授权*) NOTE="${NOTE:+$NOTE
}⚠️ 嘉信对账：$SW_SNAP";; esac
[ -n "$SW_TOKEN" ] && NOTE="${NOTE:+$NOTE
}$SW_TOKEN"
case "$STK_OUT" in *提醒*) NOTE="${NOTE:+$NOTE
}【个股】$STK_OUT";; esac
[ -n "$STK_SCREEN" ] && NOTE="${NOTE:+$NOTE
}【个股】$STK_SCREEN（bash scripts/stocks.sh screen 查看）"
[ -n "$STOPPED" ] && NOTE="⛔ 紧急停止中：清单照常生成，但不发模拟单、不自动下单（恢复：Mac 上 bash scripts/control.sh resume）
$NOTE"
[ "$ST_RC" = "3" ] && NOTE="${NOTE:+$NOTE
}$(echo "$ST_OUT" | grep '需要关注')"
# 每个交易日都发一条简报（只发一次：第二次运行仅在有异常时发）
FIRST=artifacts/.notified_$STAMP
if [ -n "$NOTE" ] || [ ! -f "$FIRST" ]; then
  bash scripts/notify.sh "美股交易系统 $LASTDAY" "${NOTE:-一切正常}
$(echo "$ST_OUT" | head -n 1)"
  touch "$FIRST"
fi
# 运行记录推送到私有仓库（未配置时跳过）；仓库里的定时检查负责漏跑报警
bash scripts/server_backup.sh 2>&1 | tail -n 1
echo "== $(date '+%F %T %Z') 完成"
