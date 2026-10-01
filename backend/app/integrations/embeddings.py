"""SiliconFlow-compatible embedding request from EmbeddingUtils.java."""

from __future__ import annotations

import time
from typing import Any

import httpx


class EmbeddingClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        base_url: str,
        model: str = "BAAI/bge-m3",
        telemetry: Any = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.telemetry = telemetry

    def embed(self, text: str | None) -> list[float]:
        if text is None or not text.strip():
            return []
        try:
            started_ns = time.monotonic_ns()
            try:
                response = self.client.post(
                    self.base_url + "/embeddings",
                    headers={"Authorization": "Bearer " + self.api_key},
                    json={"model": self.model, "input": text},
                    timeout=httpx.Timeout(connect=30, read=120, write=10, pool=5),
                )
            finally:
                if self.telemetry is not None:
                    self.telemetry.provider_request_current("embedding", started_ns)
            if not response.is_success:
                raise RuntimeError(f"Embedding API failed: {response.status_code}")
            payload: dict[str, Any] = response.json()
            if self.telemetry is not None and isinstance(payload, dict):
                self.telemetry.provider_usage_current("embedding", payload.get("usage"))
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise RuntimeError("Embedding data is empty")
            values = data[0].get("embedding")
            if not isinstance(values, list) or not values:
                raise RuntimeError("Embedding vector is empty")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
                raise TypeError("Embedding vector contains non-numeric values")
            return [float(value) for value in values]
        except Exception as error:
            raise RuntimeError("Embedding 生成失败") from error
