"""Multipart transcription and retry boundary from AliyunAsrUtils.java."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx


class ASRRejectedError(ValueError):
    """Permanent request/authorization/audio-format rejection (all 4xx except 429)."""


class ASRCallError(RuntimeError):
    """Retryable ASR failure exhausted the three local attempts."""


class _RetryableASRError(OSError):
    pass


class ASRClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        url: str,
        model: str = "TeleAI/TeleSpeechASR",
        sleep: Callable[[float], None] = time.sleep,
        telemetry: Any = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.url = url
        self.model = model
        self.sleep = sleep
        self.telemetry = telemetry

    def audio_to_text(self, file_path: str | Path) -> str:
        audio = Path(file_path)
        if not audio.is_file():
            raise RuntimeError("ASR audio file does not exist")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                # Reopen on each attempt: multipart streams are consumed by the request.
                with audio.open("rb") as stream:
                    started_ns = time.monotonic_ns()
                    try:
                        response = self.client.post(
                            self.url,
                            headers={"Authorization": "Bearer " + self.api_key},
                            data={"model": self.model},
                            files={"file": (audio.name, stream, "application/octet-stream")},
                            timeout=httpx.Timeout(connect=30, read=180, write=180, pool=5),
                        )
                    finally:
                        if self.telemetry is not None:
                            self.telemetry.provider_request_current("asr", started_ns)
                if response.is_success:
                    text = response.json().get("text")
                    if text is None or not isinstance(text, str) or not text.strip():
                        raise RuntimeError("ASR 返回空文本")
                    return text.strip()
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableASRError(f"ASR transient HTTP {response.status_code}")
                raise ASRRejectedError(f"ASR request rejected with HTTP {response.status_code}")
            except (httpx.RequestError, OSError) as error:
                last_error = error
                if attempt < 2:
                    self.sleep(1 << attempt)
        raise ASRCallError("ASR 调用失败，已达到最大重试次数") from last_error
