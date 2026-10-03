#!/usr/bin/env bash
# 在 Mac 上运行：给服务器上的监控页面开一个 HTTPS + 密码的网页入口（手机、任何电脑都能打开）。
#   bash scripts/server_web.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519            # 用 IP（自签证书）
#   bash scripts/server_web.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519 你的域名    # 用域名（自动正式证书）
# 先要部署过（scripts/server_deploy.sh）。可重复运行，用于改密码或换域名。密码在本机输入，不回显，服务器只存哈希。
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:?用法：bash scripts/server_web.sh root@服务器IP 私钥路径 [域名] [端口]}"
KEY="${2:?缺少私钥路径}"
SITE="${3:-${HOST#*@}}"; PORT="${4:-443}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")
RSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"

read -r -p "网页登录用户名 [usr]: " U; U="${U:-usr}"
while :; do
  read -r -s -p "网页登录密码（至少 10 位）: " P; echo
  read -r -s -p "再输一次: " P2; echo
  [ "$P" = "$P2" ] && [ ${#P} -ge 10 ] && break
  echo "两次不一致或少于 10 位，请重输"
done

rsync -az -e "$RSH" scripts/server_web_setup.sh "$HOST:/opt/usr-trade/app/scripts/"
printf '%s\n%s\n' "$U" "$P" | "${SSH[@]}" "bash /opt/usr-trade/app/scripts/server_web_setup.sh '$SITE' '$PORT'"
unset P P2
echo
echo "如果打不开：到云服务商控制台的“安全组/防火墙”放行 TCP $PORT（用域名时还要放行 80）。"
[[ "$SITE" =~ ^[0-9.]+$ ]] && echo "用 IP 访问时浏览器会提示证书不受信任（自签证书），选择“继续访问”即可；有域名可换成正式证书。"
