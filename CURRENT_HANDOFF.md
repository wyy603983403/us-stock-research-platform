# 当前交接（2026-09-28）

## 已完成

- 仓库骨架：Yahoo 日线采集（默认只预览）、质量门禁、sha256 内容寻址快照、月度再平衡回测（趋势 / 双动量 / 买入持有）、块自助法推断、只读晋级评估、风险配置（最差 12 个月亏损 ≤ 25%，交易永久关闭）。
- 第一份研究合同草案：`research/etf-trend-baseline/study.yml`。
- 合成数据测试覆盖全流程。

## 下一步

1. 在 Mac 上建 Python 3.11 虚拟环境并安装：`pip install -e '.[dev]'`，跑 `pytest`。
2. 运行 `scripts/run_baseline.sh` 下载 7 只 ETF、过质量门禁、生成快照。
3. 把快照 ID 和质量报告路径写进合同，冻结后回测，结果写 `docs/`。
4. 按 `docs/research-plan.md` 继续研究 2、3。
