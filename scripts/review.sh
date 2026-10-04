#!/usr/bin/env bash
# 在 Mac 上运行：记录你对某份订单清单的复核（阶段 1 门槛），并显示进度。
#   bash scripts/review.sh 2026-10-02            # 先显示清单与自动复核表，确认后记录
#   bash scripts/review.sh 2026-10-02 "备注"
#   bash scripts/review.sh                       # 只看进度
# 服务器地址取自 portfolio/.vt_on_server；私钥默认 ~/.ssh/evunea_deploy_ed25519（可用 USR_KEY 覆盖）。
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=$(cut -d' ' -f1 portfolio/.vt_on_server 2>/dev/null || true)
[ -n "$HOST" ] || { echo "找不到服务器地址（portfolio/.vt_on_server），先运行 scripts/server_deploy.sh"; exit 1; }
KEY="${USR_KEY:-$HOME/.ssh/evunea_deploy_ed25519}"
STUDY=vt_plus_defensive
RUN="cd /opt/usr-trade/app && su -s /bin/bash usrtrade -c"
DAY="${1:-}"; NOTE="${2:-}"
if [ -z "$DAY" ]; then
  ssh -i "$KEY" "$HOST" "$RUN '.venv/bin/usr-review --study $STUDY --progress'"
  exit 0
fi
ssh -i "$KEY" "$HOST" "cat /opt/usr-trade/state/orders/$STUDY/$DAY.md" || exit 1
echo
read -r -p "确认 $DAY 这份清单规则执行正确、自动复核一致，记录为已复核？[y/N] " OK
[ "$OK" = "y" ] || [ "$OK" = "Y" ] || { echo "未记录"; exit 0; }
NOTE_ESC=$(printf '%s' "$NOTE" | tr -d "'\"")
ssh -i "$KEY" "$HOST" "$RUN \".venv/bin/usr-review --study $STUDY --day $DAY --note '$NOTE_ESC'\""
