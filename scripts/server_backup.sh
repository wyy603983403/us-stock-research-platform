#!/usr/bin/env bash
# 服务器每日流水线结束后调用（usrtrade 用户）：把运行记录推送到私有 GitHub 仓库（.env 里 OPS_REPO）。
#   推送：订单清单、演练/模拟账本与净值、复核记录、状态页数据、最近 30 天日志、心跳 heartbeat.json。
#   不推送：.env、密钥、行情数据。没有配置 OPS_REPO 或密钥时什么都不做。
#   仓库里的 .github/workflows/heartbeat.yml 每天检查心跳，服务器漏跑就让 GitHub 发邮件（见 ops/README.md）。
set -uo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
STATE=/opt/usr-trade/state
DIR=/opt/usr-trade/backup
KEY="$HOME/.ssh/ops_backup_ed25519"
REPO=$(grep -E '^OPS_REPO=' "$APP/.env" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '\r')
[ -n "$REPO" ] && [ -f "$KEY" ] || { echo "备份：未配置（OPS_REPO 或密钥缺失），跳过"; exit 0; }
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
git config --global user.name >/dev/null 2>&1 || git config --global user.name "usr-trade server"
git config --global user.email >/dev/null 2>&1 || git config --global user.email "usr-trade@localhost"

if [ ! -d "$DIR/.git" ]; then
  git clone -q "$REPO" "$DIR" 2>/dev/null || {
    mkdir -p "$DIR" && git -C "$DIR" init -q && git -C "$DIR" checkout -q -b main
    git -C "$DIR" remote add origin "$REPO"
  }
fi
cd "$DIR" || exit 1
git checkout -q -B main 2>/dev/null
git fetch -q origin 2>/dev/null || true
git rev-parse -q --verify origin/main >/dev/null && git reset -q --hard origin/main

mkdir -p state/artifacts state/logs .github/workflows
rm -rf state/orders state/portfolio
cp -a "$STATE/orders" state/orders
cp -a "$STATE/portfolio" state/portfolio
for f in status.json status.md; do [ -f "$STATE/artifacts/$f" ] && cp "$STATE/artifacts/$f" state/artifacts/; done
if [ -d "$STATE/artifacts/stocks" ]; then mkdir -p state/artifacts/stocks; cp "$STATE/artifacts/stocks/"*.md "$STATE/artifacts/stocks/"*.json state/artifacts/stocks/ 2>/dev/null; fi
if [ -d "$STATE/artifacts/live" ]; then mkdir -p state/artifacts/live; cp "$STATE/artifacts/live/status."* state/artifacts/live/ 2>/dev/null; fi
find "$STATE/logs" -name 'daily_*.log' -mtime -30 -exec cp {} state/logs/ \; 2>/dev/null
find state/logs -name 'daily_*.log' -mtime +30 -delete 2>/dev/null
cp "$APP/ops/heartbeat.yml" .github/workflows/heartbeat.yml
cp "$APP/ops/README.md" README.md
python3 - "$STATE/artifacts/status.json" > heartbeat.json <<'PY'
import json, socket, sys
from datetime import datetime, timezone
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
print(json.dumps({
    "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "host": socket.gethostname(),
    "data_day": s.get("day"),
    "study": s.get("study"),
    "attention": s.get("attention", []),
}, ensure_ascii=False, indent=2))
PY
git add -A
git commit -q -m "运行记录 $(date -u '+%F %H:%M UTC')" || true
if git push -q origin HEAD:main 2>/tmp/usr_backup_err; then
  echo "备份：已推送到 $REPO"
else
  echo "备份：推送失败 $(head -c 300 /tmp/usr_backup_err)"
fi
