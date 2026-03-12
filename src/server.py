#!/usr/bin/env python3
"""
Lightweight HTTP service that receives URL requests and invokes the
douyin-downloader CLI core to perform downloads.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import aiohttp

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field


class UTF8JSONResponse(JSONResponse):
    """JSON response that outputs CJK characters directly instead of \\uxxxx escapes."""
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")

# Ensure the app directory is on sys.path
project_root = Path(__file__).parent.parent  # src/../ = project root
app_dir = project_root / "app"
src_dir = Path(__file__).parent  # src/
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

os.chdir(app_dir)

from config import ConfigLoader
from auth import CookieManager
from storage import Database
from cli.main import download_url
from uploaders.immich import get_immich_uploader
from uploaders.telegram import get_telegram_uploader
from app.utils.logger import setup_logger, set_console_log_level

logger = setup_logger("Server")
set_console_log_level(logging.INFO)


def _count_manifest_lines(manifest_path: Path) -> int:
    """Return the current line count of the manifest file."""
    if not manifest_path.exists():
        return 0
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _read_new_manifest_entries(manifest_path: Path, skip_lines: int) -> list[dict]:
    """Read manifest entries appended after *skip_lines*."""
    if not manifest_path.exists():
        return []
    entries = []
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < skip_lines:
                    continue
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as e:
        logger.warning("Failed to read new manifest entries: %s", e)
    return entries


async def _resolve_short_url(url: str) -> str:
    """Follow redirects for Douyin short links; return the original URL otherwise."""
    if "v.douyin.com" not in url:
        return url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resolved = str(resp.url)
                logger.info("Short URL resolved: %s -> %s", url, resolved)
                return resolved
    except Exception as e:
        logger.warning("Short URL resolution failed: %s -> %s", url, e)
        return url


def _find_manifest_entries_by_url(manifest_path: Path, url: str) -> list[dict]:
    """Find manifest entries matching a URL (by aweme_id or exact url field)."""
    if not manifest_path.exists():
        return []
    entries = []
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                entry_url = entry.get("url", "")
                aweme_id = entry.get("aweme_id", "")
                if (aweme_id and aweme_id in url) or (entry_url and entry_url == url):
                    entries.append(entry)
    except Exception as e:
        logger.warning("Manifest lookup failed: %s", e)
    return entries


# -- Custom Exceptions -------------------------------------------------
class ConfigurationError(Exception):
    """Raised when configuration loading fails."""
    pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI application."""
    # -- Startup --
    try:
        config = await _get_config()
        cookies = config.get("cookies", {})
        status = _check_cookies_status(cookies)

        if not status["valid"]:
            logger.warning("=" * 60)
            logger.warning("  ⚠️  COOKIES NOT CONFIGURED")
            logger.warning("=" * 60)
            logger.warning(status["message"])
            logger.warning("")
            logger.warning("To initialize cookies, run on your HOST machine:")
            logger.warning("  pip install playwright pyyaml")
            logger.warning("  playwright install chromium")
            logger.warning("  python scripts/init-cookies.py")
            logger.warning("")
            logger.warning("Or visit: http://localhost:8000/init for detailed instructions.")
            logger.warning("=" * 60)
        else:
            logger.info("✅ Cookies configured and ready.")
    except ConfigurationError as e:
        logger.error("=" * 60)
        logger.error("  ❌  CONFIGURATION ERROR")
        logger.error("=" * 60)
        logger.error(str(e))
        logger.error("=" * 60)
        # Don't raise - let the server start so /health and /init can provide guidance

    yield

    # -- Shutdown --
    try:
        immich = get_immich_uploader()
        if immich:
            await immich.close()
    except Exception as e:
        logger.warning("Error closing Immich uploader: %s", e)

    try:
        tg = get_telegram_uploader()
        if tg:
            await tg.close()
    except Exception as e:
        logger.warning("Error closing Telegram uploader: %s", e)


app = FastAPI(title="Douyin Downloader API", version="2.0.0", default_response_class=UTF8JSONResponse, lifespan=_lifespan)


