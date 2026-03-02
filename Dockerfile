FROM python:3.12-slim

WORKDIR /app

# 系统依赖（playwright 需要的最小运行时，可选）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn[standard]

# 复制项目代码
COPY app/ .

# 下载目录
RUN mkdir -p /app/Downloaded

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
