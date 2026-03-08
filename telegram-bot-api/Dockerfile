FROM aiogram/telegram-bot-api:latest

USER root

# Install proxychains-ng for SOCKS5 proxy support
RUN apk add --no-cache proxychains-ng

# Entrypoint generates proxychains config at runtime
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
