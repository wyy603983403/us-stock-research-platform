#!/usr/bin/env bash
# 安装 macOS 定时任务（launchd）：每天北京时间 07:30 运行增量更新。
# 美股收盘约北京时间 04:00–05:00，07:30 数据已就绪。电脑睡眠时错过的任务会在唤醒后补跑。
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
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer>
  </dict>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>StandardOutPath</key><string>$PWD/logs/daily_update.log</string>
  <key>StandardErrorPath</key><string>$PWD/logs/daily_update.err</string>
</dict></plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已安装：每天 07:30 运行增量更新，日志在 $PWD/logs/。卸载：bash scripts/mac_install_schedule.sh --remove"
