#!/usr/bin/env bash
# 在服务器上以 root 运行（由 server_deploy.sh 调用）。可重复运行。
#   用户 usrtrade 运行流水线；/opt/usr-trade/{app,state,data,.env}；systemd 定时器按纽约时间触发。
set -euo pipefail
BASE=/opt/usr-trade
APP=$BASE/app
id usrtrade >/dev/null 2>&1 || useradd --system --create-home --home-dir $BASE/home usrtrade
mkdir -p $BASE/state $BASE/data $BASE/state/logs
chown -R usrtrade:usrtrade $BASE
chmod 600 $BASE/.env

# 运行时状态（订单、账本、日志、状态页）放在 state，代码目录可随时覆盖
for d in orders portfolio artifacts logs; do
  [ -e "$APP/$d" ] || ln -s "$BASE/state/$d" "$APP/$d"
  mkdir -p "$BASE/state/$d"
done
ln -sf $BASE/.env $APP/.env
chown -R usrtrade:usrtrade $BASE

echo "-- Python 环境（uv 管理 Python 3.12，不动系统 Python）"
if [ ! -x $BASE/home/.local/bin/uv ]; then
  su -s /bin/bash usrtrade -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
fi
su -s /bin/bash usrtrade -c "cd $APP && ~/.local/bin/uv venv --python 3.12 --allow-existing .venv >/dev/null \
  && ~/.local/bin/uv pip install --python .venv/bin/python -q -e '.[storage]'"

echo "-- 初始数据（只在缺少时下载）"
if [ ! -f $BASE/data/parquet/daily/SPY.parquet ]; then
  su -s /bin/bash usrtrade -c "cd $APP && .venv/bin/usr-collect-daily SPY SSO BIL --start 1993-01-01 --execute >/dev/null \
    && .venv/bin/usr-collect-macro --execute >/dev/null" && echo "   SPY/SSO/BIL 与 FRED 利率已下载"
fi

echo "-- 通知（ntfy.sh；没有配置时生成一个随机频道）"
if ! grep -q '^NTFY_TOPIC=' $BASE/.env; then
  echo "NTFY_TOPIC=usr-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> $BASE/.env
fi
TOPIC=$(grep '^NTFY_TOPIC=' $BASE/.env | cut -d= -f2)

echo "-- systemd 定时器（纽约时间周一至周五 21:15，23:45 再补一次；错过的开机后补跑）"
cat > /etc/systemd/system/usr-trade.service <<UNIT
[Unit]
Description=US stock trading pipeline (daily, files and paper orders only)
After=network-online.target
[Service]
Type=oneshot
User=usrtrade
WorkingDirectory=$APP
ExecStart=/bin/bash $APP/scripts/server_daily.sh
UNIT
cat > /etc/systemd/system/usr-trade.timer <<UNIT
[Unit]
Description=Run the trading pipeline after the US close
[Timer]
OnCalendar=Mon..Fri 21:15 America/New_York
OnCalendar=Mon..Fri 23:45 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now usr-trade.timer >/dev/null
echo
echo "完成。下次运行：$(systemctl list-timers usr-trade.timer --no-legend | awk '{print $1, $2, $3}')"
echo "手机安装 ntfy App，订阅频道：$TOPIC   （或浏览器打开 https://ntfy.sh/$TOPIC）"
echo "立即试跑：systemctl start usr-trade.service；日志：journalctl -u usr-trade -n 50；状态：cat $BASE/state/artifacts/status.md"
