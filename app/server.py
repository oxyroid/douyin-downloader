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


def _count_manifest_lines(manifest_path: Path) -> int:
    """统计 manifest 文件当前行数"""
    if not manifest_path.exists():
        return 0
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _read_new_manifest_entries(manifest_path: Path, skip_lines: int) -> list[dict]:
    """读取 manifest 文件中从 skip_lines 之后新增的行"""
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
        logger.warning("读取 manifest 新增条目失败: %s", e)
    return entries

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

        # 记录下载前 manifest 行数，用于识别本次下载新增的文件
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
                message=f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
            )

            # ── Immich 上传：只上传本次下载产生的文件 ──
            immich = get_immich_uploader(config.get("immich", {}))
            if immich:
                try:
                    # 从 manifest 中读取本次下载新增的条目
                    new_entries = _read_new_manifest_entries(manifest_path, manifest_lines_before)

                    # 收集本次下载的所有文件路径
                    new_files: list[Path] = []
                    for entry in new_entries:
                        for rel_path in entry.get("file_paths", []):
                            full_path = download_dir / rel_path
                            if full_path.exists():
                                new_files.append(full_path)

                    if new_files:
                        immich_stats = await immich.upload_files(
                            new_files, download_dir, force=True
                        )
                        restored = immich_stats.get("restored", 0)
                        uploaded = immich_stats.get("uploaded", 0)
                        immich_msg = (
                            f" | Immich: 上传 {uploaded}"
                            f", 恢复 {restored}"
                            f", 重复 {immich_stats.get('duplicates', 0)}"
                            f", 失败 {immich_stats.get('failed', 0)}"
                        )
                        _tasks[task_id]["message"] += immich_msg
                        _tasks[task_id]["immich"] = immich_stats
                        logger.info("Immich 上传完成: %s", immich_stats)
                    else:
                        logger.info("本次下载无新文件需要上传到 Immich")
                        _tasks[task_id]["immich"] = {
                            "uploaded": 0, "restored": 0, "duplicates": 0, "failed": 0,
                        }
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
            parts.append(f"{s}个下载成功")
        if sk:
            parts.append(f"{sk}个已下载过，跳过")
        if f:
            parts.append(f"{f}个失败")
        immich = info.get("immich", {})
        if immich:
            uploaded = immich.get("uploaded", 0)
            restored = immich.get("restored", 0)
            duplicates = immich.get("duplicates", 0)
            im_failed = immich.get("failed", 0)
            if uploaded:
                parts.append(f"{uploaded}个已上传Immich")
            if restored:
                parts.append(f"{restored}个已从Immich垃圾箱恢复")
            if duplicates:
                parts.append(f"Immich中已有{duplicates}个")
            if im_failed:
                parts.append(f"Immich上传失败{im_failed}个")
        return "\n".join(parts) if parts else "完成"
    elif status == "failed":
        msg = info.get("message", "未知错误")
        # 截断过长的错误信息
        if len(msg) > 80:
            msg = msg[:77] + "..."
        return f"失败: {msg}"
    elif status == "running":
        return "下载中..."
    else:
        return "排队中..."


@app.get("/health")
async def health_check():
    config = await _get_config()
    immich = get_immich_uploader(config.get("immich", {}))
    return {
        "status": "ok",
        "immich_enabled": immich is not None,
    }


@app.api_route("/reset", methods=["GET", "POST"])
async def reset_downloads():
    """
    清理 downloads 目录、manifest 和数据库下载记录，
    使下次请求时下载器不会跳过已有文件，能重新下载并上传到 Immich。
    """
    import shutil

    config = await _get_config()
    download_dir = Path(config.get("path", "./Downloaded/"))

    removed_files = 0
    removed_dirs = 0

    # 1. 清理下载目录中的所有子目录（按作者名分的文件夹）
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
                logger.warning("清理失败: %s → %s", child, e)

    # 2. 清理数据库下载记录
    database = await _get_database()
    db_cleared = False
    if database:
        try:
            await database.clear_downloads()
            db_cleared = True
        except Exception as e:
            logger.warning("清理数据库记录失败: %s", e)

    # 3. 清空内存中的任务缓存
    _tasks.clear()
    _url_to_task.clear()

    logger.info(
        "Reset 完成: 删除 %d 个目录 + %d 个文件, 数据库%s",
        removed_dirs, removed_files, "已清理" if db_cleared else "未清理",
    )

    result = {
        "status": "ok",
        "removed_dirs": removed_dirs,
        "removed_files": removed_files,
        "db_cleared": db_cleared,
    }

    parts = []
    if removed_dirs or removed_files:
        parts.append(f"已清理 {removed_dirs}个目录 + {removed_files}个文件")
    else:
        parts.append("下载目录已为空")
    if db_cleared:
        parts.append("数据库记录已清空")
    parts.append("下次请求将重新下载并上传到Immich")
    result["summary"] = "\n".join(parts)

    return result


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
