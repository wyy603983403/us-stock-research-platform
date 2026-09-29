#!/usr/bin/env bash
# 分钟线批量下载（可断点续传，中断后重跑同一命令即可接着下）
#   bash scripts/mac_download_intraday.sh           # Alpaca 全部 + Tiingo 核心 ETF
#   bash scripts/mac_download_intraday.sh alpaca    # 只跑 Alpaca
#   bash scripts/mac_download_intraday.sh tiingo    # 只跑 Tiingo
# caffeinate 防止 Mac 睡眠中断下载（合上盖子仍会睡，需接电源并保持开盖或外接显示器）
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "请先运行 bash scripts/mac_bootstrap.sh"; exit 1; }
ROOT="$(grep '^USR_STORAGE_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "数据卷未挂载：$ROOT"; exit 1; fi
.venv/bin/pip install -q -e .
mkdir -p artifacts/intraday logs
STAMP="$(date +%Y%m%d_%H%M)"
WHAT="${1:-all}"

if [ "$WHAT" = all ] || [ "$WHAT" = alpaca ]; then
  echo "== Alpaca：全部已存日线的标的，2020-07-27 起（日志 logs/intraday_alpaca_${STAMP}.log）"
  caffeinate -i .venv/bin/usr-collect-intraday-alpaca --all-stored --execute \
    --report "artifacts/intraday/alpaca_${STAMP}.json" 2> >(tee "logs/intraday_alpaca_${STAMP}.log" >&2)
fi

if [ "$WHAT" = all ] || [ "$WHAT" = tiingo ]; then
  CORE="$(.venv/bin/python -c "import yaml;print(' '.join(yaml.safe_load(open('configs/universes/intraday_core.yml'))['symbols']))")"
  echo "== Tiingo：核心 ETF，2018-06-01 起（遇限流会停，隔一小时重跑本命令即可续传）"
  caffeinate -i .venv/bin/usr-collect-intraday $CORE --start 2018-06-01 --execute \
    --report "artifacts/intraday/tiingo_${STAMP}.json"
fi