@app.exception_handler(ConfigurationError)
async def configuration_error_handler(request, exc: ConfigurationError):
    """Return a friendly error when configuration is broken."""
    return JSONResponse(
        status_code=503,
        content={
            "task_id": "",
            "status": "error",
            "message": str(exc),
            "summary": f"Configuration error: {exc}",
        },
    )

# -- Singletons --------------------------------------------------------
_config: Optional[ConfigLoader] = None
_cookie_manager: Optional[CookieManager] = None
_database: Optional[Database] = None

CONFIG_PATH = os.getenv("DY_CONFIG_PATH", "config.yml")


async def _get_config() -> ConfigLoader:
    global _config
    if _config is None:
        try:
            _config = ConfigLoader(CONFIG_PATH)
        except FileNotFoundError:
            raise ConfigurationError(
                f"Config file not found: {CONFIG_PATH}. "
                "Copy config.example.yml to config.yml first."
            )
        except Exception as e:
            raise ConfigurationError(f"Failed to load config: {e}")
    return _config


async def _get_cookie_manager() -> CookieManager:
    global _cookie_manager
    if _cookie_manager is None:
        try:
            config = await _get_config()
            cookies = config.get_cookies()
            _cookie_manager = CookieManager()
            _cookie_manager.set_cookies(cookies)
        except ConfigurationError:
            raise
        except Exception as e:
            logger.error("Failed to initialize cookie manager: %s", e)
            raise ConfigurationError(f"Cookie manager initialization failed: {e}")
    return _cookie_manager


async def _get_database() -> Optional[Database]:
    global _database
    try:
        config = await _get_config()
        if config.get("database") and _database is None:
            _database = Database()
            await _database.initialize()
    except ConfigurationError:
        raise
    except Exception as e:
        logger.warning("Database initialization failed (downloads will still work): %s", e)
        return None
    return _database


# -- Request / Response models -----------------------------------------
class DownloadRequest(BaseModel):
    url: str = Field(..., description="Douyin link (video/gallery/user profile/short link)")
    mode: Optional[list[str]] = Field(None, description="Download mode, e.g. ['post']")
    number_post: Optional[int] = Field(None, description="Max posts to download, 0 = all")
    thread: Optional[int] = Field(None, description="Concurrency")
    sync: bool = Field(False, description="Wait for download to finish before responding")


class DownloadResponse(BaseModel):
    task_id: str
    status: str
    url: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    message: str = ""
    summary: str = ""


# -- In-flight task tracking -------------------------------------------
_tasks: dict[str, dict] = {}
# URL -> task_id mapping to prevent duplicate downloads
_url_to_task: dict[str, str] = {}
# Lock for concurrent access to _tasks / _url_to_task
_task_lock = asyncio.Lock()
# Maximum number of completed tasks to keep in memory
_MAX_COMPLETED_TASKS = 1000
# TTL for completed/failed tasks (seconds) — tasks older than this are eligible for cleanup
_TASK_TTL_SECONDS = 3600  # 1 hour

# -- Metrics -----------------------------------------------------------
_metrics = {
    "downloads_total": 0,
    "downloads_success": 0,
    "downloads_failed": 0,
    "downloads_skipped": 0,
    "immich_uploads_total": 0,
    "immich_uploads_success": 0,
    "immich_uploads_failed": 0,
    "telegram_sends_total": 0,
    "telegram_sends_success": 0,
    "telegram_sends_failed": 0,
    "start_time": time.time(),
}


def _normalize_url(url: str) -> str:
    """Strip trailing slashes and whitespace."""
    return url.strip().rstrip("/")


