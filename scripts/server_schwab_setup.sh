#!/usr/bin/env bash
# 服务器 root 运行一次（接口审批下来、准备开只读同步时）：安装纽约时间交易日 09:50 的提交定时器。
# 只要 configs/schwab_api.yml 的 orders_enabled 是 false，定时器每天只打印“未提交”，不会下单。
#   密钥写进 /opt/usr-trade/.env（你自己在服务器上编辑，不要发给任何人）：
#     SCHWAB_APP_KEY=...  SCHWAB_APP_SECRET=...  SCHWAB_REDIRECT_URI=https://127.0.0.1  [SCHWAB_ACCOUNT_LAST4=1234]
set -euo pipefail
BASE=/opt/usr-trade
APP=$BASE/app
cat > /etc/systemd/system/usr-submit.service <<UNIT
[Unit]
Description=Submit the verified live order list to Schwab (no-op unless orders_enabled)
After=network-online.target
[Service]
Type=oneshot
User=usrtrade
WorkingDirectory=$APP
ExecStart=/bin/bash $APP/scripts/server_submit.sh
UNIT
cat > /etc/systemd/system/usr-submit.timer <<UNIT
[Unit]
Description=Schwab submission window (New York morning)
[Timer]
OnCalendar=Mon..Fri 09:50 America/New_York
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now usr-submit.timer >/dev/null
grep -q '^SCHWAB_APP_KEY=' $BASE/.env && echo "已找到 SCHWAB_APP_KEY" || echo "提醒：$BASE/.env 里还没有 SCHWAB_APP_KEY / SCHWAB_APP_SECRET"
echo "完成。下一步：Mac 上运行 bash scripts/schwab.sh login，然后把 configs/schwab_api.yml 的 read_enabled 改为 true 并部署。"
