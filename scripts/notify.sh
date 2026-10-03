#!/usr/bin/env bash
# 发送通知：.env 里有 NTFY_TOPIC 用 ntfy.sh；有 BARK_KEY 用 Bark（iOS）。都没有就只写日志。
#   bash scripts/notify.sh "标题" "内容"
TITLE="$1"; BODY="$2"
ENV_FILE="$(dirname "$0")/../.env"
get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-; }
TOPIC=$(get NTFY_TOPIC); BARK=$(get BARK_KEY)
if [ -n "$TOPIC" ]; then
  curl -s -m 20 -H "Title: $(printf '%s' "$TITLE" | base64 -w0 2>/dev/null | sed 's/^/=?UTF-8?B?/;s/$/?=/')" \
    -d "$BODY" "https://ntfy.sh/$TOPIC" >/dev/null || echo "ntfy 发送失败"
fi
if [ -n "$BARK" ]; then
  curl -s -m 20 -X POST "https://api.day.app/$BARK" -H 'Content-Type: application/json; charset=utf-8' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"body":sys.argv[2]}))' "$TITLE" "$BODY")" \
    >/dev/null || echo "Bark 发送失败"
fi
[ -z "$TOPIC$BARK" ] && echo "（未配置通知）$TITLE：$BODY"
exit 0