def _cleanup_old_tasks():
    """Remove expired or excess completed/failed tasks.

    Tasks are removed if they exceed _TASK_TTL_SECONDS or if the total
    number of completed tasks exceeds _MAX_COMPLETED_TASKS.
    """
    now = time.time()
    removed = 0

    # First pass: remove tasks that have exceeded TTL
    for tid in list(_tasks.keys()):
        info = _tasks.get(tid, {})
        if info.get("status") not in ("completed", "failed"):
            continue
        completed_at = info.get("completed_at", 0)
        if completed_at and (now - completed_at) > _TASK_TTL_SECONDS:
            del _tasks[tid]
            for url, mapped_tid in list(_url_to_task.items()):
                if mapped_tid == tid:
                    del _url_to_task[url]
                    break
            removed += 1

    # Second pass: enforce max count
    completed_tasks = [
        (tid, info) for tid, info in _tasks.items()
        if info.get("status") in ("completed", "failed")
    ]

    if len(completed_tasks) > _MAX_COMPLETED_TASKS:
        # Sort by completed_at ascending so oldest are removed first
        completed_tasks.sort(key=lambda x: x[1].get("completed_at", 0))
        to_remove = len(completed_tasks) - _MAX_COMPLETED_TASKS
        for tid, _ in completed_tasks[:to_remove]:
            if tid in _tasks:
                del _tasks[tid]
                for url, mapped_tid in list(_url_to_task.items()):
                    if mapped_tid == tid:
                        del _url_to_task[url]
                        break
                removed += 1

    if removed > 0:
        logger.debug("Cleaned up %d old tasks", removed)


def _find_existing_task(url: str) -> Optional[str]:
    """Return the task_id if a non-failed task already exists for this URL."""
    normalized = _normalize_url(url)
    task_id = _url_to_task.get(normalized)
    if task_id and task_id in _tasks:
        status = _tasks[task_id].get("status")
        if status in ("pending", "running", "completed"):
            return task_id
    return None


def _register_task(url: str, task_id: str):
    """Register a URL -> task_id mapping."""
    _url_to_task[_normalize_url(url)] = task_id


