"""Verbatim ASR/OCR evidence checks from EvidenceVerificationService.java."""

from __future__ import annotations

import unicodedata

from app.schemas.video import VideoContext, VideoEvidence, VideoSegment


def _evidence(value: VideoEvidence | dict | None) -> VideoEvidence | None:
    if value is None or isinstance(value, VideoEvidence):
        return value
    return VideoEvidence(value.get("timestampMs", 0), value.get("source"), value.get("content"), value.get("claim"))


def _context(value: VideoContext | dict | None) -> VideoContext | None:
    if value is None or isinstance(value, VideoContext):
        return value
    return VideoContext.from_dict(value)


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(
        character for character in value.lower()
        if not character.isspace() and unicodedata.category(character)[0] not in "PS"
    )


class EvidenceVerificationService:
    @staticmethod
    def _contains(segment: VideoSegment, timestamp_ms: int) -> bool:
        return segment.startMs <= timestamp_ms < segment.endMs

    def timestamp_covered(self, context: VideoContext | dict | None, evidence: VideoEvidence | dict | None) -> bool:
        context, evidence = _context(context), _evidence(evidence)
        return bool(context is not None and evidence is not None and any(
            self._contains(segment, evidence.timestampMs) for segment in context.segments
        ))

    def supported(self, context: VideoContext | dict | None, evidence: VideoEvidence | dict | None) -> bool:
        context, evidence = _context(context), _evidence(evidence)
        if context is None or evidence is None or not evidence.content.strip():
            return False
        source = evidence.source.upper()
        if "ASR" not in source and "OCR" not in source:
            return False
        wanted = _normalize(evidence.content)
        if not wanted:
            return False
        for segment in context.segments:
            if not self._contains(segment, evidence.timestampMs):
                continue
            if "ASR" in source and "OCR" in source:
                candidate = segment.transcript + " " + " ".join(segment.ocrTexts)
            elif "ASR" in source:
                candidate = segment.transcript
            else:
                candidate = " ".join(segment.ocrTexts)
            if wanted in _normalize(candidate):
                return True
        return False

    def supports_claim(self, context: VideoContext | dict | None, claim: str | None, evidence: VideoEvidence | dict | None) -> bool:
        evidence = _evidence(evidence)
        return bool(
            evidence is not None and _normalize(claim)
            and _normalize(claim) == _normalize(evidence.claim)
            and self.supported(context, evidence)
        )
