#!/usr/bin/env bash
# Mac 一键初始化：推送到 GitHub → 建 Python 虚拟环境 → 跑测试 → 下载 ETF 日线与分红
# → 质量门禁 → 生成快照。可重复运行（已存在的步骤会跳过或幂等覆盖）。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/5 推送到 GitHub =="
if git ls-remote --exit-code origin main >/dev/null 2>&1; then
  git push origin main || echo "推送失败（远端有新提交？）— 先 git pull --rebase 再推"
else
  git push -u origin main
fi

echo "== 2/5 Python 虚拟环境 =="
if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 \
    "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12"; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "找不到 Python 3.11+，请先安装：brew install python@3.12"; exit 1
  fi
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[dev]'

echo "== 3/5 测试 =="
.venv/bin/pytest -q

echo "== 4/5 下载行情（Yahoo 复权日线 + 分红） =="
SYMBOLS="SPY QQQ IWM EFA IEF GLD SHY"
.venv/bin/usr-collect-daily $SYMBOLS --start 2004-01-01 --execute >/dev/null
.venv/bin/usr-audit-ohlcv $SYMBOLS --output artifacts/quality/etf_trend_baseline.json >/dev/null \
  || echo "质量门禁有错误，详见 artifacts/quality/etf_trend_baseline.json"

echo "== 5/5 快照 =="
.venv/bin/usr-snapshot $SYMBOLS | tee artifacts/latest-snapshot.txt
echo "完成。回到 Claude 说一声即可继续。"
