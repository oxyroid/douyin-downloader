#!/usr/bin/env python3
"""
Telegram upload module.

Sends downloaded media to a Telegram Channel via Bot API (aiohttp, no extra deps):
  POST /bot<token>/sendVideo
  POST /bot<token>/sendPhoto
  POST /bot<token>/sendDocument
  POST /bot<token>/sendMediaGroup

Files from the same aweme (video + cover) are grouped into a single MediaGroup.
All messages are sent silently (disable_notification=true).
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import aiohttp

from app.utils.logger import setup_logger

logger = setup_logger("TelegramUploader")

# File-size limits for Telegram Bot API uploads
# Official API: 50 MB, self-hosted local mode: 2 GB
_MAX_FILE_SIZE_OFFICIAL = 50 * 1024 * 1024
_MAX_FILE_SIZE_LOCAL = 2000 * 1024 * 1024

# Extension sets
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _get_video_dimensions(file_path: Path) -> tuple[int, int]:
    """Return (width, height) via ffprobe, or (0, 0) on failure."""
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
    """Look for a cover image in the same directory to use as thumbnail."""
    stem = video_path.stem
    parent = video_path.parent
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cover = parent / f"{stem}_cover{ext}"
        if cover.exists():
            return cover
    # Fallback: any *_cover.* file in the directory
    for f in parent.iterdir():
        if "_cover." in f.name and f.suffix.lower() in _IMAGE_EXTENSIONS:
            return f
    return None


class TelegramUploader:
    """Sends local media files to a Telegram Channel."""

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
            bot_token: Telegram Bot Token (from @BotFather).
            chat_id: Target Channel/Group chat_id (e.g. "@my_channel" or "-100xxxx").
            api_base: Bot API base URL (override for proxies or self-hosted server).
            upload_timeout: Per-file upload timeout in seconds.
            caption_template: Message caption template. Placeholders: {desc}, {author}, {date}, {tags}.
            send_cover: Whether to include cover images alongside videos.
            upload_extensions: Allowed file extensions for upload.
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        # Default to official API if api_base is empty
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")
        self.upload_timeout = upload_timeout
        self.caption_template = caption_template
        self.send_cover = send_cover
        self.upload_extensions: set[str] = set(upload_extensions) if upload_extensions else (
            _VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS
        )
        self._session: Optional[aiohttp.ClientSession] = None

        # Self-hosted Bot API Server = local mode (non-official URL)
        self.is_local = self.api_base != "https://api.telegram.org"
        self.max_file_size = _MAX_FILE_SIZE_LOCAL if self.is_local else _MAX_FILE_SIZE_OFFICIAL

    def _log_size_limit_hint(self, file_name: str, file_mb: int):
        """Log a warning with configuration hints when file exceeds official API limit."""
        limit_mb = self.max_file_size // (1024 * 1024)
        if not self.is_local:
            logger.warning(
                "File exceeds %d MB limit (official Telegram API), skipping: %s (%d MB)",
                limit_mb, file_name, file_mb,
            )
            logger.warning(
                "  TIP: To upload files up to 2GB, configure a self-hosted Bot API Server in config.yml:"
            )
            logger.warning("    telegram:")
            logger.warning("      api_base: 'http://your-bot-api-server:8081'")
            logger.warning("      api_id: 'your_api_id'      # Get from https://my.telegram.org")
            logger.warning("      api_hash: 'your_api_hash'")
        else:
            logger.warning(
                "File exceeds %d MB limit, skipping: %s (%d MB)",
                limit_mb, file_name, file_mb,
            )

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

    def _build_caption(self, entry: dict) -> tuple[str, str]:
        """Build a caption and original link from a manifest entry.

        Markdown-style **bold** and _italic_ in the template are converted to HTML tags.
        Returns (caption, original_url) where original_url may be empty.
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

        # Markdown → HTML
        # **text** → <b>text</b>
        caption = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', caption)
        # _text_ → <i>text</i>  (avoid matching underscores inside HTML tags)
        caption = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', caption)

        # Original Douyin video link (shown as inline button, not in caption)
        original_url = ""
        aweme_id = entry.get("aweme_id", "")
        if aweme_id:
            original_url = f"https://www.douyin.com/video/{aweme_id}"

        # Telegram caption limit: 1024 chars
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        return caption, original_url

    @staticmethod
    def _build_reply_markup(url: str) -> str:
        """Build an InlineKeyboardMarkup JSON string with a single URL button."""
        return json.dumps({
            "inline_keyboard": [[{"text": "🔗", "url": url}]]
        })

    async def _edit_reply_markup(self, message_id: int, url: str):
        """Attach an inline keyboard button to an already-sent message."""
        try:
            session = await self._get_session()
            edit_url = f"{self._api_url}/editMessageReplyMarkup"
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "reply_markup": self._build_reply_markup(url),
            }
            async with session.post(edit_url, json=payload) as resp:
                body = await resp.json()
                if not body.get("ok"):
                    logger.warning("editMessageReplyMarkup failed: %s", body.get("description"))
        except Exception as e:
            logger.warning("Failed to attach inline button: %s", e)

    async def _send_media_group(
        self, files: list[Path], caption: str = "", reply_url: str = ""
    ) -> dict:
        """Send multiple files as a single MediaGroup message.

        Caption is attached to the first item only.
        Cover images are placed before videos.
        Telegram limit: 2–10 media items per group.
        If reply_url is provided, an inline keyboard button is added to the
        first message via editMessageReplyMarkup after sending.
        """
        session = await self._get_session()
        url = f"{self._api_url}/sendMediaGroup"

        # Covers (images) first, videos after
        sorted_files = sorted(files, key=lambda f: (f.suffix.lower() in _VIDEO_EXTENSIONS, f.name))

        # Collect file handles for proper cleanup
        file_handles: list = []
        
        try:
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

                # Include width/height for videos to prevent square preview
                if media_type == "video":
                    w, h = _get_video_dimensions(file_path)
                    if w > 0 and h > 0:
                        item["width"] = w
                        item["height"] = h
                    thumb = _find_thumbnail(file_path)
                    if thumb:
                        thumb_key = f"thumb{i}"
                        item["thumbnail"] = f"attach://{thumb_key}"
                        thumb_handle = open(thumb, "rb")
                        file_handles.append(thumb_handle)
                        data.add_field(
                            thumb_key,
                            thumb_handle,
                            filename=thumb.name,
                            content_type="image/jpeg",
                        )

                # Caption on the first item only
                if i == 0 and caption:
                    item["caption"] = caption
                    item["parse_mode"] = "HTML"

                media_items.append(item)

                content_type = "video/mp4" if media_type == "video" else "image/jpeg"
                file_handle = open(file_path, "rb")
                file_handles.append(file_handle)
                data.add_field(
                    attach_key,
                    file_handle,
                    filename=file_path.name,
                    content_type=content_type,
                )

            data.add_field("media", json.dumps(media_items))

            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    names = [f.name for f in sorted_files]
                    logger.info("Sent MediaGroup (%d files): %s", len(sorted_files), names)

                    # Attach inline button to the first message in the group
                    if reply_url:
                        messages = body.get("result", [])
                        if messages:
                            first_msg_id = messages[0].get("message_id")
                            if first_msg_id:
                                await self._edit_reply_markup(first_msg_id, reply_url)

                    return {"status": "sent", "type": "media_group", "count": len(sorted_files)}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error("MediaGroup failed: %s", error_desc)
                    return {"status": "error", "type": "media_group", "detail": error_desc}
        except Exception as e:
            logger.exception("MediaGroup error")
            return {"status": "error", "type": "media_group", "detail": str(e)}
        finally:
            # Ensure all file handles are closed
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

    async def _send_single(self, file_path: Path, caption: str = "", reply_url: str = "") -> dict:
        """Send a single file (silent).

        Picks sendVideo / sendPhoto / sendDocument based on extension.
        Files exceeding the size limit are skipped (50 MB official / 2 GB local).
        If reply_url is provided, an inline keyboard button is attached.
        """
        if not file_path.exists():
            logger.warning("File not found, skipping: %s", file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        suffix = file_path.suffix.lower()
        file_size = file_path.stat().st_size

        if file_size > self.max_file_size:
            file_mb = file_size // (1024 * 1024)
            self._log_size_limit_hint(file_path.name, file_mb)
            return {"status": "skipped", "reason": "file_too_large", "file_size_mb": file_mb, "limit_mb": self.max_file_size // (1024 * 1024)}

        session = await self._get_session()

        if suffix in _VIDEO_EXTENSIONS:
            method, field_name, content_type = "sendVideo", "video", "video/mp4"
        elif suffix in _IMAGE_EXTENSIONS:
            method, field_name, content_type = "sendPhoto", "photo", "image/jpeg"
        else:
            method, field_name, content_type = "sendDocument", "document", "application/octet-stream"

        url = f"{self._api_url}/{method}"
        file_handles: list = []
        
        try:
            data = aiohttp.FormData()
            data.add_field("chat_id", self.chat_id)
            data.add_field("disable_notification", "true")
            
            file_handle = open(file_path, "rb")
            file_handles.append(file_handle)
            data.add_field(
                field_name,
                file_handle,
                filename=file_path.name,
                content_type=content_type,
            )

            # Video: attach width/height and thumbnail
            if method == "sendVideo":
                w, h = _get_video_dimensions(file_path)
                if w > 0 and h > 0:
                    data.add_field("width", str(w))
                    data.add_field("height", str(h))
                thumb = _find_thumbnail(file_path)
                if thumb:
                    thumb_handle = open(thumb, "rb")
                    file_handles.append(thumb_handle)
                    data.add_field(
                        "thumbnail",
                        thumb_handle,
                        filename=thumb.name,
                        content_type="image/jpeg",
                    )

            if caption:
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")

            if reply_url:
                data.add_field("reply_markup", self._build_reply_markup(reply_url))

            async with session.post(url, data=data) as resp:
                body = await resp.json()
                if body.get("ok"):
                    logger.info("Sent to Telegram: %s (%s)", file_path.name, method)
                    return {"status": "sent", "type": field_name, "file": file_path.name}
                else:
                    error_desc = body.get("description", "unknown error")
                    logger.error(
                        "%s failed [%d]: %s -> %s",
                        method, resp.status, file_path.name, error_desc,
                    )
                    return {"status": "error", "type": field_name, "file": file_path.name, "detail": error_desc}
        except Exception as e:
            logger.exception("%s error: %s", method, file_path.name)
            return {"status": "error", "type": field_name, "file": file_path.name, "detail": str(e)}
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

    async def upload_files(
        self,
        file_paths: list[Path],
        base_dir: Path,
        manifest_entries: Optional[list[dict]] = None,
    ) -> dict:
        """Upload files to a Telegram Channel.

        Files sharing the same aweme_id (video + cover) are merged into one MediaGroup.

        Args:
            file_paths: Absolute paths of files to send.
            base_dir: Download root directory.
            manifest_entries: Manifest entries for grouping and caption generation.
        """
        stats = {"sent": 0, "skipped": 0, "failed": 0}

        media_files = [
            f for f in file_paths
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        ]

        if not media_files:
            logger.info("No media files to send")
            return stats

        logger.info("Sending %d media file(s) to Telegram", len(media_files))

        # Build aweme_id -> manifest entry map
        entry_map: dict[str, dict] = {}
        if manifest_entries:
            for entry in manifest_entries:
                aweme_id = entry.get("aweme_id", "")
                if aweme_id:
                    entry_map[aweme_id] = entry

        # Group files by aweme_id
        aweme_groups: dict[str, list[Path]] = {}
        ungrouped: list[Path] = []

        for file_path in media_files:
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

        # Send grouped files
        for aweme_id, group_files in aweme_groups.items():
            caption = ""
            reply_url = ""
            if aweme_id in entry_map:
                caption, reply_url = self._build_caption(entry_map[aweme_id])

            group_ok: list[Path] = []
            for f in group_files:
                if f.stat().st_size > self.max_file_size:
                    file_mb = f.stat().st_size // (1024 * 1024)
                    self._log_size_limit_hint(f.name, file_mb)
                    stats["skipped"] += 1
                else:
                    group_ok.append(f)

            if len(group_ok) >= 2:
                result = await self._send_media_group(group_ok, caption, reply_url)
                if result.get("status") == "sent":
                    stats["sent"] += result.get("count", len(group_ok))
                else:
                    stats["failed"] += len(group_ok)
            elif len(group_ok) == 1:
                result = await self._send_single(group_ok[0], caption, reply_url)
                if result.get("status") == "sent":
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1

        # Send ungrouped files individually
        for file_path in ungrouped:
            result = await self._send_single(file_path)
            if result.get("status") == "sent":
                stats["sent"] += 1
            elif result.get("status") == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        return stats


# -- Singleton ---------------------------------------------------------
_uploader: Optional[TelegramUploader] = None


def get_telegram_uploader(telegram_config: Optional[dict] = None) -> Optional[TelegramUploader]:
    """Return the global TelegramUploader instance (created on first call).

    Reads from telegram_config (the ``telegram`` section of config.yml).
    bot_token / chat_id fall back to env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    """
    global _uploader
    if _uploader is not None:
        return _uploader

    cfg = telegram_config or {}

    if cfg.get("enabled") is False:
        return None

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
        "Telegram enabled: chat_id=%s (api_base=%s, timeout=%ds, mode=%s, max_file=%dMB)",
        chat_id, _uploader.api_base, _uploader.upload_timeout,
        "local" if _uploader.is_local else "official",
        _uploader.max_file_size // (1024 * 1024),
    )
    return _uploader
