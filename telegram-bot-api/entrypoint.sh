#!/bin/sh
set -e

PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TYPE="${PROXY_TYPE:-socks5}"

# 如果 PROXY_HOST 是域名，解析为 IPv4 地址
RESOLVED_HOST=$(getent ahostsv4 "$PROXY_HOST" 2>/dev/null | awk '{print $1}' | head -1)
if [ -z "$RESOLVED_HOST" ]; then
  # 回退到通用解析
  RESOLVED_HOST=$(getent hosts "$PROXY_HOST" 2>/dev/null | awk '{print $1}' | head -1)
fi
if [ -z "$RESOLVED_HOST" ]; then
  RESOLVED_HOST="$PROXY_HOST"
fi

# 生成 proxychains 配置
cat > /etc/proxychains/proxychains.conf << EOF
strict_chain
proxy_dns
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
${PROXY_TYPE} ${RESOLVED_HOST} ${PROXY_PORT}
EOF

echo "proxychains: ${PROXY_TYPE} ${RESOLVED_HOST}:${PROXY_PORT} (${PROXY_HOST})"

# 通过 proxychains 启动 telegram-bot-api
exec proxychains4 -q telegram-bot-api \
  --dir=/var/lib/telegram-bot-api \
  --temp-dir=/tmp/telegram-bot-api \
  --http-port="${TELEGRAM_HTTP_PORT:-8081}" \
  ${TELEGRAM_LOCAL:+--local} \
  "$@"
