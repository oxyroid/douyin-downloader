#!/usr/bin/env python3
"""
Telegram 上传模块：将下载完成的媒体文件通过 Telegram Bot API 发送到指定 Channel。

直接使用 aiohttp 调用 Telegram Bot API（无额外依赖）:
  POST /bot<token>/sendVideo
  POST /bot<token>/sendPhoto
  POST /bot<token>/sendDocument
  POST /bot<token>/sendMediaGroup
"""

import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

from utils.logger import setup_logger

logger = setup_logger("TelegramUploader")

# Telegram Bot API 单文件上传限制: 50MB (通过 multipart 方式)
_MAX_FILE_SIZE = 50 * 1024 * 1024

# 按扩展名分类
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


class TelegramUploader:
    """将本地媒体文件发送到 Telegram Channel"""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        api_base: str = "https://api.telegram.org",
        upload_timeout: int = 600,
        caption_template: str = "{desc}",
        send_cover: bool = True,
        upload_extensions: Optional[list[str]] = None,
    ):
        """
        Args:
            bot_token: Telegram Bot Token (从 @BotFather 获取)
            chat_id: 目标 Channel/Group 的 chat_id (如 "@my_channel" 或 "-100xxxx")
            api_base: Telegram Bot API 基础 URL (可自定义用于代理)
            upload_timeout: 上传超时秒数
            caption_template: 消息标题模板，支持占位符: {desc}, {author}, {date}, {tags}
            send_cover: 是否同时发送封面图
            upload_extensions: 允许上传的扩展名列表
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")
        self.upload_timeout = upload_timeout
        self.caption_template = caption_template
        self.send_cover = send_cover
        self.upload_extensions: set[str] = set(upload_extensions) if upload_extensions else (
            _VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS
        )
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def _api_url(self) -> str:
        return f"{self.api_base}/bot{self.bot_token}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.upload_timeout),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_caption(self, entry: dict) -> str:
        """根据 manifest entry 生成消息标题"""
        desc = entry.get("desc", "")
        author = entry.get("author_name", "")
        date = entry.get("date", "")
        tags_list = entry.get("tags", [])
        tags = " ".join(f"#{t}" for t in tags_list) if tags_list else ""

        caption = self.caption_template.format(
            desc=desc,
            author=author,
            date=date,
            tags=tags,
        )

        # Telegram caption 限制 1024 字符
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        return caption

    async def _send_video(
        self, file_path: Path, caption: str = ""
    ) -> dict:
        """发送视频文件"""
        session = await self._get_session()
        url = f"{self._api_url}/sendVideo"

        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field(
            "video",
            open(file_path, "rb"),
            filename=file_path.name,
            content_type="video/mp4",
        )
        if caption:
            data.add_field("caption", caption)
        # 支持更长的 caption
        data.add_field("parse_mode", "HTML")

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    logger.info("已发送视频到 Telegram: %s", file_path.name)
                    return {"status": "sent", "type": "video", "file": file_path.name}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error(
                        "Telegram 发送视频失败 [%d]: %s → %s",
                        resp.status, file_path.name, error_desc,
                    )
                    return {"status": "error", "type": "video", "file": file_path.name, "detail": error_desc}
        except Exception as e:
            logger.exception("Telegram 发送视频异常: %s", file_path.name)
            return {"status": "error", "type": "video", "file": file_path.name, "detail": str(e)}

    async def _send_photo(
        self, file_path: Path, caption: str = ""
    ) -> dict:
        """发送图片文件"""
        session = await self._get_session()
        url = f"{self._api_url}/sendPhoto"

        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field(
            "photo",
            open(file_path, "rb"),
            filename=file_path.name,
            content_type="image/jpeg",
        )
        if caption:
            data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    logger.info("已发送图片到 Telegram: %s", file_path.name)
                    return {"status": "sent", "type": "photo", "file": file_path.name}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error(
                        "Telegram 发送图片失败 [%d]: %s → %s",
                        resp.status, file_path.name, error_desc,
                    )
                    return {"status": "error", "type": "photo", "file": file_path.name, "detail": error_desc}
        except Exception as e:
            logger.exception("Telegram 发送图片异常: %s", file_path.name)
            return {"status": "error", "type": "photo", "file": file_path.name, "detail": str(e)}

    async def _send_document(
        self, file_path: Path, caption: str = ""
    ) -> dict:
        """发送普通文件（超过 50MB 或未识别类型时使用）"""
        session = await self._get_session()
        url = f"{self._api_url}/sendDocument"

        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field(
            "document",
            open(file_path, "rb"),
            filename=file_path.name,
            content_type="application/octet-stream",
        )
        if caption:
            data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    logger.info("已发送文件到 Telegram: %s", file_path.name)
                    return {"status": "sent", "type": "document", "file": file_path.name}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error(
                        "Telegram 发送文件失败 [%d]: %s → %s",
                        resp.status, file_path.name, error_desc,
                    )
                    return {"status": "error", "type": "document", "file": file_path.name, "detail": error_desc}
        except Exception as e:
            logger.exception("Telegram 发送文件异常: %s", file_path.name)
            return {"status": "error", "type": "document", "file": file_path.name, "detail": str(e)}

    async def send_file(self, file_path: Path, caption: str = "") -> dict:
        """
        根据文件类型自动选择发送方式。
        - 视频: sendVideo (<=50MB) / sendDocument (>50MB)
        - 图片: sendPhoto
        - 其他: sendDocument
        """
        if not file_path.exists():
            logger.warning("文件不存在，跳过发送: %s", file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        suffix = file_path.suffix.lower()
        file_size = file_path.stat().st_size

        if suffix in _VIDEO_EXTENSIONS:
            if file_size > _MAX_FILE_SIZE:
                logger.info("视频超过 50MB，以文档方式发送: %s (%dMB)", file_path.name, file_size // (1024*1024))
                return await self._send_document(file_path, caption)
            return await self._send_video(file_path, caption)
        elif suffix in _IMAGE_EXTENSIONS:
            return await self._send_photo(file_path, caption)
        else:
            return await self._send_document(file_path, caption)

    async def upload_files(
        self,
        file_paths: list[Path],
        base_dir: Path,
        manifest_entries: Optional[list[dict]] = None,
    ) -> dict:
        """
        上传指定文件列表到 Telegram Channel。

        Args:
            file_paths: 待发送的文件绝对路径列表
            base_dir: 下载根目录
            manifest_entries: manifest 条目列表，用于生成消息标题
        """
        stats = {"sent": 0, "skipped": 0, "failed": 0}

        # 过滤可上传文件
        media_files = [
            f for f in file_paths
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        ]

        if not media_files:
            logger.info("无可发送的媒体文件")
            return stats

        logger.info("准备发送 %d 个媒体文件到 Telegram", len(media_files))

        # 构建 aweme_id → manifest entry 映射，用于查找 caption 信息
        entry_map: dict[str, dict] = {}
        if manifest_entries:
            for entry in manifest_entries:
                aweme_id = entry.get("aweme_id", "")
                if aweme_id:
                    entry_map[aweme_id] = entry

        for file_path in media_files:
            # 跳过封面图（如果关闭了 send_cover）
            if not self.send_cover and "_cover." in file_path.name:
                stats["skipped"] += 1
                continue

            # 尝试从文件名中提取 aweme_id，匹配 manifest entry
            caption = ""
            for aweme_id, entry in entry_map.items():
                if aweme_id in file_path.name or aweme_id in str(file_path.parent):
                    caption = self._build_caption(entry)
                    break

            result = await self.send_file(file_path, caption)
            status = result.get("status", "")

            if status == "sent":
                stats["sent"] += 1
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        return stats


# ── 全局单例 ──────────────────────────────────────────────
_uploader: Optional[TelegramUploader] = None


def get_telegram_uploader(telegram_config: Optional[dict] = None) -> Optional[TelegramUploader]:
    """
    获取全局 TelegramUploader 实例。

    优先从 telegram_config (config.yml 的 telegram 段) 读取配置,
    bot_token / chat_id 若为空则回退到环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。
    """
    global _uploader
    if _uploader is not None:
        return _uploader

    cfg = telegram_config or {}

    # enabled 开关
    if cfg.get("enabled") is False:
        return None

    # bot_token / chat_id: config.yml 优先，环境变量回退
    bot_token = (cfg.get("bot_token") or "").strip() or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (cfg.get("chat_id") or "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        return None

    _uploader = TelegramUploader(
        bot_token=bot_token,
        chat_id=chat_id,
        api_base=cfg.get("api_base", "https://api.telegram.org"),
        upload_timeout=cfg.get("upload_timeout", 600),
        caption_template=cfg.get("caption_template", "{desc}"),
        send_cover=cfg.get("send_cover", True),
        upload_extensions=cfg.get("upload_extensions"),
    )
    logger.info(
        "Telegram 上传已启用: chat_id=%s (api_base=%s, timeout=%ds)",
        chat_id, _uploader.api_base, _uploader.upload_timeout,
    )
    return _uploader
