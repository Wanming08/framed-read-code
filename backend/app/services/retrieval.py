"""Query-aware ASR/OCR retrieval from VideoEvidenceRetrievalService.java."""

from __future__ import annotations

import math
import re
from typing import Any

from app.schemas.video import VideoChunk, VideoEvidenceHit, VideoRetrievalIntent, VideoSegment


TOP_K = 3
MAX_USER_HITS = 8
MAX_SNIPPET_LENGTH = 180


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").lower())


def _normalized_ocr(segment: VideoSegment) -> list[str]:
    unique: list[str] = []
    for value in segment.ocrTexts:
        if value is None:
            continue
        item = value.strip()
        if item and item not in unique:
            unique.append(item)
    return unique


def _term_score(terms: list[str], content: str) -> float:
    normalized_terms = list(dict.fromkeys(term for value in terms if (term := _normalize(value))))
    if not normalized_terms:
        return 0.0
    normalized_content = _normalize(content)
    return sum(term in normalized_content for term in normalized_terms) / len(normalized_terms)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_length = sum(value * value for value in left)
    right_length = sum(value * value for value in right)
    if not left_length or not right_length:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / math.sqrt(left_length * right_length)


class VideoEvidenceRetrievalService:
    def __init__(self, model: Any, embeddings: Any, vector_store: Any, telemetry: Any) -> None:
        self.model = model
        self.embeddings = embeddings
        self.vector_store = vector_store
        self.telemetry = telemetry

    def retrieve(self, media_id: int | None, goal: str, chunks: list[VideoChunk]) -> list[VideoSegment]:
        return [row[0] for row in self._rank(media_id, goal, chunks)]

    def search(self, media_id: int | None, query: str, chunks: list[VideoChunk]) -> list[VideoEvidenceHit]:
        return [self._to_hit(row) for row in self._rank(media_id, query, chunks)[:MAX_USER_HITS]]

    def index(self, media_id: int, chunks: list[VideoChunk]) -> None:
        try:
            self.vector_store.upsert(media_id, chunks)
            self.telemetry.increment_current("vectorStoreWrites", len(chunks))
        except Exception:
            self.telemetry.increment_current("vectorStoreFallbacks", 1)

    def _rank(self, media_id: int | None, goal: str, chunks: list[VideoChunk]) -> list[tuple[VideoSegment, float, float, float]]:
        intent = self._retrieval_intent(goal)
        query_embedding = self._embed(intent.semanticQuery)
        vector_scores = self._vector_scores(media_id, query_embedding)
        ranked_chunks = sorted(
            ((chunk, self._score_chunk(intent, query_embedding, vector_scores, chunk)) for chunk in chunks),
            key=lambda row: -row[1],
        )[:TOP_K]
        if ranked_chunks:
            self.telemetry.value_current("retrievalTopScore", ranked_chunks[0][1])
            self.telemetry.increment_current("retrievalChunks", len(ranked_chunks))
        ranked_segments = [
            self._score_segment(intent, chunk_score, segment)
            for chunk, chunk_score in ranked_chunks for segment in chunk.rawSegments
        ]
        return sorted(ranked_segments, key=lambda row: (-row[1], row[0].startMs))

    def _retrieval_intent(self, goal: str) -> VideoRetrievalIntent:
        try:
            value = self.model.plan_retrieval(goal)
            intent = value if isinstance(value, VideoRetrievalIntent) else VideoRetrievalIntent(
                value.get("semanticQuery"), value.get("keywords"), value.get("visualKeywords")
            )
            if intent.semanticQuery.strip():
                return intent
        except Exception:
            self.telemetry.increment_current("retrievalIntentFallbacks", 1)
        fallback = self._fallback_terms(goal)
        return VideoRetrievalIntent(goal, fallback, fallback)

    @staticmethod
    def _fallback_terms(goal: str | None) -> list[str]:
        if not goal or not goal.strip():
            return []
        values = re.split(r"[\s，。！？、,.;:：；!?]+", goal.strip())
        terms = list(dict.fromkeys(value.strip() for value in values if len(value.strip()) >= 2))[:8]
        return terms or [goal.strip()]

    def _embed(self, text: str) -> list[float]:
        try:
            return self.embeddings.embed(text)
        except Exception:
            self.telemetry.increment_current("embeddingFallbacks", 1)
            return []

    def _vector_scores(self, media_id: int | None, query_embedding: list[float]) -> dict[str, float]:
        if media_id is None or not query_embedding:
            return {}
        try:
            return {f"{hit.start_ms}:{hit.end_ms}": hit.score for hit in self.vector_store.search(media_id, query_embedding, TOP_K * 2)}
        except Exception:
            self.telemetry.increment_current("vectorStoreFallbacks", 1)
            return {}

    def _score_chunk(self, intent: VideoRetrievalIntent, embedding: list[float], remote: dict[str, float], chunk: VideoChunk) -> float:
        semantic = remote.get(f"{chunk.startTime}:{chunk.endTime}")
        if semantic is None:
            semantic = _cosine(embedding, chunk.embedding)
        searchable = " ".join([
            chunk.segmentSummary, " ".join(chunk.keywords),
            " ".join(segment.transcript for segment in chunk.rawSegments),
        ])
        visual = " ".join(text for segment in chunk.rawSegments for text in _normalized_ocr(segment))
        return semantic * 0.6 + _term_score(intent.keywords, searchable) * 0.25 + _term_score(intent.visualKeywords, visual) * 0.15

    @staticmethod
    def _score_segment(intent: VideoRetrievalIntent, chunk_score: float, segment: VideoSegment) -> tuple[VideoSegment, float, float, float]:
        transcript = _term_score(intent.keywords, segment.transcript)
        visual = _term_score(intent.visualKeywords, " ".join(_normalized_ocr(segment)))
        return (segment, chunk_score * 0.55 + transcript * 0.25 + visual * 0.20, transcript, visual)

    @staticmethod
    def _to_hit(row: tuple[VideoSegment, float, float, float]) -> VideoEvidenceHit:
        segment, _score, transcript_score, visual_score = row
        ocr_texts = _normalized_ocr(segment)
        has_transcript = bool(segment.transcript.strip())
        has_ocr = bool(ocr_texts)
        source = "ASR+OCR" if has_transcript and has_ocr else "OCR" if has_ocr else "ASR" if has_transcript else "时间片段"
        ocr_text = " ".join(ocr_texts)
        preferred = ocr_text if visual_score > transcript_score else segment.transcript
        if not preferred.strip():
            preferred = ocr_text if has_ocr else segment.transcript
        if not preferred.strip():
            preferred = "该时间段暂无可展示文本"
        snippet = re.sub(r"\s+", " ", preferred).strip()
        if len(snippet) > MAX_SNIPPET_LENGTH:
            snippet = snippet[:MAX_SNIPPET_LENGTH] + "..."
        return VideoEvidenceHit(segment.startMs, segment.endMs, source, snippet, segment.transcript, ocr_texts)
