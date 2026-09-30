#!/usr/bin/env bash
# SEC 批量数据（含已退市公司）：财报数据集 2009 起、内部人交易 2006 起，然后建立历史代码→SEC 编号对照。
# 约 150 个季度文件、合计约 5 GB 下载（存下来的只保留需要的字段，远小于此）。可反复运行，已完成的季度自动跳过。
# 需要 .env 里有 SEC_USER_AGENT="名字 邮箱"。
#   bash scripts/mac_data_sec.sh
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
ROOT="$(grep '^USR_STORAGE_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "数据卷未挂载：$ROOT"; exit 1; fi
grep -q '^SEC_USER_AGENT=' .env || { echo '请先在 .env 加一行 SEC_USER_AGENT="名字 邮箱"'; exit 1; }
.venv/bin/pip install -q -e .
mkdir -p artifacts/extras logs
LOG="logs/sec_bulk_$(date +%Y%m%d_%H%M).log"
echo "== 1/3 SEC 内部人交易（2006 起）与财报数据集（2009 起），进度见 $LOG"
caffeinate -i .venv/bin/usr-collect-sec-bulk --what both --execute \
  --report artifacts/extras/sec_bulk.json 2>>"$LOG" | tail -n 30
echo "== 2/3 历史代码 → SEC 编号"
.venv/bin/usr-build-cik-map --report artifacts/extras/ticker_cik.json | head -n 20
echo "== 3/3 数据目录"
.venv/bin/usr-catalog >/dev/null && sed -n '/## 其他数据/,/## 标普/p' artifacts/catalog.md
echo "完成。回到 Claude 说一声即可。"
