#!/usr/bin/env python3
"""
Telegram 上传模块：将下载完成的媒体文件通过 Telegram Bot API 发送到指定 Channel。

直接使用 aiohttp 调用 Telegram Bot API（无额外依赖）:
  POST /bot<token>/sendVideo
  POST /bot<token>/sendPhoto
  POST /bot<token>/sendDocument
  POST /bot<token>/sendMediaGroup

同一个作品（aweme_id）的视频+封面会合并为一条 MediaGroup 消息发送。
所有消息默认静音发送（disable_notification=true）。
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import aiohttp

from utils.logger import setup_logger

logger = setup_logger("TelegramUploader")

# Telegram Bot API 文件上传限制
# 官方 API: 50MB, 自建 local 模式: 2GB
_MAX_FILE_SIZE_OFFICIAL = 50 * 1024 * 1024
_MAX_FILE_SIZE_LOCAL = 2000 * 1024 * 1024

# 按扩展名分类
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _get_video_dimensions(file_path: Path) -> tuple[int, int]:
    """用 ffprobe 获取视频宽高，失败返回 (0, 0)"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                str(file_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "x" in result.stdout.strip():
            w, h = result.stdout.strip().split("x")[:2]
            return int(w), int(h)
    except Exception:
        pass
    return 0, 0


