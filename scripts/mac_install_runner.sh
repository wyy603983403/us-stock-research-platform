#!/usr/bin/env bash
# 在这台 Mac 上安装 GitHub Actions 自建 runner（用户 2026-10-11：GitHub 免费额度用完，CI 改在本机跑）。
# 只服务本仓库，标签 usr（工作流 runs-on: [self-hosted, macOS, usr]），装成开机自启的后台服务。
#
# 1. 浏览器打开 https://github.com/wyy603983403/us-stock-research-platform/settings/actions/runners/new
#    页面 “Configure” 一节里有一行 ./config.sh --url ... --token XXXXX，复制其中的 token（1 小时内有效，只用于登记）
# 2. bash scripts/mac_install_runner.sh        （运行后按提示粘贴 token，回车；粘贴时屏幕不显示）
#
# 卸载：bash scripts/mac_install_runner.sh --remove <token>（token 同样在上面的页面取，选 Remove 时显示）
# 安全：本仓库是公开的，工作流里已限制 fork 来的 PR 不在本机运行；另建议在仓库 Settings → Actions →
#       “Fork pull request workflows from outside collaborators” 选 “Require approval for all outside collaborators”。
set -euo pipefail
REPO_URL="https://github.com/wyy603983403/us-stock-research-platform"
DIR="${HOME}/actions-runner-us-stock"   # 放在“文稿”之外，避免 macOS 权限弹窗拦住后台服务
NAME="usr-mac-$(scutil --get LocalHostName 2>/dev/null || hostname -s)"

if [ "${1:-}" = "--remove" ]; then
  [ -n "${2:-}" ] || { echo "用法：bash scripts/mac_install_runner.sh --remove <token>"; exit 1; }
  cd "${DIR}"
  ./svc.sh stop 2>/dev/null || true
  ./svc.sh uninstall 2>/dev/null || true
  ./config.sh remove --token "$2"
  echo "已从 GitHub 注销并停止服务（目录 ${DIR} 可手动删除）"
  exit 0
fi
TOKEN="${1:-}"
if [ -z "${TOKEN}" ]; then
  echo "打开 ${REPO_URL}/settings/actions/runners/new ，复制 ./config.sh 那一行里 --token 后面的那串字符"
  read -rsp "粘贴 token 后回车（屏幕不显示）： " TOKEN; echo
fi
TOKEN="${TOKEN//[<> ]/}"
[ -n "${TOKEN}" ] || { echo "没有收到 token，已退出"; exit 1; }

case "$(uname -m)" in
  arm64) ARCH=osx-arm64 ;;
  *) ARCH=osx-x64 ;;
esac
mkdir -p "${DIR}"
cd "${DIR}"
if [ ! -x ./config.sh ]; then
  VER=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['tag_name'].lstrip('v'))")
  echo "== 下载 runner ${VER}（${ARCH}）"
  curl -fsSL -o runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v${VER}/actions-runner-${ARCH}-${VER}.tar.gz"
  tar xzf runner.tar.gz && rm -f runner.tar.gz
fi
echo "== 登记到 ${REPO_URL}（名字 ${NAME}，标签 usr）"
./config.sh --unattended --replace --url "${REPO_URL}" --token "${TOKEN}" \
  --name "${NAME}" --labels usr --work _work
echo "== 安装为后台服务（登录后自动启动）"
./svc.sh install >/dev/null 2>&1 || true
./svc.sh start
./svc.sh status | tail -n 3
echo
echo "完成。GitHub 页面 Settings → Actions → Runners 里应显示 ${NAME} 为 Idle。"
echo "触发一次检查：仓库 Actions → “01 Code quality” → Run workflow。"
