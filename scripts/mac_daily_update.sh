#!/usr/bin/env bash
# 每日增量更新（定时任务每天北京时间 07:30 调用）：
#   1 日线：给库里所有标的补最新交易日；分红/拆股导致复权价被雅虎重述时自动整段重下
#   2 宏观序列（FRED）
#   3 分钟线（Alpaca，从上次最后一根续传，只到已收盘的交易日）
#   4 每周一：拆股记录、Fama-French 因子
#   5 质量门禁 + 数据目录
#   6 每月最后一个交易日之后：趋势基线订单意向演练（只写文件，不下单）
#   7 每个交易日：均线 + 波动率目标（用户 2026-10-03 批准；需要调仓才出订单；先记账再出单）
# 任何一步失败都不影响后面各步；汇总见 logs/daily_update.log。
#   bash scripts/mac_daily_update.sh            # 更新全部
#   bash scripts/mac_daily_update.sh --dry-run  # 只看日线会做什么，不写入（其余步骤跳过）
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
# 数据卷没挂载就不更新，避免悄悄写到别处
ROOT="$(grep '^USR_STORAGE_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "数据卷未挂载：$ROOT，已跳过更新"; exit 0; fi

mkdir -p artifacts/universe artifacts/intraday artifacts/extras logs
STAMP="$(date +%Y%m%d)"
FLAG="--execute"; [ "${1:-}" = "--dry-run" ] && FLAG=""
.venv/bin/usr-update $FLAG --report "artifacts/universe/update_${STAMP}.json" > /dev/null || true
.venv/bin/python - <<PY
import json
r = json.load(open("artifacts/universe/update_${STAMP}.json"))
print(f"{r['date']}：{r['symbols']} 只标的，新增交易日 {r['appended_days']}，"
      f"整段重下 {len(r['refreshed'])} 只，失败 {len(r['failed'])} 只，质量错误 {len(r['quality_errors'])} 只"
      f"（不含已隔离 {len(r.get('quarantined', []))} 只）")
for x in r["refreshed"][:10]:
    print("  重下", x["symbol"], "-", x["reason"])
for s, e in list(r["quality_errors"].items())[:10]:
    print("  质量", s, e[:1])
for s, e in list(r["failed"].items())[:10]:
    print("  失败", s, e[:80])
behind = r.get("behind", [])
if behind:
    print(f"  注意：{len(behind)} 只没拿到 {r['date']} 的数据（数据源延迟或限流，下次运行会补）：",
          " ".join(behind[:15]) + (" …" if len(behind) > 15 else ""))
PY

[ -z "$FLAG" ] && exit 0
echo "== 宏观序列"
.venv/bin/usr-collect-macro --execute --report "artifacts/universe/macro_${STAMP}.json" >/dev/null \
  && echo "  完成" || echo "  部分失败，见 artifacts/universe/macro_${STAMP}.json"

if grep -q '^ALPACA_KEY_ID=' .env 2>/dev/null; then
  echo "== 分钟线（Alpaca）"
  .venv/bin/usr-collect-intraday-alpaca --all-stored --execute \
    --report "artifacts/intraday/alpaca_${STAMP}.json" 2>>logs/daily_intraday.err | tail -n 8 || true
fi

if [ "$(date +%u)" = "1" ]; then
  echo "== 每周：拆股记录、因子"
  .venv/bin/usr-collect-splits --yahoo-delisted --execute --report artifacts/extras/splits.json 2>/dev/null | tail -n 4 || true
  .venv/bin/usr-collect-factors --execute --report artifacts/extras/factors.json >/dev/null \
    && echo "  因子已更新" || echo "  因子下载失败"
fi

echo "== 质量门禁与数据目录"
ALL=$(.venv/bin/python -c "from us_stock_research.config import load_settings; from us_stock_research.storage import open_store; print(' '.join(open_store(load_settings()).symbols()))")
.venv/bin/usr-audit-ohlcv $ALL --output artifacts/quality/universe.json >/dev/null \
  && echo "  日线全部通过" || echo "  有标的未通过（含已隔离），见 artifacts/quality/universe.json"
