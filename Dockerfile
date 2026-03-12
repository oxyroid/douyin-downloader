FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn[standard]

# Core downloader (upstream code)
COPY app/ ./app/

# Custom modules (server + uploaders)
COPY server.py .
COPY immich_uploader.py .
COPY telegram_uploader.py .

# Download directory
RUN mkdir -p /app/Downloaded

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
