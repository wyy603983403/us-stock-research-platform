#!/usr/bin/env bash
# 在 Mac 上运行：管理实盘（嘉信，手动下单）账本。命令在服务器上执行，账本存在服务器并自动备份。
#   bash scripts/live.sh init 10000                         # 入金后建账（只做一次）
#   bash scripts/live.sh orders                             # 查看最新实盘清单（含自动独立复核表）
#   bash scripts/live.sh fill 2026-11-02 SSO BUY 69 71.80   # 每笔成交后记录：日期 标的 方向 股数 成交价 [手续费]
#   bash scripts/live.sh cash 12.34 "BIL 分红（税后）"         # 分红、利息、费用等现金变动
#   bash scripts/live.sh show                               # 查看账本
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=$(cut -d' ' -f1 portfolio/.vt_on_server 2>/dev/null || true)
[ -n "$HOST" ] || { echo "找不到服务器地址（portfolio/.vt_on_server）"; exit 1; }
KEY="${USR_KEY:-$HOME/.ssh/evunea_deploy_ed25519}"
run() { ssh -i "$KEY" "$HOST" "cd /opt/usr-trade/app && su -s /bin/bash usrtrade -c '$*'"; }
cmd="${1:-show}"; shift || true
case "$cmd" in
  init)  [ -n "${1:-}" ] || { echo "用法：bash scripts/live.sh init 金额"; exit 1; }
         read -r -p "确认：嘉信账户已入金 \$$1，建立实盘账本？[y/N] " ok; [ "$ok" = y ] || exit 0
         run ".venv/bin/usr-live init --cash $1" ;;
  fill)  [ $# -ge 5 ] || { echo "用法：bash scripts/live.sh fill 日期 标的 BUY/SELL 股数 成交价 [手续费]"; exit 1; }
         run ".venv/bin/usr-live fill --day $1 --symbol $2 --side $3 --qty $4 --price $5 --fee ${6:-0}" ;;
  cash)  [ $# -ge 2 ] || { echo "用法：bash scripts/live.sh cash 金额 说明"; exit 1; }
         note=$(printf '%s' "$2" | tr -d "'\"")
         run ".venv/bin/usr-live cash --amount $1 --note \"$note\"" ;;
  orders) ssh -i "$KEY" "$HOST" "f=\$(ls -1 /opt/usr-trade/state/orders/live/vt_plus_defensive/*.md 2>/dev/null | tail -n 1); [ -n \"\$f\" ] && cat \"\$f\" || echo 还没有实盘清单" ;;
  show)  run ".venv/bin/usr-live show" ;;
  *) echo "未知命令 $cmd"; exit 1 ;;
esac
