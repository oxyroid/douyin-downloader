#!/usr/bin/env python3
"""
轻量 HTTP 服务，接收 URL 请求后调用 douyin-downloader CLI 核心逻辑执行下载。
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
    """确保中文直接输出而非 \\uxxxx 转义"""
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")

# 确保项目根目录在 sys.path 中
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
from utils.logger import setup_logger, set_console_log_level

logger = setup_logger("Server")
set_console_log_level(logging.INFO)

app = FastAPI(title="Douyin Downloader API", version="2.0.0", default_response_class=UTF8JSONResponse)

# ── 全局单例 ──────────────────────────────────────────────
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


# ── 请求/响应模型 ─────────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str = Field(..., description="抖音链接（视频/图文/用户主页/短链均可）")
    mode: Optional[list[str]] = Field(None, description="下载模式，如 ['post']")
    number_post: Optional[int] = Field(None, description="post 下载数量限制，0=全部")
    thread: Optional[int] = Field(None, description="并发数")


class DownloadResponse(BaseModel):
    task_id: str
    status: str
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    message: str = ""


# ── 正在运行的任务追踪 ───────────────────────────────────
_tasks: dict[str, dict] = {}
# URL → task_id 映射，防止同一 URL 重复下载
_url_to_task: dict[str, str] = {}


def _normalize_url(url: str) -> str:
    """归一化 URL，去掉尾部斜杠和空白"""
    return url.strip().rstrip("/")


def _find_existing_task(url: str) -> Optional[str]:
    """如果该 URL 已有未失败的任务，返回其 task_id"""
    normalized = _normalize_url(url)
    task_id = _url_to_task.get(normalized)
    if task_id and task_id in _tasks:
        status = _tasks[task_id].get("status")
        # pending / running / completed 都视为有效，不再重复提交
        if status in ("pending", "running", "completed"):
            return task_id
    return None


def _register_task(url: str, task_id: str):
    """注册 URL → task_id 映射"""
    _url_to_task[_normalize_url(url)] = task_id


async def _run_download(task_id: str, req: DownloadRequest):
    """后台执行下载任务"""
    try:
        _tasks[task_id]["status"] = "running"

        config = await _get_config()

        # 按请求覆盖配置（不影响全局 _config，使用深拷贝）
        from copy import deepcopy

        task_config = ConfigLoader.__new__(ConfigLoader)
        task_config.config_path = config.config_path
        task_config.config = deepcopy(config.config)

        # 设置本次请求的 URL
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
                message=f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
            )

            # ── Immich 上传 ──────────────────────────────
            immich = get_immich_uploader(config.get("immich", {}))
            if immich:
                try:
                    download_dir = Path(config.get("path", "./Downloaded/"))
                    if download_dir.exists():
                        immich_stats = await immich.upload_directory(download_dir)
                        immich_msg = (
                            f" | Immich: 上传 {immich_stats.get('uploaded', 0)}"
                            f", 重复 {immich_stats.get('duplicates', 0)}"
                            f", 失败 {immich_stats.get('failed', 0)}"
                        )
                        _tasks[task_id]["message"] += immich_msg
                        _tasks[task_id]["immich"] = immich_stats
                        logger.info("Immich 上传完成: %s", immich_stats)
                except Exception as e:
                    logger.exception("Immich 上传失败")
                    _tasks[task_id]["message"] += f" | Immich 上传失败: {e}"
        else:
            _tasks[task_id].update(status="failed", message="下载失败或链接无效")

    except Exception as e:
        logger.exception("Download task %s failed", task_id)
        _tasks[task_id].update(status="failed", message=str(e))


# ── API 路由 ──────────────────────────────────────────────
@app.post("/download", response_model=DownloadResponse)
async def start_download(req: DownloadRequest):
    """提交下载任务（异步执行，立即返回 task_id）"""
    existing = _find_existing_task(req.url)
    if existing:
        info = _tasks[existing]
        return DownloadResponse(task_id=existing, **info)

    task_id = uuid.uuid4().hex[:12]
    _tasks[task_id] = {"status": "pending", "total": 0, "success": 0, "failed": 0, "skipped": 0, "message": ""}
    _register_task(req.url, task_id)

    asyncio.create_task(_run_download(task_id, req))

    return DownloadResponse(task_id=task_id, status="pending", message="任务已提交")


@app.post("/download/sync", response_model=DownloadResponse)
async def sync_download(req: DownloadRequest):
    """同步下载（等待下载完成后再返回结果）"""
    existing = _find_existing_task(req.url)
    if existing:
        info = _tasks[existing]
        return DownloadResponse(task_id=existing, **info)

    task_id = uuid.uuid4().hex[:12]
    _tasks[task_id] = {"status": "pending", "total": 0, "success": 0, "failed": 0, "skipped": 0, "message": ""}
    _register_task(req.url, task_id)

    await _run_download(task_id, req)

    info = _tasks[task_id]
    return DownloadResponse(task_id=task_id, **info)


@app.get("/task/{task_id}", response_model=DownloadResponse)
async def get_task_status(task_id: str):
    """查询异步任务状态"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    info = _tasks[task_id]
    return DownloadResponse(task_id=task_id, **info)


