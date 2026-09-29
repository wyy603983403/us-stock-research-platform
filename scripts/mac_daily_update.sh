#!/usr/bin/env bash
# 每日增量更新：给库里所有标的补最新交易日；分红/拆股导致复权价被雅虎重述时自动整段重下。
#   bash scripts/mac_daily_update.sh            # 更新全部
#   bash scripts/mac_daily_update.sh --dry-run  # 只看会做什么，不写入
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
# 数据卷没挂载就不更新，避免悄悄写到别处
ROOT="$(grep '^USR_STORAGE_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "数据卷未挂载：$ROOT，已跳过更新"; exit 0; fi

mkdir -p artifacts/universe logs
STAMP="$(date +%Y%m%d)"
FLAG="--execute"; [ "${1:-}" = "--dry-run" ] && FLAG=""
.venv/bin/usr-update $FLAG --report "artifacts/universe/update_${STAMP}.json" > /dev/null || true
.venv/bin/python - <<PY
import json
r = json.load(open("artifacts/universe/update_${STAMP}.json"))
print(f"{r['date']}：{r['symbols']} 只标的，新增交易日 {r['appended_days']}，"
      f"整段重下 {len(r['refreshed'])} 只，失败 {len(r['failed'])} 只，质量错误 {len(r['quality_errors'])} 只")
for x in r["refreshed"][:10]:
    print("  重下", x["symbol"], "-", x["reason"])
for s, e in list(r["quality_errors"].items())[:10]:
    print("  质量", s, e[:1])
for s, e in list(r["failed"].items())[:10]:
    print("  失败", s, e[:80])
PY
