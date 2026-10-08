#!/usr/bin/env bash
# 在 Mac 上运行：个股板块（自己挑、手动下单，系统记账和提醒）。命令在服务器上执行，账本存在服务器并自动备份。
#   bash scripts/stocks.sh init 5000                          # 建账（只做一次；另外的钱，不占策略资金）
#   bash scripts/stocks.sh screen                             # 看最新一周的候选清单
#   bash scripts/stocks.sh plan NVDA BUY 5 180                # 下单前检查：之后占比、是否超 25%
#   bash scripts/stocks.sh buy NVDA 5 179.60 [手续费] [日期]   # 成交后记录（日期默认今天，格式 2026-11-02）
#   bash scripts/stocks.sh sell NVDA 2 190 [手续费] [日期]
#   bash scripts/stocks.sh cash 3.21 "NVDA 分红（税后）"        # 分红、费用；入金/出金加 deposit：cash 2000 "追加" deposit
#   bash scripts/stocks.sh split NVDA 10                      # 拆股（10 拆 1 填 10）
#   bash scripts/stocks.sh status | show
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=$(cut -d' ' -f1 portfolio/.vt_on_server 2>/dev/null || true)
[ -n "$HOST" ] || { echo "找不到服务器地址（portfolio/.vt_on_server）"; exit 1; }
KEY="${USR_KEY:-$HOME/.ssh/evunea_deploy_ed25519}"
run() { ssh -i "$KEY" "$HOST" "cd /opt/usr-trade/app && su -s /bin/bash usrtrade -c '$*'"; }
sym() { printf '%s' "$1" | tr -cd 'A-Za-z0-9.-'; }
num() { printf '%s' "$1" | tr -cd '0-9.-'; }
cmd="${1:-status}"; shift || true
case "$cmd" in
  init)  [ -n "${1:-}" ] || { echo "用法：bash scripts/stocks.sh init 金额"; exit 1; }
         read -r -p "确认：个股板块入金 \$$1，建立账本？[y/N] " ok; [ "$ok" = y ] || exit 0
         run ".venv/bin/usr-stocks init --cash $(num "$1")" ;;
  plan)  [ $# -ge 4 ] || { echo "用法：bash scripts/stocks.sh plan 代码 BUY/SELL 股数 价格"; exit 1; }
         run ".venv/bin/usr-stocks plan --symbol $(sym "$1") --side $(sym "$2") --qty $(num "$3") --price $(num "$4")" ;;
  buy|sell) [ $# -ge 3 ] || { echo "用法：bash scripts/stocks.sh $cmd 代码 股数 成交价 [手续费] [日期]"; exit 1; }
         day=""; [ -n "${5:-}" ] && day="--day $(num "$5")"
         run ".venv/bin/usr-stocks $cmd --symbol $(sym "$1") --qty $(num "$2") --price $(num "$3") --fee $(num "${4:-0}") $day" ;;
  cash)  [ $# -ge 2 ] || { echo "用法：bash scripts/stocks.sh cash 金额 说明 [deposit]"; exit 1; }
         note=$(printf '%s' "$2" | tr -d "'\"")
         dep=""; [ "${3:-}" = deposit ] && dep="--deposit"
         run ".venv/bin/usr-stocks cash --amount $(num "$1") --note \"$note\" $dep" ;;
  split) [ $# -ge 2 ] || { echo "用法：bash scripts/stocks.sh split 代码 比例"; exit 1; }
         run ".venv/bin/usr-stocks split --symbol $(sym "$1") --ratio $(num "$2")" ;;
  screen) ssh -i "$KEY" "$HOST" "f=\$(ls -1 /opt/usr-trade/state/artifacts/stocks/screen_*.md 2>/dev/null | tail -n 1); [ -n \"\$f\" ] && cat \"\$f\" || echo 还没有候选清单（每周五信号日那次运行生成）" ;;
  status) ssh -i "$KEY" "$HOST" "cat /opt/usr-trade/state/artifacts/stocks/status.md 2>/dev/null || echo 还没有个股状态（建账后下一次运行生成）" ;;
  show)  run ".venv/bin/usr-stocks show" ;;
  *) echo "未知命令 $cmd"; exit 1 ;;
esac
