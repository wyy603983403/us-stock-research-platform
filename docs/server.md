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

## 个股板块（2026-10-08）

用户自己挑、在嘉信 App 手动下单；系统记账、提醒、每周出候选清单。另外的钱，不占 `configs/live.yml` 的策略资金。

- 建账：`bash scripts/stocks.sh init 金额`（一次）。下单前：`bash scripts/stocks.sh plan 代码 BUY 股数 价格`（检查单只 ≤25%）；
  成交后：`bash scripts/stocks.sh buy/sell 代码 股数 成交价`。分红、费用：`cash`；追加资金：`cash 金额 说明 deposit`；拆股：`split`。
- 每日（流水线）：`usr-stocks monitor` 估值（Alpaca 收盘价），比成本跌 20%、单只超 25%、板块回撤 30% 时通知；
  状态在 `artifacts/stocks/status.md`（`bash scripts/stocks.sh status`）。
- 每周五信号日：`usr-stocks screen` 生成 `artifacts/stocks/screen_<日期>.md`（`bash scripts/stocks.sh screen`）。
  规则见 `configs/stocks.yml`；**未经验证有超额收益**，只是参考清单。
- 个股板块不能买卖策略代码（SPY/SSO/BIL/TLT/IEF/GLD），以免两本账混在一起。

## 嘉信 Trader API（2026-10-08，默认关闭）

1. 你在 developer.schwab.com 申请开发者账号与 “Trader API – Individual”，回调地址填 `https://127.0.0.1`。
2. 批下来后，在服务器 `/opt/usr-trade/.env` 里加 `SCHWAB_APP_KEY`、`SCHWAB_APP_SECRET`、`SCHWAB_REDIRECT_URI=https://127.0.0.1`
   （多个账户时加 `SCHWAB_ACCOUNT_LAST4`）。密钥只在服务器上，不发给任何人、不进 Git。
3. 服务器 root 运行一次 `bash /opt/usr-trade/app/scripts/server_schwab_setup.sh`（装 09:50 的提交定时器；下单开关关着时它什么都不做）。
4. Mac：`bash scripts/schwab.sh login` 登录授权（**每 7 天一次**，到期前两天通知提醒）。
5. 把 `configs/schwab_api.yml` 的 `read_enabled` 改为 `true` 并部署：每日流水线自动记成交（策略代码进实盘账本，其余进个股账本）、
   对账，不一致时通知。此后不要再手动 `live.sh fill` / `stocks.sh buy`（会提示，确需手动加 `--force`）。
6. 自动下单（`orders_enabled`）只有你能打开：同时在 AGENTS.md 写明决定、改 `.github/workflows/04-risk.yml` 的检查、填 `user_decision`。
   打开后每个交易日纽约 09:50 提交前一晚通过独立复核的清单：先卖后买、当日有效限价（报价 ±0.10%）、报价偏离清单参考价 3% 以上不下、
   账户与账本不一致不下、同一清单只提交一次。紧急停止：`bash scripts/schwab.sh stop`（恢复：`resume`）。

## 手机指令（ntfy，2026-10-08）

每条通知下方有两个按钮：**查询状态**（回推净值、仓位、持仓、需要关注的事项）和**紧急停止**（创建 `portfolio/STOP_TRADING`：
清单照常生成，但不发模拟单、不做嘉信自动下单）。按钮把指令发到单独的随机频道 `NTFY_CMD_TOPIC`（服务器 .env），
由 `usr-commands.service` 监听；只认“状态 / 停止”，其他一律忽略，超过 10 分钟的旧消息不执行，记录在 `logs/commands.log`。
**恢复只能在 Mac 上**：`bash scripts/control.sh resume`（`status` 查看是否停止中）。频道名泄露时最坏情况只是被人“停止”。

## 上线前彩排（2026-10-10）

`python tools/rehearse_server.py --data <数据目录>` 在临时目录里用本仓库代码完整跑 `scripts/server_daily.sh`：
只替换联网部分（数据更新、Alpaca 模拟账户换成本地假账户、通知改为打印），逐日检查 6 个场景——首日建仓与独立复核与模拟单提交、
次日成交对账、数据晚到（只减仓、不崩溃）、Alpaca 拒单（原因进通知）、紧急停止、实盘账本建立后的实盘清单（1% 现金缓冲）与复核。
改动服务器流程后先跑它；`--days` 指定三个日期（前两个须在最近 4 天内，第三个晚于数据末日）。

## 盘中大跌预警与周报（2026-10-10）

- `usr-watch.timer`：纽约时间交易日 09:40–16:00 每 15 分钟取一次 SPY 实时价（Alpaca IEX），比昨收跌 3% / 5% / 7% 各推送一次，
  附模拟盘与实盘按当前仓位估算的当日盈亏。只提醒、从不交易；“紧急停止”只阻止新订单，不卖出持仓。
- 周报：每周最后一个交易日那次运行之后单独推送（`usr-weekly`）：本周与开始以来的账户 / 模型 / SPY 涨跌、信号与仓位、本周订单、个股板块、需要关注的事项。

