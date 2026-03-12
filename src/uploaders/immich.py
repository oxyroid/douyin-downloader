#!/usr/bin/env python3
"""
Immich upload module.

Uploads downloaded files to an Immich instance via its REST API, then
optionally groups them into albums by author name.

Uses the standard Immich REST API:
  POST /api/assets  (multipart/form-data)
  Header: x-api-key
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from app.utils.logger import setup_logger

logger = setup_logger("ImmichUploader")


class ImmichUploader:
    """Uploads local files to Immich and organizes them into albums."""

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
        self._album_cache: dict[str, str] = {}  # album_name -> album_id

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
        """Return the album ID for *album_name*, creating the album if needed."""
        if album_name in self._album_cache:
            return self._album_cache[album_name]

        session = await self._get_session()

        # Look for an existing album
        try:
            async with session.get(f"{self.api_url}/api/albums") as resp:
                if resp.status == 200:
                    albums = await resp.json()
                    for album in albums:
                        if album.get("albumName") == album_name:
                            self._album_cache[album_name] = album["id"]
                            logger.info("Found Immich album: %s (id=%s)", album_name, album["id"])
                            return album["id"]
        except Exception as e:
            logger.warning("Failed to list Immich albums: %s", e)

        # Create a new one
        try:
            payload = {"albumName": album_name}
            async with session.post(f"{self.api_url}/api/albums", json=payload) as resp:
                if resp.status == 201:
                    body = await resp.json()
                    album_id = body["id"]
                    self._album_cache[album_name] = album_id
                    logger.info("Created Immich album: %s (id=%s)", album_name, album_id)
                    return album_id
                else:
                    body = await resp.json()
                    logger.error("Album creation failed [%d]: %s", resp.status, body)
        except Exception as e:
            logger.exception("Album creation error: %s", e)

        return None

    async def _add_assets_to_album(self, album_name: str, asset_ids: list[str]):
        """Add assets to the given album in bulk."""
        if not asset_ids:
            return

        album_id = await self._ensure_album(album_name)
        if not album_id:
            logger.warning("Could not resolve album ID, skipping: %s", album_name)
            return

        session = await self._get_session()
        url = f"{self.api_url}/api/albums/{album_id}/assets"
        payload = {"ids": asset_ids}

        try:
            async with session.put(url, json=payload) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    added = sum(1 for r in results if r.get("success"))
                    logger.info("Added %d/%d asset(s) to album '%s'", added, len(asset_ids), album_name)
                else:
                    body = await resp.json()
                    logger.error("Failed to add assets to album [%d]: %s", resp.status, body)
        except Exception as e:
            logger.exception("Error adding assets to album: %s", e)

    async def _restore_from_trash(self, asset_id: str) -> bool:
        """Try to restore an asset from the Immich trash.

        When a user deletes a file in Immich it goes to trash.  Re-uploading
        the same file returns ``duplicate``, so we call the restore endpoint
        to bring it back.
        """
        session = await self._get_session()
        url = f"{self.api_url}/api/trash/restore/assets"
        payload = {"ids": [asset_id]}
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 204:
                    logger.info("Restored from Immich trash: asset_id=%s", asset_id)
                    return True
                else:
                    logger.debug(
                        "Trash restore returned [%d] (asset_id=%s, probably not in trash)",
                        resp.status, asset_id,
                    )
                    return False
        except Exception as e:
            logger.warning("Trash restore error (asset_id=%s): %s", asset_id, e)
            return False

    async def upload_file(self, file_path: Path, *, force: bool = False) -> dict:
        """Upload a single file to Immich.

        Immich ``POST /api/assets`` expects multipart/form-data with fields:
        assetData, deviceAssetId, deviceId, fileCreatedAt, fileModifiedAt.

        When *force* is True and Immich returns ``duplicate``, the asset is
        automatically restored from trash (in case the user deleted it in
        Immich earlier).

        Returns e.g. {"id": "uuid", "status": "created"} or
        {"id": "uuid", "status": "duplicate"}.
        """
        if not file_path.exists():
            logger.warning("File not found, skipping: %s", file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        stat = file_path.stat()
        created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

        session = await self._get_session()
        url = f"{self.api_url}/api/assets"

        try:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field(
                    "assetData",
                    f,
                    filename=file_path.name,
                    content_type="application/octet-stream",
                )
                data.add_field("deviceAssetId", f"{self.device_id}-{file_path.name}")
                data.add_field("deviceId", self.device_id)
                data.add_field("fileCreatedAt", created_at)
                data.add_field("fileModifiedAt", modified_at)

                async with session.post(url, data=data) as resp:
                    body = await resp.json()

                    if resp.status in (200, 201):
                        status = body.get("status", "unknown")
                        asset_id = body.get("id", "")
                        if resp.status == 201:
                            logger.info(
                                "Uploaded to Immich: %s (asset_id=%s)",
                                file_path.name,
                                asset_id,
                            )
                        else:
                            # 200 = duplicate
                            if force and asset_id:
                                restored = await self._restore_from_trash(asset_id)
                                if restored:
                                    logger.info(
                                        "Duplicate but restored from trash: %s (asset_id=%s)",
                                        file_path.name,
                                        asset_id,
                                    )
                                    return {"status": "restored", "id": asset_id, "http": resp.status}
                            logger.info(
                                "Duplicate, skipped: %s (asset_id=%s)",
                                file_path.name,
                                asset_id,
                            )
                        return {"status": status, "id": asset_id, "http": resp.status}
                    else:
                        logger.error(
                            "Immich upload failed [%d]: %s -> %s",
                            resp.status,
                            file_path.name,
                            body,
                        )
                        return {"status": "error", "http": resp.status, "detail": body}
        except Exception as e:
            logger.exception("Immich upload error: %s", file_path.name)
            return {"status": "error", "detail": str(e)}

    async def upload_files(
        self,
        file_paths: list[Path],
        base_dir: Path,
        *,
        force: bool = True,
    ) -> dict:
        """Upload a specific list of files to Immich.

        Unlike ``upload_directory``, this only touches the files you pass in —
        useful for "files produced by this download" scenarios.

        Args:
            file_paths: Absolute paths to upload.
            base_dir: Download root (used to derive author name from the first
                      path component, e.g. ``base_dir/<author>/...``).
            force: If True, try restoring from Immich trash on duplicates.
        """
        stats = {"uploaded": 0, "duplicates": 0, "restored": 0, "skipped": 0, "failed": 0}

        media_files = [
            f for f in file_paths
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        ]

        if not media_files:
            logger.info("No media files to upload")
            return stats

        logger.info("Uploading %d media file(s) to Immich", len(media_files))

        author_asset_ids: dict[str, list[str]] = {}

        for file_path in media_files:
            # Derive author name from first path component under base_dir
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

        # Add assets to per-author albums
        for author_name, asset_ids in author_asset_ids.items():
            album_name = f"{self.album_prefix}{author_name}"
            await self._add_assets_to_album(album_name, asset_ids)

        return stats

    async def upload_directory(self, directory: Path) -> dict:
        """Recursively scan a directory and upload all matching media to Immich.

        Files are grouped by first-level subdirectory (author name), and each
        author gets its own album (``<album_prefix><author>``).
        """
        if not directory.exists():
            logger.warning("Directory does not exist: %s", directory)
            return {"uploaded": 0, "skipped": 0, "failed": 0, "deleted": 0}

        stats = {"uploaded": 0, "duplicates": 0, "skipped": 0, "failed": 0}

        media_files = sorted(
            f
            for f in directory.rglob("*")
            if f.is_file() and f.suffix.lower() in self.upload_extensions
        )

        if not media_files:
            logger.info("No media files in directory: %s", directory)
            return stats

        logger.info("Found %d media file(s) to upload: %s", len(media_files), directory)

        author_asset_ids: dict[str, list[str]] = {}

        for file_path in media_files:
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
                if asset_id and author_name:
                    author_asset_ids.setdefault(author_name, []).append(asset_id)
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        for author_name, asset_ids in author_asset_ids.items():
            album_name = f"{self.album_prefix}{author_name}"
            await self._add_assets_to_album(album_name, asset_ids)

        # Keep local files around — the downloader uses them for dedup
        return stats



# -- Singleton ---------------------------------------------------------
_uploader: Optional[ImmichUploader] = None


def get_immich_uploader(immich_config: Optional[dict] = None) -> Optional[ImmichUploader]:
    """Return the global ImmichUploader instance (created on first call).

    Reads from immich_config (the ``immich`` section of config.yml).
    api_url / api_key fall back to env vars IMMICH_API_URL / IMMICH_API_KEY.
    """
    global _uploader
    if _uploader is not None:
        return _uploader

    cfg = immich_config or {}

    if cfg.get("enabled") is False:
        return None

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
    logger.info("Immich enabled: %s (album_prefix=%s, device=%s, timeout=%ds)",
                api_url, _uploader.album_prefix, _uploader.device_id, _uploader.upload_timeout)
    return _uploader
