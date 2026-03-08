# Douyin Downloader — Docker HTTP API + Immich/Telegram Integration

A **Dockerized HTTP wrapper** around [douyin-downloader V2.0](./app/README.md), exposing a RESTful API for downloading Douyin videos and galleries.  Downloads can be automatically uploaded to a self-hosted [Immich](https://immich.app/) instance and/or forwarded to a [Telegram](https://telegram.org/) Channel.  Trigger it from an iOS Shortcut with one tap.

## Features

- **Docker deployment** — `docker compose up -d` and you're done
- **HTTP API** — FastAPI REST endpoints, async and sync modes
- **iOS Shortcuts** — GET endpoint + `sync=1`, share a link and get a notification
- **Immich upload** — auto-upload after download, organized into per-author albums (`douyin-<author>`)
- **Telegram push** — auto-send to a Channel/Group after download
  - Cover + video grouped into a single MediaGroup (cover first, video second)
  - Caption with title, hashtags, and a link back to the original video
  - Video dimensions and thumbnail included for correct aspect ratio
  - Silent delivery — no buzz for channel subscribers
  - Self-hosted Bot API Server (local mode) for uploads up to 2 GB
- **Three-layer dedup** — API layer (URL) → downloader layer (SQLite + local files) → Immich layer (checksum)
- **Fully configurable** — everything lives in `config.yml` or environment variables

## Project Layout

```
├── app/                        # Application code
│   ├── server.py               # FastAPI HTTP service
│   ├── immich_uploader.py      # Immich upload module
│   ├── telegram_uploader.py    # Telegram push module
│   ├── config.example.yml      # Config template
│   └── ...                     # Core downloader modules
├── telegram-bot-api/           # Self-hosted Telegram Bot API Server
│   ├── Dockerfile              # Based on aiogram/telegram-bot-api + proxychains
│   └── entrypoint.sh           # Startup script (proxy config + DNS resolution)
├── Dockerfile
├── docker-compose.yml
├── .env.example                # Environment variable template
└── .gitignore
```

## Quick Start

### 1. Prepare config files

```bash
cp app/config.example.yml app/config.yml
cp .env.example .env
```

### 2. Get Douyin cookies

Douyin downloads require valid cookies.  The easiest way:

```bash
cd app
pip install -r requirements.txt
pip install playwright
python -m playwright install chromium
python -m tools.cookie_fetcher --config config.yml
```

Log in to Douyin in the browser that opens, then press Enter in the terminal.  Cookies are written to `config.yml` automatically.

### 3. Configure Immich (optional)

If you run a self-hosted Immich instance, enable auto-upload in `config.yml`:

```yaml
immich:
  enabled: true
  api_url: 'http://localhost:2283'
  api_key: 'your_api_key_here'       # Immich → User Settings → API Keys
```

Or via `.env` (values in `config.yml` take precedence):

```bash
IMMICH_API_KEY=your_api_key_here
```

### 3.5 Configure Telegram (optional)

To push downloads to a Telegram Channel or Group:

1. Create a Bot with [@BotFather](https://t.me/BotFather) and grab the `bot_token`
2. Add the Bot to your target Channel/Group and make it an admin
3. Add to `config.yml`:

```yaml
telegram:
  enabled: true
  bot_token: '123456:ABC-DEF...'
  chat_id: '@my_channel'             # or a numeric chat_id like -100xxxx
  api_base: 'http://telegram-bot-api:8081'  # self-hosted Bot API Server
  caption_template: '**{author}:** {desc} {tags}'  # **bold** and _italic_ are converted to HTML
  send_cover: true
```

Or via environment variables:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=@my_channel
```

**Self-hosted Bot API Server (recommended)**

The repo ships a `telegram-bot-api` container built on `aiogram/telegram-bot-api` + `proxychains-ng`:

- **Local mode** — upload files up to 2 GB (the official API caps at 50 MB)
- **Proxy support** — routes traffic through the host's SOCKS5 proxy (`host.docker.internal:7890` by default)

You need Telegram API credentials in `.env` (get them at [my.telegram.org](https://my.telegram.org)):

```bash
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

If you don't need the self-hosted server, set `api_base` to `https://api.telegram.org` and remove the `telegram-bot-api` service from `docker-compose.yml`.

### 4. Start the service

```bash
docker compose up -d
```

Verify:

```bash
curl http://localhost:8000/health
# {"status": "ok", "immich_enabled": true, "telegram_enabled": false}
```

## API

All download endpoints return a consistent response structure with a `summary` field for display purposes.

### GET `/d` — Quick download (recommended)

The simplest endpoint.  Works great with iOS Shortcuts or a browser.

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

### POST `/download` — JSON download

More control via JSON body.  Use the `sync` field to choose async/sync.

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

### GET `/task/{task_id}` — Task status

```bash
curl http://localhost:8000/task/a1b2c3d4e5f6
```

### GET `/health` — Health check

```bash
curl http://localhost:8000/health
```

### GET/POST `/reset` — Reset download history

Clears the download directory, DB records, and in-memory caches.  The next request will re-download everything and re-upload to Immich.  Useful after you've manually deleted files in Immich.

```bash
curl http://localhost:8000/reset
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

## iOS Shortcut Setup

Create a Shortcut that triggers a download from the Douyin share sheet:

1. **New Shortcut** → add a "Receive Input" action
2. **Add "Get Contents of URL"** (not "Open URL")
   - URL: `https://your-domain.com/d?url=<Shortcut Input>&sync=1`
   - Method: GET
3. **Add "Get Dictionary Value"**
   - Get `summary` from the URL contents
4. **Add "Show Notification"**
   - Title: Douyin Download
   - Body: the `summary` value from the previous step

> Expose the service externally via Cloudflare Tunnel or similar.
>
> If you've deleted files in Immich and want to re-upload, call `/reset` first, then share the link again.

## Configuration Reference

### `config.yml`

```yaml
# -- Downloader core --------------------
path: ./Downloaded/
thread: 5
retry_times: 3
database: true                   # SQLite-based dedup

# -- HTTP service ------------------------
server:
  host: 0.0.0.0
  port: 8000

# -- Immich ------------------------------
immich:
  enabled: true
  api_url: ''                    # Falls back to env IMMICH_API_URL
  api_key: ''                    # Falls back to env IMMICH_API_KEY
  album_prefix: 'douyin-'       # Albums: douyin-<author>
  device_id: 'douyin-downloader'
  upload_timeout: 600
  upload_extensions:
    - .mp4
    - .jpg
    # ... see config.example.yml for full list

# -- Telegram ----------------------------
telegram:
  enabled: false
  bot_token: ''                  # Falls back to env TELEGRAM_BOT_TOKEN
  chat_id: ''                    # Falls back to env TELEGRAM_CHAT_ID
  api_base: 'http://telegram-bot-api:8081'  # or https://api.telegram.org
  caption_template: '**{author}:** {desc} {tags}'  # **bold** / _italic_ auto-converted to HTML
  send_cover: true
  upload_timeout: 600
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DY_CONFIG_PATH` | Path to config file | `config.yml` |
| `IMMICH_API_URL` | Immich API URL (config.yml takes precedence) | — |
| `IMMICH_API_KEY` | Immich API key (config.yml takes precedence) | — |
| `TELEGRAM_BOT_TOKEN` | Bot token (config.yml takes precedence) | — |
| `TELEGRAM_CHAT_ID` | Channel/Group ID (config.yml takes precedence) | — |
| `TELEGRAM_API_ID` | Required for self-hosted Bot API Server (from my.telegram.org) | — |
| `TELEGRAM_API_HASH` | Required for self-hosted Bot API Server (from my.telegram.org) | — |

### `docker-compose.yml` volumes

```yaml
volumes:
  - ./downloads:/app/Downloaded     # Persist downloaded files
  - ./app/config.yml:/app/config.yml:ro  # Mount config (read-only)
```

Restart after editing `config.yml`:

```bash
docker compose restart
```

## Dedup Strategy

Three layers prevent redundant downloads and uploads:

| Layer | Mechanism | Granularity | Persistence |
|-------|-----------|-------------|-------------|
| **API** | In-memory URL → task_id map | URL | Cleared on container restart |
| **Downloader** | SQLite + local file detection | aweme_id | Persistent (clearable via `/reset`) |
| **Immich** | File checksum + trash restore | File content | Persistent (Immich DB) |

## Local Development

```bash
cd app
pip install -r requirements.txt
pip install fastapi uvicorn[standard]

# Run directly
python server.py

# Or with hot reload
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

## Credits

- Core downloader based on [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) V2.0
- Photo management via [Immich](https://immich.app/)
- Telegram Bot API docs: [core.telegram.org/bots/api](https://core.telegram.org/bots/api)