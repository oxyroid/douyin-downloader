# Douyin Downloader Wrapper

Docker HTTP wrapper for [douyin-downloader](./app/README.md) with [Immich](https://immich.app/) and [Telegram](https://telegram.org/) integration. Works great with iOS Shortcuts.

## Features

- Docker: `docker compose up -d`
- HTTP API with sync/async modes
- iOS Shortcuts friendly
- Immich auto-upload, grouped by author
- Telegram push
  - Cover + video as MediaGroup
  - Custom caption template
  - Silent delivery
  - Self-hosted Bot API Server support (up to 2GB)
- 3-layer dedup: API → downloader → Immich

## Project Layout

```
├── app/                        # Upstream douyin-downloader (git submodule)
├── src/                        # Custom extensions
│   ├── server.py               # FastAPI HTTP service
│   └── uploaders/
│       ├── immich.py           # Immich upload module
│       └── telegram.py         # Telegram push module
├── scripts/
│   └── init-cookies.py         # Cookie initialization script
├── config.yml                  # Configuration (sensitive, gitignored)
├── config.example.yml          # Config template
├── Dockerfile
├── docker-compose.yml
└── downloads/                  # Downloaded files
```

## Quick Start

### 1. Clone

```bash
git clone --recurse-submodules https://github.com/oxyroid/douyin-downloader.git
cd douyin-downloader
cp config.example.yml config.yml
```

### 2. Initialize cookies ⚠️

Douyin requires cookies. Run on your **host machine** (not Docker):

```bash
pip install playwright pyyaml
playwright install chromium
python scripts/init-cookies.py
```

Log in via QR code, then press Enter. Re-run when downloads start failing.

### 3. Immich (optional)

```yaml
immich:
  enabled: true
  api_url: 'http://host.docker.internal:2283'
  api_key: 'your_api_key'
```

### 4. Telegram (optional)

```yaml
telegram:
  enabled: true
  bot_token: '123456:ABC-DEF...'
  chat_id: '-1001234567890'       # Channel/Group ID (use @username or numeric ID like -100xxx)
  # Self-hosted Bot API for files >50MB (optional)
  api_base: 'http://telegram-bot-api:8081'
  api_id: 'your_api_id'       # from my.telegram.org
  api_hash: 'your_api_hash'
```

Leave `api_base` empty to use official Telegram API (50MB limit).

### 5. Start

```bash
docker compose up -d
```

Verify:

```bash
curl http://localhost:8000/health
# {"status": "ok", "immich_enabled": true, "telegram_enabled": false}
```

## API

### GET `/d`

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Douyin link (URL-encoded) |
| `sync` | bool | no | `1` = wait for completion, `0` = run in background (default) |

```bash
# Async
curl "http://localhost:8000/d?url=https%3A%2F%2Fv.douyin.com%2Fxxxxxxxx"

# Sync (blocks until done)
curl "http://localhost:8000/d?url=https%3A%2F%2Fv.douyin.com%2Fxxxxxxxx&sync=1"
```

**Response example:**

```json
{
    "task_id": "a1b2c3d4e5f6",
    "status": "completed",
    "url": "https://v.douyin.com/xxxxxxxx",
    "message": "ok 1 / fail 0 / skip 0 | Immich: uploaded 2, dup 0, failed 0 | Telegram: sent 2, skip 0, fail 0",
    "summary": "1 downloaded\n2 uploaded to Immich\n2 sent to Telegram"
}
```

### POST `/download`

```bash
# Async
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxxxxx"}'

# Sync
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxxxxx", "sync": true}'
```

**Body params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Douyin link |
| `sync` | bool | no | Wait for completion (default `false`) |
| `mode` | string[] | no | Download mode, e.g. `["post"]` |
| `number_post` | int | no | Max posts to download, `0` = all |
| `thread` | int | no | Concurrency |

### GET `/task/{task_id}`

```bash
curl http://localhost:8000/task/a1b2c3d4e5f6
```

### GET `/health`

```bash
curl http://localhost:8000/health
```

### GET `/health/deep`

Deep health check that tests actual connectivity to Immich and Telegram.

```bash
curl http://localhost:8000/health/deep
```

**Response example:**

```json
{
    "status": "ok",
    "checks": {
        "config": {"status": "ok"},
        "cookies": {"status": "ok"},
        "immich": {"status": "ok", "url": "http://immich:2283"},
        "telegram": {"status": "ok", "bot_username": "my_bot"}
    }
}
```

### GET `/metrics`

Prometheus-compatible metrics endpoint.

```bash
curl http://localhost:8000/metrics
```

### POST `/reload-config`

Reload configuration without restarting the container.

```bash
curl -X POST http://localhost:8000/reload-config
```

### GET `/init`

Get initialization instructions and cookie status.

```bash
curl http://localhost:8000/init
```

### POST `/reset`

Clear downloads and DB. Next request re-downloads everything.

```bash
curl -X POST http://localhost:8000/reset
```

**Response example:**

```json
{
    "status": "ok",
    "removed_dirs": 3,
    "removed_files": 1,
    "db_cleared": true,
    "summary": "Removed 3 dir(s) + 1 file(s)\nDB records cleared\nNext request will re-download and re-upload to Immich"
}
```

## iOS Shortcut

1. New Shortcut → "Receive Input"
2. "Get Contents of URL": `https://your-domain.com/d?url=<Input>&sync=1`
3. "Get Dictionary Value" → `summary`
4. "Show Notification"

Expose via Cloudflare Tunnel or similar.

## Config Reference

See [config.example.yml](./config.example.yml) for all options.

Restart after changes: `docker compose restart`

## Credits

- [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader)
- [Immich](https://immich.app/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

---

🤖 This project is 100% built with **Claude Opus 4.5** + **GitHub Copilot**.