#!/usr/bin/env bash
# 安装 macOS 定时任务（launchd）：每天北京时间 10:05 和 13:05 各运行一次增量更新。
# 2026-10 实测：雅虎日线在收盘后数小时内才陆续给出当天数据（北京 07:30 只拿到 17/602 只，09:50 拿到 521 只），
# 所以第一次放在 10:05，13:05 再补一次（各步骤都可重复运行：已有数据不重下、订单不重复记账/发送）。
# 这两个时间也都在 Alpaca 收盘竞价单的接收窗口内（纽约时间 19:00 之后、次日 15:50 之前）。
# 电脑睡眠时错过的任务会在唤醒后补跑。
#   bash scripts/mac_install_schedule.sh            # 安装
#   bash scripts/mac_install_schedule.sh --remove   # 卸载
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL="com.usr.daily-update"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ "${1:-}" = "--remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"; echo "已卸载 $LABEL"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" logs
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$PWD/scripts/mac_daily_update.sh</string>
  </array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>5</integer></dict>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>StandardOutPath</key><string>$PWD/logs/daily_update.log</string>
  <key>StandardErrorPath</key><string>$PWD/logs/daily_update.err</string>
</dict></plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已安装：每天 10:05、13:05 运行增量更新，日志在 $PWD/logs/。卸载：bash scripts/mac_install_schedule.sh --remove"
