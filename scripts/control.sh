#!/usr/bin/env bash
# 在 Mac 上运行：紧急停止 / 恢复交易（模拟盘下单与将来的嘉信自动下单；清单照常生成）。
#   bash scripts/control.sh stop      # 也可以在手机通知里点“紧急停止”，或往指令频道发“停止”
#   bash scripts/control.sh resume    # 恢复只能在这里做（手机不能恢复）
#   bash scripts/control.sh status
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=$(cut -d' ' -f1 portfolio/.vt_on_server 2>/dev/null || true)
[ -n "$HOST" ] || { echo "找不到服务器地址（portfolio/.vt_on_server）"; exit 1; }
KEY="${USR_KEY:-$HOME/.ssh/evunea_deploy_ed25519}"
F=/opt/usr-trade/state/portfolio/STOP_TRADING
case "${1:-status}" in
  stop)   ssh -i "$KEY" "$HOST" "su -s /bin/bash usrtrade -c 'echo stopped from mac \$(date -u +%FT%TZ) > $F'" && echo "⛔ 已紧急停止" ;;
  resume) read -r -p "确认恢复交易（模拟盘下单、自动下单开关打开时的嘉信下单）？[y/N] " ok; [ "$ok" = y ] || exit 0
          ssh -i "$KEY" "$HOST" "rm -f $F" && echo "已恢复" ;;
  status) ssh -i "$KEY" "$HOST" "[ -f $F ] && { echo '⛔ 停止中：'; cat $F; } || echo 运行中（未停止）; grep -c . /opt/usr-trade/state/logs/commands.log 2>/dev/null | sed 's/^/手机指令记录条数：/'" ;;
  *) echo "用法：bash scripts/control.sh stop|resume|status"; exit 1 ;;
esac