.venv/bin/usr-catalog >/dev/null && echo "  数据目录已更新：artifacts/catalog.md"

# 月末：上一个已收盘交易日是当月最后一个交易日时，生成一次订单意向演练
ASOF=$(.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
from us_stock_research.calendar import is_trading_day
from us_stock_research.quality.intraday import last_closed_session
d = last_closed_session(datetime.now(UTC))
nxt = d + timedelta(days=1)
while not is_trading_day(nxt):
    nxt += timedelta(days=1)
print(d.isoformat() if nxt.month != d.month else "")
PY
)
# 演练账本：上月订单按成交日收盘价模拟记账（成交日收盘后才会记）
LEDGER=portfolio/rehearsal/etf_trend_baseline.yml
if ls orders/etf_trend_baseline/*.json >/dev/null 2>&1; then
  .venv/bin/usr-rehearsal-fill --study etf_trend_baseline --since 2026-09-30 2>&1 | tail -n 3 \
    || echo "  演练记账失败"
fi
if [ -n "$ASOF" ] && [ ! -f "orders/etf_trend_baseline/${ASOF}.json" ]; then
  echo "== 月末订单意向演练（${ASOF}，只写文件）"
  HOLD=""; [ -f "$LEDGER" ] && HOLD="--holdings $LEDGER"
  .venv/bin/usr-order-intent --contract research/etf-trend-baseline/study.yml --as-of "$ASOF" $HOLD \
    | tail -n 3 || echo "  生成失败"
fi
# 每个交易日：SPY 200 日均线 + 2 倍杠杆的演练（先按今天收盘记昨天的订单，再按今天收盘出新订单）
LASTDAY=$(.venv/bin/python -c "from datetime import UTC, datetime
from us_stock_research.quality.intraday import last_closed_session
print(last_closed_session(datetime.now(UTC)).isoformat())")
echo "== 均线 + 波动率目标（已批准，${LASTDAY}，只写文件）"
.venv/bin/usr-rehearsal-fill --study sp500_trend_voltarget --cost-bps 10 2>&1 | tail -n 3 \
  || echo "  演练记账失败"
LT_LEDGER=portfolio/rehearsal/sp500_trend_voltarget.yml
PAPER_ON=$(.venv/bin/python -c "import yaml;print(bool((yaml.safe_load(open('configs/paper_broker.yml')) or {}).get('enabled')))" 2>/dev/null)
if [ "$PAPER_ON" = "True" ]; then
  # 阶段 2：持仓以 Alpaca 模拟账户为准（先对账，不一致则不发新单）
  echo "== 模拟盘（Alpaca paper）对账"
  .venv/bin/usr-paper --sync 2>&1 | tail -n 5
  LT_LEDGER=portfolio/paper/sp500_trend_voltarget.yml
fi
LT_HOLD=""; [ -f "$LT_LEDGER" ] && LT_HOLD="--holdings $LT_LEDGER"
# 2026-10-03 起改为用户批准的“均线 + 波动率目标”（SPY/SSO/BIL）；熔断线 40% 为用户决定
.venv/bin/usr-lt-intent --contract research/sp500-trend-voltarget/study.yml --as-of "$LASTDAY" \
  --signal-symbol SPY --one-x SPY --risk-on SSO --risk-off BIL --breaker 0.40 $LT_HOLD 2>&1 \
  | tail -n 1 || echo "  生成失败"
if [ "$PAPER_ON" = "True" ]; then
  echo "== 模拟盘下单（收盘竞价单，次日收盘成交）"
  .venv/bin/usr-paper --submit 2>&1 | tail -n 12
fi
echo "完成 $(date '+%Y-%m-%d %H:%M')"
