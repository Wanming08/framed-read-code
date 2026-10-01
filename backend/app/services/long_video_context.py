"""Checkpoint-backed long-video context selection from the Java service."""

from __future__ import annotations

from typing import Any

from app.schemas.video import VideoChunk, VideoContext, VideoSegment


CHUNK_MS = 5 * 60 * 1000
MAX_CONTEXT_CHARS = 24_000


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


class LongVideoContextService:
    def __init__(self, telemetry: Any, checkpoints: Any, chunking: Any, retrieval: Any) -> None:
        self.telemetry = telemetry
        self.checkpoints = checkpoints
        self.chunking = chunking
        self.retrieval = retrieval

    def select_relevant(self, media_id: int | VideoContext | None, context: VideoContext | dict | None = None) -> VideoContext:
        if context is None:
            context, media_id = media_id, None
        if isinstance(context, dict):
            context = VideoContext.from_dict(context)
        assert isinstance(context, VideoContext)
        if not context.segments or context.segments[-1].endMs <= CHUNK_MS:
            return self._within_budget(context, context.segments)
        chunks = self._resolve_chunks(media_id, context.segments)
        selected = self.retrieval.retrieve(media_id, context.userGoal, chunks)
        return self._within_budget(context, selected)

    def search_evidence(self, media_id: int | None, context: VideoContext | dict) -> list:
        if isinstance(context, dict):
            context = VideoContext.from_dict(context)
        if not context.segments:
            return []
        return self.retrieval.search(media_id, context.userGoal, self._resolve_chunks(media_id, context.segments))

    def refine_for_critique(
        self, media_id: int | None, full_context: VideoContext | dict,
        selected_context: VideoContext | dict, critique: Any,
    ) -> VideoContext:
        if isinstance(full_context, dict):
            full_context = VideoContext.from_dict(full_context)
        if isinstance(selected_context, dict):
            selected_context = VideoContext.from_dict(selected_context)
        required = _field(critique, "requiredTimestamps", []) or []
        selected: dict[str, VideoSegment] = {}
        for segment in full_context.segments:
            if any(self._near_segment(timestamp, segment) for timestamp in required):
                selected[self._segment_key(segment)] = segment
        query = self._critique_query(full_context.userGoal, critique)
        retry = self.select_relevant(media_id, VideoContext(full_context.source, query, full_context.segments))
        for segment in retry.segments:
            selected.setdefault(self._segment_key(segment), segment)
        for segment in selected_context.segments:
            selected.setdefault(self._segment_key(segment), segment)
        return self._within_budget(full_context, list(selected.values()))

    @staticmethod
    def _critique_query(goal: str, critique: Any) -> str:
        if critique is None:
            return goal
        return "\n".join([
            goal,
            " ".join(_field(critique, "feedback", []) or []),
            " ".join(_field(critique, "missingRequirements", []) or []),
            " ".join(_field(critique, "unsupportedClaims", []) or []),
        ])

    @staticmethod
    def _segment_key(segment: VideoSegment) -> str:
        return f"{segment.startMs}:{segment.endMs}"

    @staticmethod
    def _near_segment(timestamp: int, segment: VideoSegment) -> bool:
        margin = max(60_000, segment.endMs - segment.startMs)
        return max(0, segment.startMs - margin) <= timestamp < segment.endMs + margin

    def _within_budget(self, context: VideoContext, candidates: list[VideoSegment]) -> VideoContext:
        selected: list[VideoSegment] = []
        used_chars = 0
        for segment in candidates:
            segment_chars = _java_length(segment.transcript) + sum(_java_length(value) for value in segment.ocrTexts)
            if selected and used_chars + segment_chars > MAX_CONTEXT_CHARS:
                continue
            selected.append(segment)
            used_chars += segment_chars
        self.telemetry.increment_current("contextSegmentsDropped", len(candidates) - len(selected))
        self.telemetry.value_current("contextChars", used_chars)
        selected.sort(key=lambda segment: segment.startMs)
        return VideoContext(context.source, context.userGoal, selected)

    def _resolve_chunks(self, media_id: int | None, segments: list[VideoSegment]) -> list[VideoChunk]:
        cached = self.checkpoints.load_chunks(media_id) if media_id is not None else None
        if cached:
            self.telemetry.increment_current("chunkCheckpointHits", 1)
            return [item if isinstance(item, VideoChunk) else VideoChunk.from_dict(item) for item in cached]
        chunks = self.chunking.build(segments)
        if media_id is not None:
            self.checkpoints.save_chunks(media_id, chunks)
            self.retrieval.index(media_id, chunks)
        return chunks
