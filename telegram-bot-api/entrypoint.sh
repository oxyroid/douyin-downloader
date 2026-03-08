#!/bin/sh
set -e

PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TYPE="${PROXY_TYPE:-socks5}"

# If PROXY_HOST is a hostname, resolve it to an IPv4 address
RESOLVED_HOST=$(getent ahostsv4 "$PROXY_HOST" 2>/dev/null | awk '{print $1}' | head -1)
if [ -z "$RESOLVED_HOST" ]; then
  # Fall back to generic resolution
  RESOLVED_HOST=$(getent hosts "$PROXY_HOST" 2>/dev/null | awk '{print $1}' | head -1)
fi
if [ -z "$RESOLVED_HOST" ]; then
  RESOLVED_HOST="$PROXY_HOST"
fi

# Generate proxychains config
cat > /etc/proxychains/proxychains.conf << EOF
strict_chain
proxy_dns
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
${PROXY_TYPE} ${RESOLVED_HOST} ${PROXY_PORT}
EOF

echo "proxychains: ${PROXY_TYPE} ${RESOLVED_HOST}:${PROXY_PORT} (${PROXY_HOST})"

# Launch telegram-bot-api through proxychains
exec proxychains4 -q telegram-bot-api \
  --dir=/var/lib/telegram-bot-api \
  --temp-dir=/tmp/telegram-bot-api \
  --http-port="${TELEGRAM_HTTP_PORT:-8081}" \
  ${TELEGRAM_LOCAL:+--local} \
  "$@"
