#!/usr/bin/env python3
"""
Lightweight HTTP service that receives URL requests and invokes the
douyin-downloader CLI core to perform downloads.
"""

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class UTF8JSONResponse(JSONResponse):
    """JSON response that outputs CJK characters directly instead of \\uxxxx escapes."""
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")

# Ensure the project root is on sys.path
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os

os.chdir(project_root)

from config import ConfigLoader
from auth import CookieManager
from storage import Database
from cli.main import download_url
from immich_uploader import get_immich_uploader
from telegram_uploader import get_telegram_uploader
from utils.logger import setup_logger, set_console_log_level

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
        import aiohttp
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

app = FastAPI(title="Douyin Downloader API", version="2.0.0", default_response_class=UTF8JSONResponse)

# -- Singletons --------------------------------------------------------
_config: Optional[ConfigLoader] = None
_cookie_manager: Optional[CookieManager] = None
_database: Optional[Database] = None

CONFIG_PATH = os.getenv("DY_CONFIG_PATH", "config.yml")


async def _get_config() -> ConfigLoader:
    global _config
    if _config is None:
        _config = ConfigLoader(CONFIG_PATH)
    return _config


async def _get_cookie_manager() -> CookieManager:
    global _cookie_manager
    if _cookie_manager is None:
        config = await _get_config()
        cookies = config.get_cookies()
        _cookie_manager = CookieManager()
        _cookie_manager.set_cookies(cookies)
    return _cookie_manager


async def _get_database() -> Optional[Database]:
    global _database
    config = await _get_config()
    if config.get("database") and _database is None:
        _database = Database()
        await _database.initialize()
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


def _normalize_url(url: str) -> str:
    """Strip trailing slashes and whitespace."""
    return url.strip().rstrip("/")


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
        from copy import deepcopy

        task_config = ConfigLoader.__new__(ConfigLoader)
        task_config.config_path = config.config_path
        task_config.config = deepcopy(config.config)

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
                total=result.total,
                success=result.success,
                failed=result.failed,
                skipped=result.skipped,
                message=f"ok {result.success} / fail {result.failed} / skip {result.skipped}",
            )

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
                    else:
                        logger.info("No matching files in manifest for Telegram")
                        _tasks[task_id]["telegram"] = {
                            "sent": 0, "skipped": 0, "failed": 0,
                        }
                except Exception as e:
                    logger.exception("Telegram send failed")
                    _tasks[task_id]["message"] += f" | Telegram send failed: {e}"
        else:
            _tasks[task_id].update(status="failed", message="Download failed or invalid link")

    except Exception as e:
        logger.exception("Download task %s failed", task_id)
        _tasks[task_id].update(status="failed", message=str(e))


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
    existing = _find_existing_task(url)
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
        asyncio.create_task(_run_download(task_id, req))

    return _task_to_response(task_id, url)


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
    config = await _get_config()
    immich = get_immich_uploader(config.get("immich", {}))
    tg = get_telegram_uploader(config.get("telegram", {}))
    return {
        "status": "ok",
        "immich_enabled": immich is not None,
        "telegram_enabled": tg is not None,
    }


@app.api_route("/reset", methods=["GET", "POST"])
async def reset_downloads():
    """Wipe the download directory, manifest, and DB records.

    After this, subsequent requests will re-download everything and
    re-upload to Immich.
    """
    import shutil

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


@app.on_event("shutdown")
async def shutdown_event():
    immich = get_immich_uploader()
    if immich:
        await immich.close()
    tg = get_telegram_uploader()
    if tg:
        await tg.close()


if __name__ == "__main__":
    import uvicorn

    async def _load_server_config():
        config = await _get_config()
        server_cfg = config.get("server", {})
        return server_cfg.get("host", "0.0.0.0"), server_cfg.get("port", 8000)

    import asyncio as _asyncio
    _host, _port = _asyncio.run(_load_server_config())
    uvicorn.run(app, host=_host, port=_port)
