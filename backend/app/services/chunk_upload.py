"""Resumable multipart upload using the original Redis keys and MinIO objects."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
from pathlib import Path
from uuid import UUID, uuid4

from app.api.errors import BusinessError, ErrorCode

logger = logging.getLogger(__name__)
MAX_CHUNK_BYTES = 5 * 1024 * 1024
MAX_TOTAL_CHUNKS = 410
SESSION_TTL = 86400


class _DigestWriter:
    def __init__(self, output, digest):
        self.output = output
        self.digest = digest

    def write(self, data):
        self.digest.update(data)
        return self.output.write(data)


class _MergeLease:
    """Redis lock with renewal; fail closed if the merge lease is lost."""

    def __init__(self, redis_client, key: str):
        self.lock = redis_client.lock(key, timeout=60, thread_local=False)
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.worker: threading.Thread | None = None

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            raise BusinessError(ErrorCode.CONFLICT, "该上传任务正在合并中，请稍后重试")
        self.worker = threading.Thread(target=self._renew, daemon=True)
        self.worker.start()
        return self

    def _renew(self):
        while not self.stop.wait(15):
            try:
                if not self.lock.extend(additional_time=60, replace_ttl=True):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set() or not self.lock.owned():
            raise RuntimeError("upload merge lock was lost")

    def __exit__(self, _type, _value, _traceback):
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=1)
        try:
            if self.lock.owned():
                self.lock.release()
        except Exception:
            logger.warning("upload_merge_lock_release_failed", exc_info=True)


class ChunkUploadService:
    def __init__(self, redis_client, storage, media_service):
        self.redis = redis_client
        self.storage = storage
        self.media = media_service

    @staticmethod
    def _validate_id(upload_id: str) -> None:
        try:
            UUID(upload_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid uploadId") from exc

    @staticmethod
    def _upload_key(upload_id: str) -> str:
        return f"upload:chunked:{upload_id}"

    @classmethod
    def _parts_key(cls, upload_id: str) -> str:
        return cls._upload_key(upload_id) + ":parts"

    @classmethod
    def _completed_key(cls, upload_id: str) -> str:
        return cls._upload_key(upload_id) + ":completed"

    @classmethod
    def _chunk_name(cls, upload_id: str, index: int) -> str:
        cls._validate_id(upload_id)
        return f"chunk-uploads/{upload_id}/part-{index}"

    def initialize(self, filename: str, total_chunks: int, user_id: int) -> str:
        normalized = self.media.normalize_video_filename(filename)
        if total_chunks <= 0 or total_chunks > MAX_TOTAL_CHUNKS:
            raise ValueError("totalChunks must be between 1 and 410")
        if user_id is None:
            raise PermissionError("missing authenticated user")
        upload_id = str(uuid4())
        key = self._upload_key(upload_id)
        self.redis.hset(key, mapping={
            "filename": normalized, "totalChunks": str(total_chunks), "userId": str(user_id),
        })
        self.redis.expire(key, SESSION_TTL)
        return upload_id

    def _require_upload(self, upload_id: str, user_id: int) -> dict[str, str]:
        self._validate_id(upload_id)
        metadata = self.redis.hgetall(self._upload_key(upload_id))
        if not metadata:
            raise ValueError("uploadId does not exist or has expired")
        if str(metadata.get("userId")) != str(user_id):
            raise PermissionError("无权访问该上传任务")
        return metadata

    def uploaded_chunks(self, upload_id: str, user_id: int) -> list[int]:
        self._require_upload(upload_id, user_id)
        return sorted(int(member) for member in self.redis.smembers(self._parts_key(upload_id)))

    def upload_chunk(self, upload_id: str, index: int, total_chunks: int, stream, size: int, user_id: int) -> None:
        if size <= 0:
            raise ValueError("chunk is empty")
        if size > MAX_CHUNK_BYTES:
            raise ValueError("chunk size cannot exceed 5MB")
        metadata = self._require_upload(upload_id, user_id)
        expected = int(metadata["totalChunks"])
        if total_chunks != expected or index < 0 or index >= expected:
            raise ValueError("invalid chunk index or totalChunks")
        self.storage.upload_object(self._chunk_name(upload_id, index), stream, size, "application/octet-stream")
        parts_key = self._parts_key(upload_id)
        self.redis.sadd(parts_key, str(index))
        self.redis.expire(self._upload_key(upload_id), SESSION_TTL)
        self.redis.expire(parts_key, SESSION_TTL)

    def _completed_upload(self, upload_id: str, user_id: int):
        media_id = self.redis.get(self._completed_key(upload_id))
        if media_id is None:
            return None
        try:
            return self.media.require_owned_media(int(media_id), user_id)
        except ValueError:
            self.redis.delete(self._completed_key(upload_id))
            return None

    def _cleanup(self, upload_id: str, total_chunks: int, media_id: int) -> None:
        for index in range(total_chunks):
            try:
                self.storage.remove_object(self._chunk_name(upload_id, index))
            except Exception:
                logger.warning("chunk_object_cleanup_failed uploadId=%s chunkIndex=%s mediaId=%s", upload_id, index, media_id, exc_info=True)
        try:
            self.redis.delete(self._upload_key(upload_id), self._parts_key(upload_id))
        except Exception:
            logger.warning("chunk_metadata_cleanup_failed uploadId=%s mediaId=%s", upload_id, media_id, exc_info=True)

    def complete(self, upload_id: str, user_id: int):
        self._validate_id(upload_id)
        with _MergeLease(self.redis, f"lock:upload:merge:{upload_id}") as lease:
            completed = self._completed_upload(upload_id, user_id)
            if completed is not None:
                return completed
            metadata = self._require_upload(upload_id, user_id)
            filename = metadata["filename"]
            total_chunks = int(metadata["totalChunks"])
            uploaded = self.uploaded_chunks(upload_id, user_id)
            if len(uploaded) != total_chunks:
                raise BusinessError(ErrorCode.CONFLICT, f"分片尚未全部上传完成（已传 {len(uploaded)}/{total_chunks}）")
            suffix = Path(filename).suffix
            with tempfile.NamedTemporaryFile(prefix="dovideo-merged-", suffix=suffix, delete=False) as temporary:
                path = Path(temporary.name)
            try:
                digest = hashlib.md5()
                with path.open("wb") as output:
                    writer = _DigestWriter(output, digest)
                    for index in range(total_chunks):
                        lease.check()
                        self.storage.copy_object_to(self._chunk_name(upload_id, index), writer)
                lease.check()
                file_url = self.storage.upload_local_file(path, filename)
                lease.check()
                media = self.media.save_uploaded_media(filename, file_url, user_id, digest.hexdigest())
                self.redis.set(self._completed_key(upload_id), media.id, ex=SESSION_TTL)
                self._cleanup(upload_id, total_chunks, media.id)
                return media
            finally:
                path.unlink(missing_ok=True)
