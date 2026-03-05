FROM aiogram/telegram-bot-api:latest

USER root

# 安装 proxychains-ng
RUN apk add --no-cache proxychains-ng

# 生成 proxychains 配置（运行时由 entrypoint 覆盖）
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
