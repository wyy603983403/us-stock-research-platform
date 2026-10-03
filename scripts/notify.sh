#!/usr/bin/env bash
# 发送通知（.env 里配了哪个就发哪个，可同时配多个；都没有就只打印）：
#   FEISHU_WEBHOOK（+ 可选 FEISHU_SECRET）  飞书群自定义机器人
#   NTFY_TOPIC                              ntfy.sh
#   BARK_KEY                                Bark（iOS）
#   bash scripts/notify.sh "标题" "内容"
#   bash scripts/notify.sh --test           # 发一条测试消息
TITLE="${1:-}"; BODY="${2:-}"
if [ "$TITLE" = "--test" ]; then TITLE="美股交易系统 测试"; BODY="通知配置正常（$(hostname) $(date '+%F %T')）"; fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$DIR/.env"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY=python3
get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '\r'; }
FEISHU=$(get FEISHU_WEBHOOK); TOPIC=$(get NTFY_TOPIC); BARK=$(get BARK_KEY)

if [ -n "$FEISHU" ]; then
  FEISHU_SECRET=$(get FEISHU_SECRET) "$PY" - "$FEISHU" "$TITLE" "$BODY" <<'PY' || echo "飞书发送失败"
import base64, hashlib, hmac, json, os, sys, time, urllib.request
url, title, body = sys.argv[1:4]
msg = {"msg_type": "text", "content": {"text": f"{title}\n{body}".strip()}}
secret = os.environ.get("FEISHU_SECRET", "")
if secret:  # 机器人开了“签名校验”时需要
    ts = str(int(time.time()))
    key = f"{ts}\n{secret}".encode()
    msg |= {"timestamp": ts, "sign": base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()}
req = urllib.request.Request(url, json.dumps(msg).encode(), {"Content-Type": "application/json"})
r = json.load(urllib.request.urlopen(req, timeout=20))
if r.get("code", r.get("StatusCode", 0)) != 0:
    sys.exit(f"飞书返回错误：{r.get('msg') or r}")
PY
fi
if [ -n "$TOPIC" ]; then
  curl -s -m 20 -H "Title: =?UTF-8?B?$(printf '%s' "$TITLE" | base64 | tr -d '\n')?=" \
    -d "$BODY" "https://ntfy.sh/$TOPIC" >/dev/null || echo "ntfy 发送失败"
fi
if [ -n "$BARK" ]; then
  curl -s -m 20 -X POST "https://api.day.app/$BARK" -H 'Content-Type: application/json; charset=utf-8' \
    -d "$("$PY" -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"body":sys.argv[2]}))' "$TITLE" "$BODY")" \
    >/dev/null || echo "Bark 发送失败"
fi
[ -z "$FEISHU$TOPIC$BARK" ] && echo "（未配置通知）$TITLE：$BODY"
exit 0
