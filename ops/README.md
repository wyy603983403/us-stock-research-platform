# 美股交易系统 · 运行记录

本仓库由云服务器每次运行后自动推送（`scripts/server_backup.sh`），只含运行记录，不含密钥和行情数据：

- `state/orders/` 订单清单（`.md` 末尾有“自动独立复核”；`.json.rejected` 为复核未通过被搁置的清单）
- `state/portfolio/` 演练 / 模拟账本、净值历史（`*.nav.csv`）、复核记录（`reviews/*.jsonl`）
- `state/artifacts/status.json`、`status.md` 最新状态
- `state/logs/` 最近 30 天日志
- `heartbeat.json` 最近一次运行时间

`.github/workflows/heartbeat.yml` 每天北京时间 14:30 检查心跳：周二至周六超过 10 小时没有推送即判定漏跑，
检查失败时 GitHub 会给仓库所有者发邮件（Settings → Notifications → Actions 保持开启）；
在仓库 Settings → Secrets and variables → Actions 新建 `NTFY_TOPIC` 可同时推送到手机。

服务器坏了时：新服务器部署后，把本仓库的 `state/orders`、`state/portfolio` 拷回 `/opt/usr-trade/state/` 即可接着跑。
