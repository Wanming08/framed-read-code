"""Thin MinIO adapter preserving the original object URLs and filenames."""

from __future__ import annotations

import mimetypes
import re
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse
from uuid import uuid4

from minio import Minio


class ObjectStorage:
    def __init__(self, client: Minio, endpoint: str, bucket: str, public_client: Minio | None = None):
        self.client = client
        self.public_client = public_client or client
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings) -> "ObjectStorage":
        parsed = urlparse(settings.minio_endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid MinIO endpoint")
        client = Minio(
            parsed.netloc,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=parsed.scheme == "https",
        )
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
        public_endpoint = getattr(settings, "minio_public_endpoint", None) or settings.minio_endpoint
        public = urlparse(public_endpoint)
        if public.scheme not in {"http", "https"} or not public.netloc:
            raise ValueError("invalid public MinIO endpoint")
        signer = client
        if public_endpoint.rstrip("/") != settings.minio_endpoint.rstrip("/"):
            # Signing is local; pin the bucket region so the public-only host is
            # never contacted from inside the container during URL generation.
            signer = Minio(
                public.netloc,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=public.scheme == "https",
                region="us-east-1",
            )
        return cls(client, public_endpoint, settings.minio_bucket, signer)

    @staticmethod
    def _suffix(filename: str | None) -> str:
        if filename is None:
            return ""
        suffix = filename[filename.rfind("."):] if "." in filename else ""
        if len(suffix) > 11 or not re.fullmatch(r"\.[a-z0-9]+", suffix.lower()):
            return ""
        return suffix.lower()

    @staticmethod
    def _validate_name(object_name: str) -> None:
        if not object_name or object_name.startswith("/") or ".." in object_name:
            raise ValueError("invalid MinIO object name")

    def object_url(self, object_name: str) -> str:
        self._validate_name(object_name)
        return f"{self.endpoint}/{self.bucket}/{object_name}"

    def upload_object(self, object_name: str, stream: BinaryIO, size: int, content_type: str) -> str:
        self._validate_name(object_name)
        self.client.put_object(self.bucket, object_name, stream, size, content_type=content_type)
        return self.object_url(object_name)

    def upload_file(self, stream: BinaryIO, size: int, filename: str, content_type: str | None) -> str:
        name = str(uuid4()) + self._suffix(filename)
        return self.upload_object(name, stream, size, content_type or "application/octet-stream")

    def upload_local_file(self, path: str | Path, filename: str, prefix: str = "") -> str:
        path = Path(path)
        if not path.is_file():
            raise ValueError("local file does not exist")
        normalized_prefix = prefix.strip("/")
        name = (normalized_prefix + "/" if normalized_prefix else "") + str(uuid4()) + self._suffix(filename)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            return self.upload_object(name, stream, path.stat().st_size, content_type)

    def copy_object_to(self, object_name: str, output: BinaryIO) -> None:
        self._validate_name(object_name)
        response = None
        try:
            response = self.client.get_object(self.bucket, object_name)
            while chunk := response.read(8192):
                output.write(chunk)
        except Exception as exc:
            raise RuntimeError("MinIO 文件读取失败") from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def remove_object(self, object_name: str) -> None:
        self._validate_name(object_name)
        try:
            self.client.remove_object(self.bucket, object_name)
        except Exception as exc:
            raise RuntimeError("MinIO 文件删除失败") from exc

    def is_managed_file(self, file_url: str | None) -> bool:
        return bool(file_url and file_url.startswith(f"{self.endpoint}/{self.bucket}/"))

    def _object_name(self, file_url: str) -> str:
        path = urlparse(file_url).path
        prefix = f"/{self.bucket}/"
        offset = path.find(prefix)
        if offset < 0:
            raise ValueError("invalid MinIO object URL")
        name = path[offset + len(prefix):]
        self._validate_name(name)
        return name

    def remove_file(self, file_url: str | None) -> None:
        if self.is_managed_file(file_url):
            self.remove_object(self._object_name(file_url))

    def readable_source(self, source: str | None) -> str | None:
        if not self.is_managed_file(source):
            return source
        try:
            return self.public_client.presigned_get_object(self.bucket, self._object_name(source), expires=timedelta(hours=1))
        except Exception as exc:
            raise RuntimeError("MinIO 预签名地址生成失败") from exc

    def processing_source(self, source: str | None) -> str | None:
        """Sign for FFmpeg inside Compose; browser playback uses readable_source."""
        if not self.is_managed_file(source):
            return source
        try:
            return self.client.presigned_get_object(self.bucket, self._object_name(source), expires=timedelta(hours=1))
        except Exception as exc:
            raise RuntimeError("MinIO 内部预签名地址生成失败") from exc
