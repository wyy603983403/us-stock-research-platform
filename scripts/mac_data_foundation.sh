#!/usr/bin/env bash
# 数据基座一键脚本：标普500成分股价格 + 行业信息 → 宏观序列(FRED) → 公司财报(SEC) → 增量更新 → 数据目录。
# 可反复运行（已下载的自动跳过）。财报需要先在 .env 里设 SEC_USER_AGENT="你的名字 你的邮箱"（SEC 要求，只发给 sec.gov）。
#   bash scripts/mac_data_foundation.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
.venv/bin/pip install -q -e '.[dev]'
mkdir -p artifacts/universe

echo "== 1/5 价格：ETF、指数、大盘股、标普500成分股（含行业信息） =="
.venv/bin/usr-collect-universe etf_core macro_indices mega_caps sp500 --start 2000-01-01 --execute \
  --report artifacts/universe/last_download.json >/dev/null || echo "部分失败，见 artifacts/universe/last_download.json，重跑可补"

echo "== 2/5 宏观序列（FRED） =="
.venv/bin/usr-collect-macro --execute --report artifacts/universe/macro.json | tail -n 3 || echo "部分失败，见 artifacts/universe/macro.json"

echo "== 3/5 公司财报（SEC EDGAR，大盘股 + 标普500） =="
if grep -q '^SEC_USER_AGENT=' .env 2>/dev/null; then
  SP=$(.venv/bin/python - <<'PY'
import csv, io
from us_stock_research.config import load_settings
from us_stock_research.tables import TableStore
s = TableStore.from_settings(load_settings())
print(" ".join(r[0] for r in s.read("meta", "sp500", "symbol")) if s.has("meta", "sp500") else "")
PY
)
  .venv/bin/usr-collect-fundamentals $SP --execute --report artifacts/universe/fundamentals.json >/dev/null \
    || echo "部分失败（ETF/退市代码无 CIK 属正常），见 artifacts/universe/fundamentals.json"
else
  echo "跳过：.env 里没有 SEC_USER_AGENT。加一行 SEC_USER_AGENT=\"Your Name your@email\" 后重跑即可。"
fi

echo "== 4/5 质量门禁 =="
ALL=$(.venv/bin/python - <<'PY'
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store
print(" ".join(open_store(load_settings()).symbols()))
PY
)
.venv/bin/usr-audit-ohlcv $ALL --output artifacts/quality/universe.json >/dev/null || true

echo "== 5/5 数据目录 =="
.venv/bin/usr-catalog
echo "完成。数据目录也保存在 artifacts/catalog.md，回到 Claude 说一声。"