def _find_thumbnail(video_path: Path) -> Optional[Path]:
    """在同目录下查找视频对应的封面图作为 thumbnail"""
    stem = video_path.stem
    parent = video_path.parent
    # 常见封面命名: xxx_cover.jpg, xxx_cover.png
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        # 精确匹配: 同名_cover
        cover = parent / f"{stem}_cover{ext}"
        if cover.exists():
            return cover
    # 模糊匹配: 目录下任何 _cover 文件
    for f in parent.iterdir():
        if "_cover." in f.name and f.suffix.lower() in _IMAGE_EXTENSIONS:
            return f
    return None


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

        # 判断是否使用自建 Bot API Server (local 模式)
        self.is_local = api_base.rstrip("/") != "https://api.telegram.org"
        self.max_file_size = _MAX_FILE_SIZE_LOCAL if self.is_local else _MAX_FILE_SIZE_OFFICIAL

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
        """根据 manifest entry 生成消息标题。
        模板中支持 Markdown 风格的 **加粗** 和 _斜体_，会自动转换为 HTML 标签。
        """
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

        # 把 Markdown 风格标记转换为 HTML
        # **text** → <b>text</b>
        caption = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', caption)
        # _text_ → <i>text</i>  (注意不匹配已经是 HTML 标签里的下划线)
        caption = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', caption)

        # 在末尾追加原抖音视频链接
        aweme_id = entry.get("aweme_id", "")
        if aweme_id:
            link = f"https://www.douyin.com/video/{aweme_id}"
            caption += f' <a href="{link}">🔗</a>'

        # Telegram caption 限制 1024 字符
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        return caption

    async def _send_media_group(
        self, files: list[Path], caption: str = ""
    ) -> dict:
        """
        将多个媒体文件合并为一条 MediaGroup 消息发送。
        caption 只附加在第一个媒体上。
        封面图排在前面，视频排在后面。
        Telegram 限制: 2-10 个媒体项。
        """
        session = await self._get_session()
        url = f"{self._api_url}/sendMediaGroup"

        # 封面（图片）排前面，视频排后面
        sorted_files = sorted(files, key=lambda f: (f.suffix.lower() in _VIDEO_EXTENSIONS, f.name))

        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field("disable_notification", "true")

        media_items = []
        for i, file_path in enumerate(sorted_files):
            attach_key = f"file{i}"
            suffix = file_path.suffix.lower()

            if suffix in _VIDEO_EXTENSIONS:
                media_type = "video"
            else:
                media_type = "photo"

            item = {
                "type": media_type,
                "media": f"attach://{attach_key}",
            }

            # 视频: 附带宽高，避免正方形预览
            if media_type == "video":
                w, h = _get_video_dimensions(file_path)
                if w > 0 and h > 0:
                    item["width"] = w
                    item["height"] = h
                # thumbnail
                thumb = _find_thumbnail(file_path)
                if thumb:
                    thumb_key = f"thumb{i}"
                    item["thumbnail"] = f"attach://{thumb_key}"
                    data.add_field(
                        thumb_key,
                        open(thumb, "rb"),
                        filename=thumb.name,
                        content_type="image/jpeg",
                    )

            # caption 只放在第一个媒体上
            if i == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"

            media_items.append(item)

            content_type = "video/mp4" if media_type == "video" else "image/jpeg"
            data.add_field(
                attach_key,
                open(file_path, "rb"),
                filename=file_path.name,
                content_type=content_type,
            )

        data.add_field("media", json.dumps(media_items))

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    names = [f.name for f in sorted_files]
                    logger.info("已发送 MediaGroup 到 Telegram (%d 个文件): %s", len(sorted_files), names)
                    return {"status": "sent", "type": "media_group", "count": len(sorted_files)}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error("Telegram MediaGroup 发送失败: %s", error_desc)
                    return {"status": "error", "type": "media_group", "detail": error_desc}
        except Exception as e:
            logger.exception("Telegram MediaGroup 发送异常")
            return {"status": "error", "type": "media_group", "detail": str(e)}

    async def _send_single(self, file_path: Path, caption: str = "") -> dict:
        """
        发送单个文件，静音模式。
        根据文件类型自动选择 sendVideo / sendPhoto / sendDocument。
        超过大小限制的文件跳过（官方 50MB / local 模式 2GB）。
        """
        if not file_path.exists():
            logger.warning("文件不存在，跳过发送: %s", file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        suffix = file_path.suffix.lower()
        file_size = file_path.stat().st_size

        if file_size > self.max_file_size:
            limit_mb = self.max_file_size // (1024 * 1024)
            logger.warning(
                "文件超过 %dMB 限制，跳过发送: %s (%dMB)",
                limit_mb, file_path.name, file_size // (1024 * 1024),
            )
            return {"status": "skipped", "reason": "file_too_large"}

        session = await self._get_session()

        # 选择 API 方法和字段名
        if suffix in _VIDEO_EXTENSIONS:
            method, field_name, content_type = "sendVideo", "video", "video/mp4"
        elif suffix in _IMAGE_EXTENSIONS:
            method, field_name, content_type = "sendPhoto", "photo", "image/jpeg"
        else:
            method, field_name, content_type = "sendDocument", "document", "application/octet-stream"

        url = f"{self._api_url}/{method}"
        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field("disable_notification", "true")
        data.add_field(
            field_name,
            open(file_path, "rb"),
            filename=file_path.name,
            content_type=content_type,
        )

        # 视频: 附带宽高和缩略图，避免 Telegram 显示为正方形
        if method == "sendVideo":
            w, h = _get_video_dimensions(file_path)
            if w > 0 and h > 0:
                data.add_field("width", str(w))
                data.add_field("height", str(h))
            thumb = _find_thumbnail(file_path)
            if thumb:
                data.add_field(
                    "thumbnail",
                    open(thumb, "rb"),
                    filename=thumb.name,
                    content_type="image/jpeg",
                )

        if caption:
            data.add_field("caption", caption)
            data.add_field("parse_mode", "HTML")

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    logger.info("已发送到 Telegram: %s (%s)", file_path.name, method)
                    return {"status": "sent", "type": field_name, "file": file_path.name}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error(
                        "Telegram %s 失败 [%d]: %s → %s",
                        method, resp.status, file_path.name, error_desc,
                    )
                    return {"status": "error", "type": field_name, "file": file_path.name, "detail": error_desc}
        except Exception as e:
            logger.exception("Telegram %s 异常: %s", method, file_path.name)
            return {"status": "error", "type": field_name, "file": file_path.name, "detail": str(e)}

    async def upload_files(
        self,
        file_paths: list[Path],
        base_dir: Path,
        manifest_entries: Optional[list[dict]] = None,
    ) -> dict:
        """
        上传指定文件列表到 Telegram Channel。
        同一个作品（aweme_id）的视频+封面合并为一条 MediaGroup 消息发送。

        Args:
            file_paths: 待发送的文件绝对路径列表
            base_dir: 下载根目录
            manifest_entries: manifest 条目列表，用于分组和生成标题
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

        # 构建 aweme_id → manifest entry 映射
        entry_map: dict[str, dict] = {}
        if manifest_entries:
            for entry in manifest_entries:
                aweme_id = entry.get("aweme_id", "")
                if aweme_id:
                    entry_map[aweme_id] = entry

        # ── 按 aweme_id 分组文件 ──
        # 每个 aweme 的文件放在同一个子目录下，目录名包含 aweme_id
        aweme_groups: dict[str, list[Path]] = {}  # aweme_id → [file_paths]
        ungrouped: list[Path] = []

        for file_path in media_files:
            # 跳过封面图（如果关闭了 send_cover）
            if not self.send_cover and "_cover." in file_path.name:
                stats["skipped"] += 1
                continue

            matched_aweme = None
            for aweme_id in entry_map:
                if aweme_id in file_path.name or aweme_id in str(file_path.parent):
                    matched_aweme = aweme_id
                    break

            if matched_aweme:
                aweme_groups.setdefault(matched_aweme, []).append(file_path)
            else:
                ungrouped.append(file_path)

        # ── 按组发送 ──
        for aweme_id, group_files in aweme_groups.items():
            caption = self._build_caption(entry_map[aweme_id]) if aweme_id in entry_map else ""

            # 过滤掉超过大小限制的文件（官方 50MB / local 模式 2GB）
            group_ok: list[Path] = []
            for f in group_files:
                if f.stat().st_size > self.max_file_size:
                    limit_mb = self.max_file_size // (1024 * 1024)
                    logger.warning("文件超过 %dMB 限制，跳过: %s (%dMB)", limit_mb, f.name, f.stat().st_size // (1024 * 1024))
                    stats["skipped"] += 1
                else:
                    group_ok.append(f)

            if len(group_ok) >= 2:
                # 2 个以上文件：用 MediaGroup 合并发送
                result = await self._send_media_group(group_ok, caption)
                if result.get("status") == "sent":
                    stats["sent"] += result.get("count", len(group_ok))
                else:
                    stats["failed"] += len(group_ok)
            elif len(group_ok) == 1:
                # 只有 1 个文件：单独发送
                result = await self._send_single(group_ok[0], caption)
                if result.get("status") == "sent":
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1

        # ── 未匹配到 aweme 的文件单独发送 ──
        for file_path in ungrouped:
            result = await self._send_single(file_path)
            if result.get("status") == "sent":
                stats["sent"] += 1
            elif result.get("status") == "skipped":
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
        "Telegram 上传已启用: chat_id=%s (api_base=%s, timeout=%ds, mode=%s, max_file=%dMB)",
        chat_id, _uploader.api_base, _uploader.upload_timeout,
        "local" if _uploader.is_local else "official",
        _uploader.max_file_size // (1024 * 1024),
    )
    return _uploader