@app.get("/d")
async def quick_download(
    url: str = Query(..., description="抖音链接（需 URL 编码）"),
    sync: bool = Query(False, description="是否同步等待下载完成"),
):
    """
    GET 快捷下载接口。
    - sync=0（默认）: 立即返回，后台异步执行
    - sync=1: 等待下载+上传完成后返回结果（适合 iOS 快捷指令）

    用法: /d?url=<encoded_url>&sync=1
    """
    decoded_url = unquote(url)

    existing = _find_existing_task(decoded_url)
    if existing:
        info = _tasks[existing]
        return {"task_id": existing, "status": info["status"], "url": decoded_url,
                "message": info.get("message") or "该链接已提交过",
                "summary": _build_summary(info)}

    task_id = uuid.uuid4().hex[:12]
    _tasks[task_id] = {"status": "pending", "total": 0, "success": 0, "failed": 0, "skipped": 0, "message": ""}
    _register_task(decoded_url, task_id)

    req = DownloadRequest(url=decoded_url)

    if sync:
        await _run_download(task_id, req)
        info = _tasks[task_id]
        return {"task_id": task_id, "status": info["status"], "url": decoded_url,
                "message": info.get("message", ""),
                "summary": _build_summary(info)}
    else:
        asyncio.create_task(_run_download(task_id, req))
        return {"task_id": task_id, "status": "pending", "url": decoded_url, "message": "任务已提交"}


def _build_summary(info: dict) -> str:
    """为 iOS 快捷指令生成一行精简摘要"""
    status = info.get("status", "unknown")
    if status == "completed":
        parts = []
        s, f, sk = info.get("success", 0), info.get("failed", 0), info.get("skipped", 0)
        if s:
            parts.append(f"✅ {s}个下载成功")
        if sk:
            parts.append(f"⏭️ {sk}个已下载过，跳过")
        if f:
            parts.append(f"❌ {f}个失败")
        immich = info.get("immich", {})
        if immich:
            uploaded = immich.get("uploaded", 0)
            duplicates = immich.get("duplicates", 0)
            im_failed = immich.get("failed", 0)
            if uploaded:
                parts.append(f"📤 {uploaded}个已上传Immich")
            if duplicates:
                parts.append(f"☁️ Immich中已有{duplicates}个")
            if im_failed:
                parts.append(f"⚠️ Immich上传失败{im_failed}个")
        return "\n".join(parts) if parts else "✅ 完成"
    elif status == "failed":
        msg = info.get("message", "未知错误")
        # 截断过长的错误信息
        if len(msg) > 80:
            msg = msg[:77] + "..."
        return f"❌ 失败: {msg}"
    elif status == "running":
        return "⏳ 下载中..."
    else:
        return "⏳ 排队中..."


@app.get("/health")
async def health_check():
    config = await _get_config()
    immich = get_immich_uploader(config.get("immich", {}))
    return {
        "status": "ok",
        "immich_enabled": immich is not None,
    }


@app.on_event("shutdown")
async def shutdown_event():
    immich = get_immich_uploader()
    if immich:
        await immich.close()


if __name__ == "__main__":
    import uvicorn

    async def _load_server_config():
        config = await _get_config()
        server_cfg = config.get("server", {})
        return server_cfg.get("host", "0.0.0.0"), server_cfg.get("port", 8000)

    import asyncio as _asyncio
    _host, _port = _asyncio.run(_load_server_config())
    uvicorn.run(app, host=_host, port=_port)