async def _run_download(task_id: str, req: DownloadRequest):
    """Execute a download task (runs in background or inline)."""
    try:
        _tasks[task_id]["status"] = "running"

        config = await _get_config()

        # Deep-copy config so per-request overrides don't leak globally
        task_config = deepcopy(config)

        task_config.update(link=[req.url])

        if req.mode:
            task_config.update(mode=req.mode)
        if req.number_post is not None:
            number = task_config.get("number", {})
            number["post"] = req.number_post
            task_config.update(number=number)
        if req.thread:
            task_config.update(thread=req.thread)

        cookie_manager = await _get_cookie_manager()
        database = await _get_database()

        # Record manifest line count before download so we can find new entries
        download_dir = Path(config.get("path", "./Downloaded/"))
        manifest_path = download_dir / "download_manifest.jsonl"
        manifest_lines_before = _count_manifest_lines(manifest_path)

        result = await download_url(
            req.url,
            task_config,
            cookie_manager,
            database,
            progress_reporter=None,
        )

        if result:
            _tasks[task_id].update(
                status="completed",
                completed_at=time.time(),
                total=result.total,
                success=result.success,
                failed=result.failed,
                skipped=result.skipped,
                message=f"ok {result.success} / fail {result.failed} / skip {result.skipped}",
            )
            
            # Update metrics
            _metrics["downloads_total"] += 1
            _metrics["downloads_success"] += result.success
            _metrics["downloads_failed"] += result.failed
            _metrics["downloads_skipped"] += result.skipped

            # -- Collect files produced by this download --
            new_entries = _read_new_manifest_entries(manifest_path, manifest_lines_before)
            new_files: list[Path] = []
            for entry in new_entries:
                for rel_path in entry.get("file_paths", []):
                    full_path = download_dir / rel_path
                    if full_path.exists():
                        new_files.append(full_path)

            # -- Immich upload --
            immich = get_immich_uploader(config.get("immich", {}))
            if immich:
                try:
                    if new_files:
                        immich_stats = await immich.upload_files(
                            new_files, download_dir, force=True
                        )
                        restored = immich_stats.get("restored", 0)
                        uploaded = immich_stats.get("uploaded", 0)
                        immich_msg = (
                            f" | Immich: uploaded {uploaded}"
                            f", restored {restored}"
                            f", dup {immich_stats.get('duplicates', 0)}"
                            f", failed {immich_stats.get('failed', 0)}"
                        )
                        _tasks[task_id]["message"] += immich_msg
                        _tasks[task_id]["immich"] = immich_stats
                        logger.info("Immich upload done: %s", immich_stats)
                        
                        # Update Immich metrics
                        _metrics["immich_uploads_total"] += uploaded + restored + immich_stats.get("duplicates", 0) + immich_stats.get("failed", 0)
                        _metrics["immich_uploads_success"] += uploaded + restored
                        _metrics["immich_uploads_failed"] += immich_stats.get("failed", 0)
                    else:
                        logger.info("No new files to upload to Immich")
                        _tasks[task_id]["immich"] = {
                            "uploaded": 0, "restored": 0, "duplicates": 0, "failed": 0,
                        }
                except Exception as e:
                    logger.exception("Immich upload failed")
                    _tasks[task_id]["message"] += f" | Immich upload failed: {e}"

            # -- Telegram upload --
            # Always scan manifest for matching files, even if nothing new was downloaded
            tg = get_telegram_uploader(config.get("telegram", {}))
            if tg:
                try:
                    # Prefer newly downloaded files; otherwise resolve short URL and match
                    tg_entries = new_entries
                    tg_files: list[Path] = []
                    if new_entries:
                        tg_files = list(new_files)
                    else:
                        resolved_url = await _resolve_short_url(req.url)
                        tg_entries = _find_manifest_entries_by_url(manifest_path, resolved_url)
                        for entry in tg_entries:
                            for rel_path in entry.get("file_paths", []):
                                full_path = download_dir / rel_path
                                if full_path.exists():
                                    tg_files.append(full_path)

                    if tg_files:
                        tg_stats = await tg.upload_files(
                            tg_files, download_dir,
                            manifest_entries=tg_entries,
                        )
                        tg_msg = (
                            f" | Telegram: sent {tg_stats.get('sent', 0)}"
                            f", skip {tg_stats.get('skipped', 0)}"
                            f", fail {tg_stats.get('failed', 0)}"
                        )
                        _tasks[task_id]["message"] += tg_msg
                        _tasks[task_id]["telegram"] = tg_stats
                        logger.info("Telegram send done: %s", tg_stats)
                        
                        # Update Telegram metrics
                        _metrics["telegram_sends_total"] += tg_stats.get("sent", 0) + tg_stats.get("failed", 0)
                        _metrics["telegram_sends_success"] += tg_stats.get("sent", 0)
                        _metrics["telegram_sends_failed"] += tg_stats.get("failed", 0)
                    else:
                        logger.info("No matching files in manifest for Telegram")
                        _tasks[task_id]["telegram"] = {
                            "sent": 0, "skipped": 0, "failed": 0,
                        }
                except Exception as e:
                    logger.exception("Telegram send failed")
                    _tasks[task_id]["message"] += f" | Telegram send failed: {e}"
        else:
            _tasks[task_id].update(status="failed", completed_at=time.time(), message="Download failed or invalid link")

    except Exception as e:
        logger.exception("Download task %s failed", task_id)
        _tasks[task_id].update(status="failed", completed_at=time.time(), message=str(e))
    finally:
        # Clean up old tasks to prevent memory leak
        _cleanup_old_tasks()


# -- Task helpers ------------------------------------------------------
def _task_to_response(task_id: str, url: str = "") -> DownloadResponse:
    """Convert internal task dict to a DownloadResponse."""
    info = _tasks.get(task_id, {})
    return DownloadResponse(
        task_id=task_id,
        url=url,
        status=info.get("status", "unknown"),
        total=info.get("total", 0),
        success=info.get("success", 0),
        failed=info.get("failed", 0),
        skipped=info.get("skipped", 0),
        message=info.get("message", ""),
        summary=_build_summary(info),
    )


