"""Five-minute knowledge chunks from VideoChunkingService.java."""

from __future__ import annotations

from typing import Any

from app.schemas.video import VideoChunk, VideoSegment


CHUNK_MS = 5 * 60 * 1000


def _normalize_texts(values: list[str] | None) -> list[str]:
    unique: list[str] = []
    for value in values or []:
        if value is None:
            continue
        normalized = value.strip()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


class VideoChunkingService:
    def __init__(self, model: Any, embeddings: Any, telemetry: Any) -> None:
        self.model = model
        self.embeddings = embeddings
        self.telemetry = telemetry

    def build(self, segments: list[VideoSegment]) -> list[VideoChunk]:
        ordered = sorted((segment for segment in segments if segment is not None), key=lambda value: value.startMs)
        if not ordered:
            return []
        chunks: list[VideoChunk] = []
        for start in range(0, ordered[-1].startMs + 1, CHUNK_MS):
            end = start + CHUNK_MS
            raw = [segment for segment in ordered if start <= segment.startMs < end]
            if not raw:
                continue
            summary = self._summarize(raw)
            text = (summary["segmentSummary"] or "").strip()
            keywords = _normalize_texts(summary.get("keywords"))
            embedding = self._embed(text + "\n" + " ".join(keywords))
            chunks.append(VideoChunk(start, end, text, keywords, raw, embedding))
        return chunks

    def _summarize(self, segments: list[VideoSegment]) -> dict[str, Any]:
        try:
            result = self.model.summarize_chunk(segments)
            if not isinstance(result, dict):
                raise TypeError("chunk summary must be an object")
            return result
        except Exception:
            self.telemetry.increment_current("summaryFallbacks", 1)
            raw = " ".join(
                text for segment in segments
                if (text := segment.transcript + " " + " ".join(_normalize_texts(segment.ocrTexts))).strip()
            )
            return {"segmentSummary": raw[:500], "keywords": []}

    def _embed(self, text: str) -> list[float]:
        try:
            return self.embeddings.embed(text)
        except Exception:
            self.telemetry.increment_current("embeddingFallbacks", 1)
            return []
