# 云服务器运行交易流水线（2026-10-03）

研究数据和回测留在 Mac；美国云服务器只跑已批准策略 `sp500_trend_voltarget` 的每日流水线，
只下载 SPY、SSO、BIL 日线和 FRED 利率（几 MB）。

部署（在 Mac 项目目录，可重复运行）：

    bash scripts/server_deploy.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519

- 代码在 `/opt/usr-trade/app`，订单/账本/日志/状态页在 `/opt/usr-trade/state`，数据在 `/opt/usr-trade/data`，
  密钥 `/opt/usr-trade/.env`（600 权限；首次部署从 Mac 的 .env 只带 `ALPACA_*` 与 `SEC_USER_AGENT`）。
- 以系统用户 `usrtrade` 运行；systemd 定时器 `usr-trade.timer`：纽约时间周一至周五 21:15，23:45 再补一次（错过的开机补跑），
  都在 Alpaca 收盘竞价单的接收窗口内。
- 通知：飞书群自定义机器人（Mac 的 .env 加 `FEISHU_WEBHOOK=`，开了签名校验再加 `FEISHU_SECRET=`，部署时同步到服务器，部署最后会发一条测试消息；本地测试 `bash scripts/notify.sh --test`）；另有 ntfy.sh（部署时生成随机频道）和 Bark（`BARK_KEY`）。每个交易日一条简报，
  有订单/只减仓/需要关注时另发。
- 部署成功后 Mac 写入 `portfolio/.vt_on_server`，Mac 的每日更新不再为该策略出单/记账（删除该文件即恢复）。
  ETF 趋势基线演练仍在 Mac。

常用：`systemctl start usr-trade.service`（立即运行）、`journalctl -u usr-trade -n 80`、`cat /opt/usr-trade/state/artifacts/status.md`、
`systemctl list-timers usr-trade.timer`。

监控页面（2026-10-03）：每次运行后生成 `/opt/usr-trade/state/artifacts/dashboard.html`（状态、净值对比研究模型与 SPY、
仓位历史、SPY 与 200 日均线、最近订单、持仓、需要关注的事项；明暗两种主题，手机可看）。页面只在服务器本机 8787 端口提供
（`usr-dashboard.service`），不对公网开放；在 Mac 上运行 `bash scripts/open_dashboard.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519`
建立 SSH 隧道并自动用浏览器打开。Mac 本地运行该策略时同样生成 `artifacts/dashboard.html`，可直接双击打开。
