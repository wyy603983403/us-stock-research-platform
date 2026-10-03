#!/usr/bin/env bash
# 在 Mac 上打开云服务器的监控页面：建一条 SSH 隧道（页面不对公网开放），然后用浏览器打开。
#   bash scripts/open_dashboard.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519
# 关闭：在这个终端按 Ctrl+C。
set -euo pipefail
HOST="${1:?用法：bash scripts/open_dashboard.sh root@服务器IP 私钥路径}"
KEY="${2:?缺少私钥路径}"
PORT=8787
( sleep 2; open "http://localhost:$PORT/dashboard.html" ) &
echo "隧道已建立：http://localhost:$PORT/dashboard.html （Ctrl+C 关闭）"
exec ssh -i "$KEY" -N -o ExitOnForwardFailure=yes -L "$PORT:127.0.0.1:$PORT" "$HOST"
