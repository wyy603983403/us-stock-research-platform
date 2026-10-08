#!/usr/bin/env bash
# 在 Mac 上运行：嘉信接口（服务器上执行；密钥和授权令牌只在服务器上）。
#   bash scripts/schwab.sh login              # 每 7 天一次：打开链接登录嘉信，把跳转后的地址粘贴回来
#   bash scripts/schwab.sh status             # 授权剩余天数、只读/自动下单开关
#   bash scripts/schwab.sh snapshot           # 账户持仓与现金，和账本对账
#   bash scripts/schwab.sh fills [2026-11-02] # 把某天的成交记入账本（每日流水线会自动做）
#   bash scripts/schwab.sh stop | resume      # 紧急停止 / 恢复自动下单（停止文件 portfolio/STOP_TRADING）
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=$(cut -d' ' -f1 portfolio/.vt_on_server 2>/dev/null || true)
[ -n "$HOST" ] || { echo "找不到服务器地址（portfolio/.vt_on_server）"; exit 1; }
KEY="${USR_KEY:-$HOME/.ssh/evunea_deploy_ed25519}"
run() { ssh -i "$KEY" "$HOST" "cd /opt/usr-trade/app && su -s /bin/bash usrtrade -c '$*'"; }
cmd="${1:-status}"; shift || true
case "$cmd" in
  login)
    url=$(run ".venv/bin/usr-schwab auth-url")
    case "$url" in https://*) ;; *) echo "$url"; exit 1;; esac
    echo "1) 在浏览器打开下面的链接，登录嘉信并同意授权："
    echo "$url"
    echo "2) 登录后浏览器会跳到 https://127.0.0.1/?code=... （页面打不开是正常的），复制地址栏里的完整地址"
    read -r -p "3) 粘贴到这里后回车：" redirected
    printf '%s\n' "$redirected" | ssh -i "$KEY" "$HOST" "cd /opt/usr-trade/app && su -s /bin/bash usrtrade -c '.venv/bin/usr-schwab auth-code'" ;;
  status)   run ".venv/bin/usr-schwab status" ;;
  snapshot) run ".venv/bin/usr-schwab snapshot" ;;
  fills)    day=""; [ -n "${1:-}" ] && day="--day $(printf '%s' "$1" | tr -cd '0-9-')"
            run ".venv/bin/usr-schwab record-fills $day" ;;
  stop)     ssh -i "$KEY" "$HOST" "su -s /bin/bash usrtrade -c 'touch /opt/usr-trade/state/portfolio/STOP_TRADING'" && echo "已停止自动下单" ;;
  resume)   ssh -i "$KEY" "$HOST" "su -s /bin/bash usrtrade -c 'rm -f /opt/usr-trade/state/portfolio/STOP_TRADING'" && echo "已解除停止" ;;
  *) echo "未知命令 $cmd"; exit 1 ;;
esac
