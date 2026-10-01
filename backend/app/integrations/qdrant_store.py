"""Qdrant REST adapter preserving the Java payload and point identity."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import UUID

import httpx

from app.schemas.video import VideoChunk


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class VectorHit:
    start_ms: int
    end_ms: int
    score: float


class QdrantStore:
    def __init__(
        self, enabled: bool, base_url: str, api_key: str, collection: str,
        *, client: httpx.Client | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", collection):
            raise ValueError("Qdrant collection name is invalid")
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.collection = collection
        self._ready = False
        self._lock = Lock()
        self._owned_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(connect=3, read=10, write=10, pool=3))

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    @staticmethod
    def point_id(media_id: int, chunk: VideoChunk) -> str:
        source = f"{media_id}:{chunk.startTime}:{chunk.endTime}".encode("utf-8")
        return str(UUID(bytes=hashlib.md5(source).digest(), version=3))

    def _url(self, path: str) -> str:
        return self.base_url + "/collections/" + self.collection + path

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"api-key": self.api_key} if self.api_key.strip() else {}
        response = self.client.request(method, self._url(path), headers=headers, json=json, params=params)
        if not response.is_success:
            raise RuntimeError(f"Qdrant API failed: {response.status_code} {response.text}")
        return response

    def _ensure_collection(self, dimensions: int) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            headers = {"api-key": self.api_key} if self.api_key.strip() else {}
            response = self.client.get(self._url(""), headers=headers)
            if response.status_code == 404:
                self._request("PUT", "", json={"vectors": {"size": dimensions, "distance": "Cosine"}})
            elif not response.is_success:
                raise RuntimeError(f"Qdrant collection lookup failed: {response.status_code}")
            self._ready = True

    def upsert(self, media_id: int, chunks: list[VideoChunk]) -> None:
        if not self.enabled:
            return
        vectorized = [chunk for chunk in chunks if chunk.embedding]
        if not vectorized:
            return
        try:
            self._ensure_collection(len(vectorized[0].embedding))
            points = [{
                "id": self.point_id(media_id, chunk),
                "vector": chunk.embedding,
                "payload": {"mediaId": media_id, "startMs": chunk.startTime, "endMs": chunk.endTime},
            } for chunk in vectorized]
            self._request("PUT", "/points", json={"points": points}, params={"wait": "true"})
        except Exception as error:
            self._ready = False
            raise RuntimeError("Qdrant 分段向量写入失败") from error

    def search(self, media_id: int, query_embedding: list[float], limit: int) -> list[VectorHit]:
        if not self.enabled or not query_embedding:
            return []
        try:
            self._ensure_collection(len(query_embedding))
            response = self._request("POST", "/points/query", json={
                "query": query_embedding,
                "filter": {"must": [{"key": "mediaId", "match": {"value": media_id}}]},
                "limit": limit,
                "with_payload": True,
            })
            points = (response.json().get("result") or {}).get("points") or []
            return [VectorHit(int(point["payload"].get("startMs") or 0), int(point["payload"].get("endMs") or 0), float(point.get("score") or 0))
                    for point in points if point.get("payload") is not None]
        except Exception as error:
            self._ready = False
            raise RuntimeError("Qdrant 语义检索失败") from error

    def delete_media(self, media_id: int) -> None:
        if not self.enabled:
            return
        try:
            self._request("POST", "/points/delete", json={
                "filter": {"must": [{"key": "mediaId", "match": {"value": media_id}}]},
            }, params={"wait": "true"})
        except Exception:
            LOG.warning("qdrant_media_cleanup_failed mediaId=%s", media_id, exc_info=True)
