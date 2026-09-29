#!/usr/bin/env bash
# 批量下载常用美股数据到当前存储（.env 里设了 USR_STORAGE_ROOT 就是 Parquet）。
# 可反复运行：已下载的标的自动跳过，中断后接着来。
#   bash scripts/mac_download_universe.sh              # ETF + 指数/利率 + 大盘股精选
#   bash scripts/mac_download_universe.sh sp500        # 另加当前标普500成分股（约500只，约10分钟）
#   REFRESH=1 bash scripts/mac_download_universe.sh    # 已有数据也重新拉一遍（增量更新到今天）
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
.venv/bin/pip install -q -e '.[dev]'

UNIVERSES=("etf_core" "macro_indices" "mega_caps" "$@")
ARGS=()
[ "${REFRESH:-0}" = "1" ] && ARGS+=(--refresh)

mkdir -p artifacts/universe
.venv/bin/usr-collect-universe "${UNIVERSES[@]}" ${ARGS[@]+"${ARGS[@]}"} --start 2000-01-01 --execute \
  --report artifacts/universe/last_download.json | tail -n 25 || echo "部分标的失败，详见 artifacts/universe/last_download.json（重跑即可补下）"

echo "== 质量门禁 =="
ALL=$(.venv/bin/python - <<'PY'
import yaml, sys
from pathlib import Path
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store
store = open_store(load_settings())
names = []
for p in sorted(Path("configs/universes").glob("*.yml")):
    names += yaml.safe_load(p.read_text())["symbols"]
print(" ".join(s for s in dict.fromkeys(names) if store.has_bars(s)))
PY
)
.venv/bin/usr-audit-ohlcv $ALL --output artifacts/quality/universe.json >/dev/null \
  && echo "全部通过" || echo "有标的未通过，详见 artifacts/quality/universe.json"
.venv/bin/python - <<'PY'
import json
r = json.load(open("artifacts/quality/universe.json"))["reports"]
bad = [x for x in r if x["errors"]]
print(f"已入库 {len(r)} 只；有错误 {len(bad)} 只：", [x["symbol"] for x in bad][:20])
PY
du -sh "$(grep '^USR_STORAGE_ROOT=' .env | cut -d= -f2-)" 2>/dev/null || true
echo "== 第二数据源交叉验证（Tiingo，核心 ETF；需 .env 里有 TIINGO_TOKEN） =="
.venv/bin/usr-crosscheck SPY QQQ IWM EFA IEF GLD SHY --output artifacts/quality/crosscheck.json \
  >/dev/null && echo "交叉验证通过" || echo "交叉验证未通过或取不到数据，详见 artifacts/quality/crosscheck.json"
echo "完成。回到 Claude 说一声，我来检查结果。"
