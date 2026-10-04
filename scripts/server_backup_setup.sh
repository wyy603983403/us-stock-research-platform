#!/usr/bin/env bash
# 在 Mac 上运行：给服务器开启“运行记录备份 + 漏跑报警”。先在 GitHub 新建一个空的私有仓库（如 us-stock-ops）。
#   1) bash scripts/server_backup_setup.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519 git@github.com:你的用户名/us-stock-ops.git
#      → 服务器生成专用密钥并显示公钥；到仓库 Settings → Deploy keys → Add deploy key 粘贴，勾选 Allow write access
#   2) 再运行一次同一条命令：测试推送（成功后每天自动备份）
# 这把密钥只能写这一个仓库，不能访问你的其他仓库。
set -euo pipefail
HOST="${1:?用法：bash scripts/server_backup_setup.sh root@服务器IP 私钥路径 git@github.com:用户名/仓库.git}"
KEY="${2:?缺少私钥路径}"
REPO="${3:?缺少仓库地址（git@github.com:用户名/仓库.git）}"
case "$REPO" in git@github.com:*/*.git) ;; *) echo "仓库地址应形如 git@github.com:用户名/仓库.git"; exit 1;; esac
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")
cd "$(dirname "$0")/.."
rsync -az -e "ssh -i $KEY" scripts/server_backup.sh "$HOST:/opt/usr-trade/app/scripts/"
rsync -az -e "ssh -i $KEY" ops/ "$HOST:/opt/usr-trade/app/ops/"
"${SSH[@]}" bash -s -- "$REPO" <<'REMOTE'
set -euo pipefail
REPO="$1"; BASE=/opt/usr-trade; U=usrtrade; H=$BASE/home
command -v git >/dev/null || (dnf install -y -q git || yum install -y -q git || (apt-get update -qq && apt-get install -y -qq git)) >/dev/null 2>&1
f=$BASE/.env; t=$(mktemp); grep -v '^OPS_REPO=' $f > $t || true; echo "OPS_REPO=$REPO" >> $t; cat $t > $f; rm -f $t
mkdir -p $BASE/backup && chown $U:$U $BASE/backup
if [ ! -f $H/.ssh/ops_backup_ed25519 ]; then
  su -s /bin/bash $U -c "mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -q -t ed25519 -N '' -C usr-trade-ops-backup -f ~/.ssh/ops_backup_ed25519"
  echo ""
  echo "== 第一步：把下面这行公钥加到仓库 Settings → Deploy keys → Add deploy key（勾选 Allow write access）："
  echo ""
  cat $H/.ssh/ops_backup_ed25519.pub
  echo ""
  echo "加好后再运行一次同一条命令进行测试推送。"
  exit 0
fi
echo "== 测试推送"
su -s /bin/bash $U -c "cd $BASE/app && bash scripts/server_backup.sh"
REMOTE
