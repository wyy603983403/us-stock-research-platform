#!/usr/bin/env bash
# 在 Mac 上运行：把“交易流水线”部署到美国云服务器（研究数据不上传，服务器只下 SPY/SSO/BIL 与利率）。
#   bash scripts/server_deploy.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519
# 可重复运行：代码每次更新；服务器上的订单、账本、.env 不会被覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:?用法：bash scripts/server_deploy.sh root@服务器IP 私钥路径}"
KEY="${2:?缺少私钥路径}"
APP=/opt/usr-trade/app
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")
RSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"

echo "== 1/4 上传代码"
"${SSH[@]}" "mkdir -p $APP /opt/usr-trade/state; for p in rsync curl; do command -v \$p >/dev/null || \
  (apt-get update -qq && apt-get install -y -qq \$p) >/dev/null 2>&1 || yum install -y -q \$p >/dev/null 2>&1 || dnf install -y -q \$p >/dev/null; done"
rsync -az --delete -e "$RSH" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude 'artifacts' \
  --exclude 'orders' --exclude 'portfolio' --exclude 'logs' --exclude 'data' --exclude '.env' \
  --exclude '.usr_sync_*' --exclude 'backup_*' \
  src scripts configs research docs pyproject.toml README.md AGENTS.md "$HOST:$APP/"

echo "== 2/4 迁移已批准策略的订单与账本（服务器已有的不覆盖）"
for d in orders/sp500_trend_voltarget portfolio/rehearsal portfolio/paper; do
  [ -d "$d" ] || continue
  "${SSH[@]}" "mkdir -p /opt/usr-trade/state/$d"
  rsync -az --ignore-existing -e "$RSH" "$d/" "$HOST:/opt/usr-trade/state/$d/"
done

echo "== 3/4 密钥（只在服务器还没有 .env 时创建；只带 Alpaca 与 SEC 联系方式）"
if ! "${SSH[@]}" "test -f /opt/usr-trade/.env"; then
  { grep -E '^(ALPACA_|SEC_USER_AGENT=)' .env || true
    echo "USR_STORAGE_ROOT=/opt/usr-trade/data"; } \
    | "${SSH[@]}" "umask 077; cat > /opt/usr-trade/.env"
fi
# 飞书机器人：Mac 的 .env 里有、服务器上还没有（或已改）的，同步过去
for k in FEISHU_WEBHOOK FEISHU_SECRET; do
  v=$(grep -E "^$k=" .env 2>/dev/null | head -n1 | cut -d= -f2- || true)
  [ -n "$v" ] || continue
  printf '%s=%s\n' "$k" "$v" | "${SSH[@]}" "umask 077; f=/opt/usr-trade/.env; t=\$(mktemp); \
    grep -v '^$k=' \$f > \$t; cat >> \$t; cat \$t > \$f; rm -f \$t"
  echo "  已同步 $k"
done

echo "== 4/4 服务器端安装（环境、数据、定时任务、通知）"
"${SSH[@]}" "bash $APP/scripts/server_setup.sh"
# 从此由服务器运行“均线 + 波动率目标”，Mac 的每日更新不再重复出单/记账（删掉这个文件即恢复）
mkdir -p portfolio && echo "$HOST $(date '+%F %T')" > portfolio/.vt_on_server
echo "Mac 端已停止该策略的出单与记账（标记文件 portfolio/.vt_on_server）"
echo "== 发一条测试通知"
"${SSH[@]}" "cd $APP && sudo -u usrtrade bash scripts/notify.sh --test 2>&1 || bash scripts/notify.sh --test"
