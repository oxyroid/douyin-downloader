#!/usr/bin/env python3
"""
Immich 上传模块：将下载完成的文件通过 Immich API 上传，然后删除本地文件。

使用 Immich 标准 REST API:
  POST /api/assets  (multipart/form-data)
  Header: x-api-key
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from utils.logger import setup_logger

logger = setup_logger("ImmichUploader")


class ImmichUploader:
    """将本地文件上传到 Immich 并清理本地副本"""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        device_id: str = "douyin-downloader",
        album_prefix: str = "douyin-",
        upload_timeout: int = 600,
        upload_extensions: Optional[list[str]] = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.device_id = device_id
        self.album_prefix = album_prefix
        self.upload_timeout = upload_timeout
        self.upload_extensions: set[str] = set(upload_extensions) if upload_extensions else {
            ".mp4", ".mov", ".avi", ".mkv", ".webm",
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._album_cache: dict[str, str] = {}  # album_name → album_id 缓存

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"x-api-key": self.api_key},
                timeout=aiohttp.ClientTimeout(total=self.upload_timeout),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_album(self, album_name: str) -> Optional[str]:
        """确保目标相册存在，返回相册 ID。结果会被缓存。"""
        if album_name in self._album_cache:
            return self._album_cache[album_name]

        session = await self._get_session()

        # 1. 查找已有相册
        try:
            async with session.get(f"{self.api_url}/api/albums") as resp:
                if resp.status == 200:
                    albums = await resp.json()
                    for album in albums:
                        if album.get("albumName") == album_name:
                            self._album_cache[album_name] = album["id"]
                            logger.info("找到已有 Immich 相册: %s (id=%s)", album_name, album["id"])
                            return album["id"]
        except Exception as e:
            logger.warning("查询 Immich 相册列表失败: %s", e)

        # 2. 没找到则创建
        try:
            payload = {"albumName": album_name}
            async with session.post(f"{self.api_url}/api/albums", json=payload) as resp:
                if resp.status == 201:
                    body = await resp.json()
                    album_id = body["id"]
                    self._album_cache[album_name] = album_id
                    logger.info("创建 Immich 相册: %s (id=%s)", album_name, album_id)
                    return album_id
                else:
                    body = await resp.json()
                    logger.error("创建 Immich 相册失败 [%d]: %s", resp.status, body)
        except Exception as e:
            logger.exception("创建 Immich 相册异常: %s", e)

        return None

    async def _add_assets_to_album(self, album_name: str, asset_ids: list[str]):
        """将资产批量添加到指定相册"""
        if not asset_ids:
            return

        album_id = await self._ensure_album(album_name)
        if not album_id:
            logger.warning("无法获取相册 ID，跳过添加到相册: %s", album_name)
            return

        session = await self._get_session()
        url = f"{self.api_url}/api/albums/{album_id}/assets"
        payload = {"ids": asset_ids}

        try:
            async with session.put(url, json=payload) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    added = sum(1 for r in results if r.get("success"))
                    logger.info("已将 %d/%d 个资产添加到相册 '%s'", added, len(asset_ids), album_name)
                else:
                    body = await resp.json()
                    logger.error("添加资产到相册失败 [%d]: %s", resp.status, body)
        except Exception as e:
            logger.exception("添加资产到相册异常: %s", e)

    async def _restore_from_trash(self, asset_id: str) -> bool:
        """尝试将资产从 Immich 垃圾箱恢复。用户在 Immich 中删除的文件会进垃圾箱，
        重新上传时 Immich 返回 duplicate，此时需要调用 restore 接口恢复。"""
        session = await self._get_session()
        url = f"{self.api_url}/api/trash/restore/assets"
        payload = {"ids": [asset_id]}
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 204:
                    logger.info("已从 Immich 垃圾箱恢复: asset_id=%s", asset_id)
                    return True
                else:
                    # 可能资产不在垃圾箱中（正常 duplicate），忽略即可
                    logger.debug(
                        "垃圾箱恢复返回 [%d] (asset_id=%s，可能不在垃圾箱中)",
                        resp.status, asset_id,
                    )
                    return False
        except Exception as e:
            logger.warning("垃圾箱恢复异常 (asset_id=%s): %s", asset_id, e)
            return False

    async def upload_file(self, file_path: Path, *, force: bool = False) -> dict:
        """
        上传单个文件到 Immich。

        Immich POST /api/assets 要求：
        - multipart/form-data
        - assetData: 文件二进制
        - deviceAssetId: 唯一标识（使用文件名）
        - deviceId: 设备标识
        - fileCreatedAt: ISO 格式时间
        - fileModifiedAt: ISO 格式时间

        当 force=True 时，如果 Immich 返回 duplicate，会自动尝试从垃圾箱恢复该资产，
        确保用户在 Immich 中手动删除过的文件能被重新恢复。

        返回值示例:
          {"id": "uuid", "status": "created"} 或 {"id": "uuid", "status": "duplicate"}
        """
        if not file_path.exists():
            logger.warning("文件不存在，跳过上传: %s", file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        stat = file_path.stat()
        created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

        session = await self._get_session()
        url = f"{self.api_url}/api/assets"

        data = aiohttp.FormData()
        data.add_field(
            "assetData",
            open(file_path, "rb"),
            filename=file_path.name,
            content_type="application/octet-stream",
        )
        data.add_field("deviceAssetId", f"{self.device_id}-{file_path.name}")
        data.add_field("deviceId", self.device_id)
        data.add_field("fileCreatedAt", created_at)
        data.add_field("fileModifiedAt", modified_at)

        try:
            async with session.post(url, data=data) as resp:
                body = await resp.json()

                if resp.status in (200, 201):
                    status = body.get("status", "unknown")
                    asset_id = body.get("id", "")
                    if resp.status == 201:
                        logger.info(
                            "已上传到 Immich: %s (asset_id=%s)",
                            file_path.name,
                            asset_id,
                        )
                    else:
                        # 200 = duplicate
                        if force and asset_id:
                            # force 模式：尝试从垃圾箱恢复（用户可能在 Immich 中删除过）
                            restored = await self._restore_from_trash(asset_id)
                            if restored:
                                logger.info(
                                    "Immich 重复但已从垃圾箱恢复: %s (asset_id=%s)",
                                    file_path.name,
                                    asset_id,
                                )
                                return {"status": "restored", "id": asset_id, "http": resp.status}
                        logger.info(
                            "Immich 重复跳过: %s (asset_id=%s)",
                            file_path.name,
                            asset_id,
                        )
                    return {"status": status, "id": asset_id, "http": resp.status}
                else:
                    logger.error(
                        "Immich 上传失败 [%d]: %s → %s",
                        resp.status,
                        file_path.name,
                        body,
                    )
                    return {"status": "error", "http": resp.status, "detail": body}
        except Exception as e:
            logger.exception("Immich 上传异常: %s", file_path.name)
            return {"status": "error", "detail": str(e)}

    async def upload_files(
        self,
        file_paths: list[Path],
        base_dir: Path,
        *,
        force: bool = True,
    ) -> dict:
        """
        上传指定文件列表到 Immich。

        与 upload_directory 不同，此方法只上传明确指定的文件，
        适用于"本次下载产生的文件"场景。

        Args:
            file_paths: 待上传的文件绝对路径列表
            base_dir: 下载根目录（用于解析作者名，即一级子目录名）
            force: 若为 True，对 Immich 返回 duplicate 的资产尝试从垃圾箱恢复
        """
        stats = {"uploaded": 0, "duplicates": 0, "restored": 0, "skipped": 0, "failed": 0}

        # 过滤：只保留存在且扩展名匹配的文件
        media_files = [
            f for f in file_paths
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        ]

        if not media_files:
            logger.info("无可上传的媒体文件")
            return stats

        logger.info("准备上传 %d 个媒体文件到 Immich", len(media_files))

        author_asset_ids: dict[str, list[str]] = {}

        for file_path in media_files:
            # 解析作者名：base_dir/<作者名>/...
            try:
                rel = file_path.relative_to(base_dir)
                author_name = rel.parts[0] if len(rel.parts) > 1 else None
            except ValueError:
                author_name = None

            result = await self.upload_file(file_path, force=force)
            status = result.get("status", "")
            asset_id = result.get("id", "")

            if result.get("http") == 201:
                stats["uploaded"] += 1
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif status == "restored":
                stats["restored"] += 1
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif result.get("http") == 200:
                stats["duplicates"] += 1
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        # 按作者分别添加到对应相册
        for author_name, asset_ids in author_asset_ids.items():
            album_name = f"{self.album_prefix}{author_name}"
            await self._add_assets_to_album(album_name, asset_ids)

        return stats

    async def upload_directory(self, directory: Path) -> dict:
        """
        扫描目录（递归），将所有符合条件的媒体文件上传到 Immich。
        按一级子目录（作者名）分组，每个作者创建独立相册 "douyin-作者名"。

        返回统计信息。
        """
        if not directory.exists():
            logger.warning("目录不存在: %s", directory)
            return {"uploaded": 0, "skipped": 0, "failed": 0, "deleted": 0}

        stats = {"uploaded": 0, "duplicates": 0, "skipped": 0, "failed": 0}

        # 收集所有待上传文件
        media_files = sorted(
            f
            for f in directory.rglob("*")
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        )

        if not media_files:
            logger.info("目录中无可上传的媒体文件: %s", directory)
            return stats

        logger.info("发现 %d 个媒体文件待上传到 Immich: %s", len(media_files), directory)

        # 按作者（一级子目录名）分组收集 asset ID
        author_asset_ids: dict[str, list[str]] = {}

        for file_path in media_files:
            # 解析作者名：Downloaded/<作者名>/...
            try:
                rel = file_path.relative_to(directory)
                author_name = rel.parts[0] if len(rel.parts) > 1 else None
            except ValueError:
                author_name = None

            result = await self.upload_file(file_path)
            status = result.get("status", "")
            asset_id = result.get("id", "")

            if result.get("http") == 201:
                stats["uploaded"] += 1
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif result.get("http") == 200:
                stats["duplicates"] += 1
                # 重复的资产也加入相册（可能之前未加入）
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        # 按作者分别添加到对应相册
        for author_name, asset_ids in author_asset_ids.items():
            album_name = f"{self.album_prefix}{author_name}"
            await self._add_assets_to_album(album_name, asset_ids)

        # 上传完成后不删除本地文件，保留用于下载器去重
        return stats



# ── 全局单例 ──────────────────────────────────────────────
_uploader: Optional[ImmichUploader] = None


def get_immich_uploader(immich_config: Optional[dict] = None) -> Optional[ImmichUploader]:
    """
    获取全局 ImmichUploader 实例。

    优先从 immich_config（config.yml 的 immich 段）读取配置，
    api_url / api_key 若为空则回退到环境变量 IMMICH_API_URL / IMMICH_API_KEY。
    """
    global _uploader
    if _uploader is not None:
        return _uploader

    cfg = immich_config or {}

    # enabled 开关：如果显式设为 false 则禁用
    if cfg.get("enabled") is False:
        return None

    # api_url / api_key：config.yml 优先，环境变量回退
    api_url = (cfg.get("api_url") or "").strip() or os.environ.get("IMMICH_API_URL", "").strip()
    api_key = (cfg.get("api_key") or "").strip() or os.environ.get("IMMICH_API_KEY", "").strip()

    if not api_url or not api_key:
        return None

    _uploader = ImmichUploader(
        api_url=api_url,
        api_key=api_key,
        device_id=cfg.get("device_id", "douyin-downloader"),
        album_prefix=cfg.get("album_prefix", "douyin-"),
        upload_timeout=cfg.get("upload_timeout", 600),
        upload_extensions=cfg.get("upload_extensions"),
    )
    logger.info("Immich 上传已启用: %s (album_prefix=%s, device=%s, timeout=%ds)",
                api_url, _uploader.album_prefix, _uploader.device_id, _uploader.upload_timeout)
    return _uploader
