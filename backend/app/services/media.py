"""Media persistence, ownership and list cache from MediaService.java."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import BinaryIO

from app.api.errors import BusinessError, ErrorCode
from app.db.models import MediaFile


VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})
LOG = logging.getLogger(__name__)


def _java_trim(value: str) -> str:
    return value.strip("".join(chr(i) for i in range(33)))


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def summary(media: MediaFile) -> dict[str, object]:
    return {
        "id": media.id,
        "filename": media.filename,
        "status": media.status,
        "coverUrl": media.cover_url,
        "uploadTime": media.upload_time.isoformat(timespec="milliseconds") if media.upload_time else None,
    }


class MediaService:
    def __init__(self, repository, redis_client, storage, *, checkpoints=None, telemetry=None, vector_store=None, video_context=None):
        self.repository = repository
        self.redis = redis_client
        self.storage = storage
        self.checkpoints = checkpoints
        self.telemetry = telemetry
        self.vector_store = vector_store
        self.video_context = video_context

    @staticmethod
    def normalize_video_filename(filename: str | None) -> str:
        if filename is None or not _java_trim(filename):
            raise ValueError("视频文件名不能为空")
        normalized = _java_trim(filename.replace("\\", "/").rsplit("/", 1)[-1])
        if not normalized or _java_length(normalized) > 255:
            raise ValueError("视频文件名无效或过长")
        suffix = normalized[normalized.rfind("."):].lower() if "." in normalized else ""
        if suffix not in VIDEO_SUFFIXES:
            raise ValueError("仅支持 MP4、MOV、MKV、AVI、WEBM 和 M4V 视频")
        return normalized

    @staticmethod
    def calculate_md5(stream: BinaryIO) -> str:
        position = stream.tell()
        digest = hashlib.md5()
        while chunk := stream.read(8192):
            digest.update(chunk)
        stream.seek(position)
        return digest.hexdigest()

    def _remember_hash(self, media_id: int, digest: str | None) -> None:
        if not digest:
            return
        try:
            self.redis.set(f"media:md5:{media_id}", digest)
        except Exception:
            pass

    def save_uploaded_media(self, filename: str, file_url: str, user_id: int, md5: str) -> MediaFile:
        media = MediaFile(
            filename=self.normalize_video_filename(filename), file_path=file_url,
            status="COMPLETED", upload_time=datetime.now(), user_id=user_id, content_hash=md5,
        )
        try:
            self.repository.insert(media)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            try:
                self.storage.remove_file(file_url)
            except Exception:
                pass
            raise
        self._remember_hash(media.id, md5)
        self.invalidate_user_list(user_id)
        return media

    def ingest_file(self, upload_file, user_id: int) -> MediaFile:
        filename = self.normalize_video_filename(upload_file.filename)
        stream = upload_file.file
        size = upload_file.size
        if size is None:
            position = stream.tell()
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(position)
        if size <= 0:
            raise ValueError("上传文件不能为空")
        digest = self.calculate_md5(stream)
        file_url = self.storage.upload_file(stream, size, filename, upload_file.content_type)
        return self.save_uploaded_media(filename, file_url, user_id, digest)

    def invalidate_user_list(self, user_id: int | None) -> None:
        if user_id is None:
            return
        try:
            self.redis.delete(f"media:list:v2:user:{user_id}")
        except Exception:
            pass

    def list_by_user(self, user_id: int) -> list[dict[str, object]]:
        cache_key = f"media:list:v2:user:{user_id}"
        try:
            cached = self.redis.get(cache_key)
            if cached is not None:
                parsed = json.loads(cached)
                if isinstance(parsed, list):
                    return [{key: item.get(key) for key in ("id", "filename", "status", "coverUrl", "uploadTime")} for item in parsed]
        except Exception:
            pass
        result = [summary(media) for media in self.repository.list_by_user(user_id)]
        try:
            self.redis.set(cache_key, json.dumps(result, ensure_ascii=False), ex=1800)
        except Exception:
            pass
        return result

    def require_owned_media(self, media_id: int, user_id: int) -> MediaFile:
        media = self.repository.get_by_id(media_id)
        if media is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "文件不存在")
        if media.user_id != user_id:
            raise PermissionError("无权访问该文件")
        return media

    def playback(self, media_id: int, user_id: int) -> str | None:
        media = self.require_owned_media(media_id, user_id)
        return self.storage.readable_source(media.file_path)

    def content_hash(self, media_id: int) -> str | None:
        try:
            cached = self.redis.get(f"media:md5:{media_id}")
            if cached and cached.strip():
                return cached
        except Exception:
            pass
        media = self.repository.get_by_id(media_id)
        digest = media.content_hash if media else None
        self._remember_hash(media_id, digest)
        return digest

    def exists(self, media_id: int | None) -> bool:
        return media_id is not None and self.repository.get_by_id(media_id) is not None

    def delete_owned_media(self, media_id: int, user_id: int) -> None:
        media = self.require_owned_media(media_id, user_id)
        try:
            self.repository.delete_by_id(media_id)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
        if media.file_path and media.file_path.startswith("http"):
            try:
                self.storage.remove_file(media.file_path)
            except Exception:
                LOG.warning("media_object_cleanup_failed mediaId=%s path=%s", media_id, media.file_path, exc_info=True)
        self.purge_runtime_artifacts(media_id)
        self.invalidate_user_list(user_id)

    def purge_runtime_artifacts(self, media_id: int) -> None:
        if any(dependency is None for dependency in (
            self.checkpoints, self.telemetry, self.vector_store, self.video_context,
        )):
            raise RuntimeError("media runtime cleanup dependencies are required")
        context = None
        try:
            context = self.checkpoints.load_context(media_id)
        except Exception:
            LOG.warning("media_evidence_manifest_read_failed mediaId=%s", media_id, exc_info=True)
        self.video_context.delete_evidence_frames(context)
        try:
            self.redis.delete(
                f"media:md5:{media_id}",
                f"transcription:active:{media_id}",
                f"transcription:state:{media_id}",
            )
            self.checkpoints.delete_media(media_id)
            self.telemetry.delete_task(media_id)
            self.vector_store.delete_media(media_id)
        except Exception:
            LOG.warning("media_runtime_cleanup_failed mediaId=%s", media_id, exc_info=True)
