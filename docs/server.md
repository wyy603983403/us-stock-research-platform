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

网页入口（HTTPS + 密码，2026-10-03）：在 Mac 运行 `bash scripts/server_web.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519 [域名]`，
本机输入用户名和密码（服务器只存 bcrypt 哈希），服务器装 Caddy（`usr-web.service`）。只开放 `/dashboard.html` 与 `/status.json`，
其余 404；不带密码 401。没有域名时用 IP + 自签证书（浏览器首次提示不安全，确认即可，传输仍加密）；有域名时自动申请正式证书
（需放行 80/443）。云服务商安全组需放行 TCP 443。重新运行即可改密码；停用：`systemctl disable --now usr-web`。

运行记录备份与漏跑报警（2026-10-04）：在 GitHub 新建空的私有仓库（如 `us-stock-ops`），Mac 运行
`bash scripts/server_backup_setup.sh root@43.135.185.94 ~/.ssh/evunea_deploy_ed25519 git@github.com:用户名/us-stock-ops.git`，
把显示的公钥加为该仓库的 Deploy key（允许写入），再运行一次测试。之后每次流水线结束推送订单、账本、复核记录、状态与日志
（不含密钥和行情）；仓库内 `heartbeat.yml` 每天北京时间 14:30 检查，周二至周六超过 10 小时无心跳即失败，GitHub 发邮件。

阶段 1 复核：Mac 运行 `bash scripts/review.sh 2026-10-02` 查看清单（含自动独立复核表），确认后记录；
`bash scripts/review.sh` 只看进度。记录存 `portfolio/reviews/vt_plus_defensive.jsonl`，监控页显示进度。