async def _submit_task(url: str, req: DownloadRequest) -> DownloadResponse:
    """Shared task submission: dedup check -> create task -> sync/async exec."""
    # Check dedup against original URL
    existing = _find_existing_task(url)
    if existing:
        return _task_to_response(existing, url)

    # Also check resolved URL for short links to avoid duplicate downloads
    if "v.douyin.com" in url:
        resolved = await _resolve_short_url(url)
        if resolved != url:
            existing = _find_existing_task(resolved)
            if existing:
                return _task_to_response(existing, url)

    task_id = uuid.uuid4().hex[:12]
    _tasks[task_id] = {
        "status": "pending", "total": 0, "success": 0,
        "failed": 0, "skipped": 0, "message": "",
    }
    _register_task(url, task_id)

    if req.sync:
        await _run_download(task_id, req)
    else:
        # Fire-and-forget with error logging
        task = asyncio.create_task(_run_download(task_id, req))
        task.add_done_callback(_handle_task_exception)

    return _task_to_response(task_id, url)


def _handle_task_exception(task: asyncio.Task):
    """Log exceptions from background tasks that would otherwise be silently lost."""
    try:
        exc = task.exception()
        if exc:
            logger.error("Background task failed with exception: %s", exc, exc_info=exc)
    except asyncio.CancelledError:
        pass


# -- API routes --------------------------------------------------------
@app.post("/download", response_model=DownloadResponse)
async def download(req: DownloadRequest):
    """Download endpoint (POST JSON).

    sync=false (default): run in background, return task_id immediately.
    sync=true: block until download + upload finishes, then return result.
    """
    return await _submit_task(req.url, req)


@app.get("/d", response_model=DownloadResponse)
async def quick_download(
    url: str = Query(..., description="Douyin link (URL-encoded)"),
    sync: bool = Query(False, description="Wait for completion"),
):
    """GET shortcut for downloading. Handy for browsers and iOS Shortcuts.

    Usage: /d?url=<encoded_url>&sync=1
    """
    decoded_url = unquote(url)
    req = DownloadRequest(url=decoded_url, sync=sync)
    return await _submit_task(decoded_url, req)


