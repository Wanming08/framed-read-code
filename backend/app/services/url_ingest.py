"""Temporary URL download, MinIO upload and media registration."""

from __future__ import annotations

import logging


LOG = logging.getLogger(__name__)


class UrlIngestService:
    def __init__(self, downloader, media_service):
        self.downloader = downloader
        self.media = media_service

    def ingest_url(self, url: str, user_id: int):
        if url is None or not url.strip():
            raise ValueError("视频链接不能为空")
        temp_file = None
        try:
            temp_file = self.downloader.download_video(url)
            with temp_file.open("rb") as stream:
                digest = self.media.calculate_md5(stream)
            file_url = self.media.storage.upload_local_file(temp_file, temp_file.name)
            return self.media.save_uploaded_media("WEB_" + temp_file.name, file_url, user_id, digest)
        finally:
            if temp_file is not None:
                try:
                    temp_file.unlink(missing_ok=True)
                except Exception:
                    LOG.warning("temporary_video_cleanup_failed path=%s", temp_file, exc_info=True)
