#!/usr/bin/env bash
# 在服务器上以 root 运行（由 Mac 上的 scripts/server_web.sh 调用）：用 Caddy 把监控页面放到 HTTPS + 密码后面。
#   标准输入两行：用户名、密码（密码只存 bcrypt 哈希）
#   bash server_web_setup.sh 域名或IP [端口]
# 只开放 /dashboard.html 和 /status.json；其余路径 404。页面内容仍由 usr-dashboard.service（127.0.0.1:8787）提供。
set -euo pipefail
SITE="${1:?缺少域名或 IP}"; PORT="${2:-443}"
CADDY_VERSION=2.10.2
read -r WEB_USER; read -r WEB_PASS
[ -n "$WEB_USER" ] && [ ${#WEB_PASS} -ge 10 ] || { echo "用户名不能为空，密码至少 10 位"; exit 1; }

echo "-- 安装 Caddy $CADDY_VERSION"
if ! /usr/local/bin/caddy version 2>/dev/null | grep -q "v$CADDY_VERSION"; then
  case "$(uname -m)" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) echo "不支持的架构 $(uname -m)"; exit 1;; esac
  T=$(mktemp -d)
  curl -fsSL -m 120 -o "$T/c.tgz" \
    "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_$A.tar.gz" \
    || curl -fsSL -m 120 -o "$T/caddy" "https://caddyserver.com/api/download?os=linux&arch=$A"
  [ -f "$T/c.tgz" ] && tar xzf "$T/c.tgz" -C "$T" caddy
  install -m 755 "$T/caddy" /usr/local/bin/caddy; rm -rf "$T"
fi
id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
mkdir -p /var/lib/caddy && chown -R caddy:caddy /var/lib/caddy && chmod 750 /var/lib/caddy
command -v restorecon >/dev/null && restorecon -R /var/lib/caddy /usr/local/bin/caddy 2>/dev/null || true

# 端口被别的程序（比如已有的 nginx 网站）占用时不去动它，改用 8443
port_owner() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -n1 | cut -d'"' -f2; }
systemctl stop usr-web.service 2>/dev/null || true
OWNER=$(port_owner "$PORT")
if [ -n "$OWNER" ] && [ "$OWNER" != caddy ]; then
  if [ "$PORT" = 443 ] && [ -z "$(port_owner 8443)" ]; then
    echo "  443 端口已被 $OWNER 占用（不改动它），网页入口改用 8443"; PORT=8443
  else
    echo "端口 $PORT 已被 $OWNER 占用，请换一个端口重跑（第 4 个参数）"; exit 1
  fi
fi

HASH=$(/usr/local/bin/caddy hash-password --plaintext "$WEB_PASS")
unset WEB_PASS
if [[ "$SITE" =~ ^[0-9.]+$ ]]; then
  # 只有 IP：用 Caddy 自签证书（浏览器第一次会提示“不安全”，确认一次即可；传输仍加密）
  GLOBAL="default_sni $SITE
	auto_https disable_redirects"; TLS="tls internal"; ADDR="https://$SITE:$PORT"
else
  GLOBAL=""; TLS=""; ADDR="$SITE"; [ "$PORT" = 443 ] || ADDR="$SITE:$PORT"
fi
mkdir -p /etc/caddy
umask 027
cat > /etc/caddy/Caddyfile <<EOF
{
	$GLOBAL
}
$ADDR {
	$TLS
	basic_auth {
		$WEB_USER $HASH
	}
	header {
		X-Robots-Tag "noindex, nofollow"
		Cache-Control "no-store"
		X-Frame-Options DENY
		-Server
	}
	handle / {
		redir * /dashboard.html
	}
	handle /dashboard.html {
		reverse_proxy 127.0.0.1:8787
	}
	handle /status.json {
		reverse_proxy 127.0.0.1:8787
	}
	handle {
		respond 404
	}
	log {
		output file /var/lib/caddy/access.log {
			roll_size 5MiB
			roll_keep 3
		}
	}
}
EOF
chown root:caddy /etc/caddy/Caddyfile
/usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

cat > /etc/systemd/system/usr-web.service <<'EOF'
[Unit]
Description=美股交易系统监控页面（HTTPS + 密码）
After=network-online.target usr-dashboard.service
Wants=network-online.target

[Service]
User=caddy
Group=caddy
Environment=HOME=/var/lib/caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=full
StateDirectory=caddy
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable usr-dashboard.service usr-web.service >/dev/null 2>&1
systemctl restart usr-dashboard.service usr-web.service

# 系统防火墙（如果开着）
OPEN="$PORT"; [ "$TLS" = "" ] && OPEN="$PORT 80"
for p in $OPEN; do
  if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow "$p/tcp" >/dev/null; fi
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$p/tcp" && firewall-cmd -q --reload; fi
done

echo "-- 自检"
sleep 3
CODE_NOAUTH=$(curl -sk --connect-to "::127.0.0.1:$PORT" -o /dev/null -w '%{http_code}' \
  "https://$SITE:$PORT/dashboard.html" || true)
echo "  未带密码访问：HTTP $CODE_NOAUTH（应为 401）"
systemctl is-active --quiet usr-web.service && echo "  usr-web 运行中" || { journalctl -u usr-web -n 30 --no-pager; exit 1; }
[ -f /opt/usr-trade/state/artifacts/dashboard.html ] \
  || echo "  注意：还没有 dashboard.html（第一次运行流水线后生成：systemctl start usr-trade.service）"
URL="https://$SITE"; [ "$PORT" = 443 ] || URL="$URL:$PORT"
echo "完成：$URL  （用户名 $WEB_USER）"