@app.get("/task/{task_id}", response_model=DownloadResponse)
async def get_task_status(task_id: str):
    """Query the status of an async task."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task_id)


def _build_summary(info: dict) -> str:
    """Build a short summary string (used by iOS Shortcuts notifications)."""
    status = info.get("status", "unknown")
    if status == "completed":
        parts = []
        s, f, sk = info.get("success", 0), info.get("failed", 0), info.get("skipped", 0)
        if s:
            parts.append(f"{s} downloaded")
        if sk:
            parts.append(f"{sk} already existed, skipped")
        if f:
            parts.append(f"{f} failed")
        immich = info.get("immich", {})
        if immich:
            uploaded = immich.get("uploaded", 0)
            restored = immich.get("restored", 0)
            duplicates = immich.get("duplicates", 0)
            im_failed = immich.get("failed", 0)
            if uploaded:
                parts.append(f"{uploaded} uploaded to Immich")
            if restored:
                parts.append(f"{restored} restored from Immich trash")
            if duplicates:
                parts.append(f"{duplicates} already in Immich")
            if im_failed:
                parts.append(f"{im_failed} Immich upload failed")
        tg = info.get("telegram", {})
        if tg:
            tg_sent = tg.get("sent", 0)
            tg_failed = tg.get("failed", 0)
            if tg_sent:
                parts.append(f"{tg_sent} sent to Telegram")
            if tg_failed:
                parts.append(f"{tg_failed} Telegram send failed")
        return "\n".join(parts) if parts else "Done"
    elif status == "failed":
        msg = info.get("message", "unknown error")
        if len(msg) > 80:
            msg = msg[:77] + "..."
        return f"Failed: {msg}"
    elif status == "running":
        return "Downloading..."
    else:
        return "Queued..."


@app.get("/health")
async def health_check():
    try:
        config = await _get_config()
        immich = get_immich_uploader(config.get("immich", {}))
        tg = get_telegram_uploader(config.get("telegram", {}))
        cookies = config.get("cookies", {})
        cookies_status = _check_cookies_status(cookies)
        return {
            "status": "ok",
            "immich_enabled": immich is not None,
            "telegram_enabled": tg is not None,
            "cookies_status": cookies_status,
        }
    except ConfigurationError as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": str(e),
                "immich_enabled": False,
                "telegram_enabled": False,
                "cookies_status": {"valid": False, "message": str(e), "missing": []},
            },
        )


@app.get("/health/deep")
async def deep_health_check():
    """Deep health check that tests actual connectivity to Immich and Telegram."""
    result = {
        "status": "ok",
        "checks": {},
    }
    
    try:
        config = await _get_config()
        result["checks"]["config"] = {"status": "ok"}
    except ConfigurationError as e:
        result["status"] = "degraded"
        result["checks"]["config"] = {"status": "error", "message": str(e)}
        return JSONResponse(status_code=503, content=result)
    
    # Check cookies
    cookies = config.get("cookies", {})
    cookies_status = _check_cookies_status(cookies)
    if cookies_status["valid"]:
        result["checks"]["cookies"] = {"status": "ok"}
    else:
        result["status"] = "degraded"
        result["checks"]["cookies"] = {"status": "warning", "message": cookies_status["message"]}
    
    # Check Immich connectivity
    immich = get_immich_uploader(config.get("immich", {}))
    if immich:
        try:
            async with aiohttp.ClientSession(
                headers={"x-api-key": immich.api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.get(f"{immich.api_url}/api/server/ping") as resp:
                    if resp.status == 200:
                        result["checks"]["immich"] = {"status": "ok", "url": immich.api_url}
                    else:
                        result["status"] = "degraded"
                        result["checks"]["immich"] = {
                            "status": "error",
                            "message": f"HTTP {resp.status}",
                            "url": immich.api_url,
                        }
        except Exception as e:
            result["status"] = "degraded"
            result["checks"]["immich"] = {
                "status": "error",
                "message": str(e),
                "url": immich.api_url,
            }
    else:
        result["checks"]["immich"] = {"status": "disabled"}
    
    # Check Telegram connectivity
    tg = get_telegram_uploader(config.get("telegram", {}))
    if tg:
        try:
            tg_api_url = f"{tg.api_base}/bot{tg.bot_token}"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{tg_api_url}/getMe") as resp:
                    body = await resp.json()
                    if body.get("ok"):
                        bot_info = body.get("result", {})
                        result["checks"]["telegram"] = {
                            "status": "ok",
                            "bot_username": bot_info.get("username", "unknown"),
                        }
                    else:
                        result["status"] = "degraded"
                        result["checks"]["telegram"] = {
                            "status": "error",
                            "message": body.get("description", "Unknown error"),
                        }
        except Exception as e:
            result["status"] = "degraded"
            result["checks"]["telegram"] = {"status": "error", "message": str(e)}
    else:
        result["checks"]["telegram"] = {"status": "disabled"}
    
    status_code = 200 if result["status"] == "ok" else 503
    return JSONResponse(status_code=status_code, content=result)


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    uptime = time.time() - _metrics["start_time"]
    
    # Count task statuses
    pending = sum(1 for t in _tasks.values() if t.get("status") == "pending")
    running = sum(1 for t in _tasks.values() if t.get("status") == "running")
    completed = sum(1 for t in _tasks.values() if t.get("status") == "completed")
    failed = sum(1 for t in _tasks.values() if t.get("status") == "failed")
    
    lines = [
        "# HELP douyin_uptime_seconds Server uptime in seconds",
        "# TYPE douyin_uptime_seconds gauge",
        f"douyin_uptime_seconds {uptime:.2f}",
        "",
        "# HELP douyin_tasks_total Total tasks by status",
        "# TYPE douyin_tasks_total gauge",
        f'douyin_tasks_total{{status="pending"}} {pending}',
        f'douyin_tasks_total{{status="running"}} {running}',
        f'douyin_tasks_total{{status="completed"}} {completed}',
        f'douyin_tasks_total{{status="failed"}} {failed}',
        "",
        "# HELP douyin_downloads_total Total download operations",
        "# TYPE douyin_downloads_total counter",
        f"douyin_downloads_total {_metrics['downloads_total']}",
        "",
        "# HELP douyin_downloads_success_total Successful downloads",
        "# TYPE douyin_downloads_success_total counter",
        f"douyin_downloads_success_total {_metrics['downloads_success']}",
        "",
        "# HELP douyin_downloads_failed_total Failed downloads",
        "# TYPE douyin_downloads_failed_total counter",
        f"douyin_downloads_failed_total {_metrics['downloads_failed']}",
        "",
        "# HELP douyin_downloads_skipped_total Skipped downloads",
        "# TYPE douyin_downloads_skipped_total counter",
        f"douyin_downloads_skipped_total {_metrics['downloads_skipped']}",
        "",
        "# HELP douyin_immich_uploads_total Total Immich upload attempts",
        "# TYPE douyin_immich_uploads_total counter",
        f"douyin_immich_uploads_total {_metrics['immich_uploads_total']}",
        "",
        "# HELP douyin_immich_uploads_success_total Successful Immich uploads",
        "# TYPE douyin_immich_uploads_success_total counter",
        f"douyin_immich_uploads_success_total {_metrics['immich_uploads_success']}",
        "",
        "# HELP douyin_immich_uploads_failed_total Failed Immich uploads",
        "# TYPE douyin_immich_uploads_failed_total counter",
        f"douyin_immich_uploads_failed_total {_metrics['immich_uploads_failed']}",
        "",
        "# HELP douyin_telegram_sends_total Total Telegram send attempts",
        "# TYPE douyin_telegram_sends_total counter",
        f"douyin_telegram_sends_total {_metrics['telegram_sends_total']}",
        "",
        "# HELP douyin_telegram_sends_success_total Successful Telegram sends",
        "# TYPE douyin_telegram_sends_success_total counter",
        f"douyin_telegram_sends_success_total {_metrics['telegram_sends_success']}",
        "",
        "# HELP douyin_telegram_sends_failed_total Failed Telegram sends",
        "# TYPE douyin_telegram_sends_failed_total counter",
        f"douyin_telegram_sends_failed_total {_metrics['telegram_sends_failed']}",
        "",
    ]
    return "\n".join(lines)


@app.post("/reload-config")
async def reload_config():
    """Reload configuration from disk.
    
    This resets the config, cookie manager, and uploader singletons,
    allowing changes to config.yml to take effect without restarting.
    """
    global _config, _cookie_manager
    
    # Close existing uploaders
    try:
        immich = get_immich_uploader()
        if immich:
            await immich.close()
    except Exception as e:
        logger.warning("Error closing Immich uploader during reload: %s", e)
    
    try:
        tg = get_telegram_uploader()
        if tg:
            await tg.close()
    except Exception as e:
        logger.warning("Error closing Telegram uploader during reload: %s", e)
    
    # Reset singletons
    _config = None
    _cookie_manager = None
    
    # Reset uploader singletons (they use module-level globals)
    from uploaders import immich as immich_module
    from uploaders import telegram as telegram_module
    immich_module._uploader = None
    telegram_module._uploader = None
    
    # Reload config
    try:
        config = await _get_config()
        cookies = config.get("cookies", {})
        cookies_status = _check_cookies_status(cookies)
        
        # Recreate uploaders
        new_immich = get_immich_uploader(config.get("immich", {}))
        new_tg = get_telegram_uploader(config.get("telegram", {}))
        
        logger.info("Configuration reloaded successfully")
        return {
            "status": "ok",
            "message": "Configuration reloaded",
            "immich_enabled": new_immich is not None,
            "telegram_enabled": new_tg is not None,
            "cookies_status": cookies_status,
        }
    except ConfigurationError as e:
        logger.error("Configuration reload failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Reload failed: {e}",
            },
        )


def _check_cookies_status(cookies: dict) -> dict:
    """Check if cookies are configured and likely valid."""
    required_keys = {"msToken", "ttwid", "odin_tt", "passport_csrf_token"}
    present_keys = {k for k, v in cookies.items() if v and not v.startswith("YOUR_")}
    missing_keys = required_keys - present_keys
    
    # Check if cookies look like placeholders
    placeholder_keys = {k for k, v in cookies.items() if v and v.startswith("YOUR_")}
    
    if not present_keys or placeholder_keys:
        return {
            "valid": False,
            "message": "Cookies not configured. Run init-cookies.py first.",
            "missing": list(missing_keys | placeholder_keys),
        }
    elif missing_keys:
        return {
            "valid": False,
            "message": f"Missing required cookies: {', '.join(sorted(missing_keys))}",
            "missing": list(missing_keys),
        }
    else:
        return {
            "valid": True,
            "message": "Cookies configured",
            "missing": [],
        }


@app.get("/init")
async def init_info():
    """Return initialization instructions for first-time setup."""
    try:
        config = await _get_config()
        cookies = config.get("cookies", {})
        cookies_status = _check_cookies_status(cookies)
    except ConfigurationError as e:
        cookies_status = {"valid": False, "message": str(e), "missing": []}
    
    return {
        "cookies_status": cookies_status,
        "instructions": {
            "step1": "Install dependencies on your HOST machine (not in Docker):",
            "step1_commands": [
                "pip install playwright pyyaml",
                "playwright install chromium",
            ],
            "step2": "Run the cookie initialization script:",
            "step2_commands": [
                "python scripts/init-cookies.py",
            ],
            "step3": "Restart the Docker container:",
            "step3_commands": [
                "docker compose up -d --build --force-recreate",
            ],
            "note": "The script will open a browser for you to log in to Douyin. After logging in, press Enter in the terminal to save cookies.",
        },
    }


@app.post("/reset")
async def reset_downloads():
    """Wipe the download directory, manifest, and DB records.

    After this, subsequent requests will re-download everything and
    re-upload to Immich.
    """
    config = await _get_config()
    download_dir = Path(config.get("path", "./Downloaded/"))

    removed_files = 0
    removed_dirs = 0

    if download_dir.exists():
        for child in list(download_dir.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                    removed_dirs += 1
                elif child.is_file():
                    child.unlink()
                    removed_files += 1
            except Exception as e:
                logger.warning("Cleanup failed: %s -> %s", child, e)

    database = await _get_database()
    db_cleared = False
    if database:
        try:
            await database.clear_downloads()
            db_cleared = True
        except Exception as e:
            logger.warning("Failed to clear DB records: %s", e)

    _tasks.clear()
    _url_to_task.clear()

    logger.info(
        "Reset done: removed %d dir(s) + %d file(s), db %s",
        removed_dirs, removed_files, "cleared" if db_cleared else "skipped",
    )

    result = {
        "status": "ok",
        "removed_dirs": removed_dirs,
        "removed_files": removed_files,
        "db_cleared": db_cleared,
    }

    parts = []
    if removed_dirs or removed_files:
        parts.append(f"Removed {removed_dirs} dir(s) + {removed_files} file(s)")
    else:
        parts.append("Download directory already empty")
    if db_cleared:
        parts.append("DB records cleared")
    parts.append("Next request will re-download and re-upload to Immich")
    result["summary"] = "\n".join(parts)

    return result


if __name__ == "__main__":
    import uvicorn

    async def _load_server_config():
        config = await _get_config()
        server_cfg = config.get("server", {})
        return server_cfg.get("host", "0.0.0.0"), server_cfg.get("port", 8000)

    import asyncio as _asyncio
    _host, _port = _asyncio.run(_load_server_config())
    uvicorn.run(app, host=_host, port=_port)
