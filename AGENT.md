# AGENT.md

Guide for AI agents working on this codebase.

## Project Structure

```
├── app/                    # Git submodule → jiji262/douyin-downloader (DO NOT EDIT)
├── src/                    # Our custom code
│   ├── server.py           # FastAPI HTTP service (main entry)
│   └── uploaders/
│       ├── immich.py       # Immich upload logic
│       └── telegram.py     # Telegram Bot API upload
├── scripts/
│   └── init-cookies.py     # Cookie initialization (runs on host, not Docker)
├── config.yml              # Runtime config (gitignored, contains secrets)
├── config.example.yml      # Config template
├── Dockerfile
└── docker-compose.yml
```

## Key Points

### 1. `app/` is a git submodule

- **Never modify files in `app/`** — it tracks upstream `jiji262/douyin-downloader`
- All custom code goes in `src/`
- Import from `app/` like: `from app.utils.logger import setup_logger`

### 2. Docker is the primary runtime

- Dev loop: `docker compose up -d --build`
- Logs: `docker logs -f douyin-downloader`
- The container runs `src/server.py` via uvicorn
- Working directory inside container is `/app`

### 3. Config lives in `config.yml`

- Sensitive values (cookies, API keys) — never commit
- `config.example.yml` is the template users copy from
- Config is loaded via `app/config/config_loader.py`

### 4. Cookies are required

- Douyin downloads fail without valid cookies
- `scripts/init-cookies.py` uses Playwright to grab cookies from browser
- Cookies expire periodically — users re-run the script when downloads fail

## Code Patterns

### Adding a new uploader

1. Create `src/uploaders/your_uploader.py`
2. Implement async upload method
3. Add singleton getter like `get_your_uploader(config)`
4. Wire it up in `src/server.py` after download completes

### Telegram specifics

- `api_base` empty → uses official API (50MB limit)
- `api_base` set → self-hosted Bot API Server (2GB limit)
- Cover + video are grouped into MediaGroup
- `_log_size_limit_hint()` shows config hints when files are too large

### Response format

All endpoints return JSON with a `summary` field for iOS Shortcuts:
```python
{
    "task_id": "...",
    "status": "completed",
    "summary": "1 downloaded\n2 sent to Telegram"  # Human-readable
}
```

## Testing

```bash
# Quick test
curl "http://localhost:8000/d?url=https://v.douyin.com/xxx&sync=1"

# Health check (basic)
curl http://localhost:8000/health

# Health check (deep - tests Immich/Telegram connectivity)
curl http://localhost:8000/health/deep

# Prometheus metrics
curl http://localhost:8000/metrics

# Reload config without restart
curl -X POST http://localhost:8000/reload-config

# Reset all downloads
curl -X POST http://localhost:8000/reset
```

## Common Pitfalls

1. **Editing `app/` files** — Don't. Changes will be lost on submodule update.

2. **Empty `api_base`** — Must default to `https://api.telegram.org`, not empty string.

3. **Cookie expiration** — Downloads randomly fail? Cookies probably expired.

4. **Docker context** — `config.yml` is mounted read-only. Edit on host, then `docker compose restart`.

5. **File paths** — Inside container paths start with `/app/`. Downloads go to `/app/Downloaded/`.

## Useful Commands

```bash
# Rebuild and restart
docker compose up -d --build

# View logs
docker logs -f douyin-downloader

# Enter container
docker exec -it douyin-downloader bash

# Update submodule
git submodule update --remote app

# Check git status (should show nothing in app/)
git status
```

## Style Guidelines

- **English only in code and UI-facing strings.** This is an English-language project. Do not use Chinese or other non-English text in code, comments, button labels, captions, log messages, or any user-facing strings. Use emoji or universal symbols when words aren't needed.

- No emoji in code.
