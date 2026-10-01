"""The Java video DTO fields remain camelCase in JSON and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VideoSegment:
    startMs: int
    endMs: int
    transcript: str = ""
    ocrTexts: list[str] = field(default_factory=list)
    evidenceFrames: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.startMs < 0 or self.endMs <= self.startMs:
            raise ValueError("invalid segment range")
        object.__setattr__(self, "transcript", (self.transcript or "").strip())
        object.__setattr__(self, "ocrTexts", list(self.ocrTexts or []))
        object.__setattr__(self, "evidenceFrames", list(self.evidenceFrames or []))

    @classmethod
    def from_dict(cls, value: dict) -> "VideoSegment":
        return cls(value["startMs"], value["endMs"], value.get("transcript"),
                   value.get("ocrTexts"), value.get("evidenceFrames"))


@dataclass(frozen=True)
class VideoContext:
    source: str
    userGoal: str = ""
    segments: list[VideoSegment] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source is None or not self.source.strip():
            raise ValueError("video source is required")
        object.__setattr__(self, "userGoal", (self.userGoal or "").strip())
        object.__setattr__(self, "segments", list(self.segments or []))

    def transcript_text(self) -> str:
        return "\n".join(segment.transcript for segment in self.segments if segment.transcript.strip())

    @classmethod
    def from_dict(cls, value: dict) -> "VideoContext":
        return cls(value["source"], value.get("userGoal"), [
            item if isinstance(item, VideoSegment) else VideoSegment.from_dict(item)
            for item in value.get("segments") or []
        ])


@dataclass(frozen=True)
class VideoChunk:
    startTime: int
    endTime: int
    segmentSummary: str = ""
    keywords: list[str] = field(default_factory=list)
    rawSegments: list[VideoSegment] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.startTime < 0 or self.endTime <= self.startTime:
            raise ValueError("invalid chunk range")
        object.__setattr__(self, "segmentSummary", (self.segmentSummary or "").strip())
        for field_name in ("keywords", "rawSegments", "embedding"):
            object.__setattr__(self, field_name, list(getattr(self, field_name) or []))

    @property
    def start_ms(self) -> int:
        return self.startTime

    @property
    def end_ms(self) -> int:
        return self.endTime

    @classmethod
    def from_dict(cls, value: dict) -> "VideoChunk":
        return cls(value["startTime"], value["endTime"], value.get("segmentSummary"),
                   value.get("keywords"), [
                       item if isinstance(item, VideoSegment) else VideoSegment.from_dict(item)
                       for item in value.get("rawSegments") or []
                   ], value.get("embedding"))


@dataclass(frozen=True)
class VideoRetrievalIntent:
    semanticQuery: str = ""
    keywords: list[str] = field(default_factory=list)
    visualKeywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "semanticQuery", (self.semanticQuery or "").strip())
        for field_name in ("keywords", "visualKeywords"):
            values = getattr(self, field_name) or []
            unique: list[str] = []
            for value in values:
                if value is None:
                    continue
                normalized = value.strip()
                if normalized and normalized not in unique:
                    unique.append(normalized)
                if len(unique) == 16:
                    break
            object.__setattr__(self, field_name, unique)


@dataclass(frozen=True)
class VideoEvidenceHit:
    startMs: int
    endMs: int
    source: str = ""
    snippet: str = ""
    transcript: str = ""
    ocrTexts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in ("source", "snippet", "transcript"):
            object.__setattr__(self, field_name, getattr(self, field_name) or "")
        object.__setattr__(self, "ocrTexts", list(self.ocrTexts or []))


@dataclass(frozen=True)
class VideoEvidence:
    timestampMs: int
    source: str = "UNKNOWN"
    content: str = ""
    claim: str = ""

    def __post_init__(self) -> None:
        if self.timestampMs < 0:
            raise ValueError("evidence timestamp cannot be negative")
        for field_name in ("source", "content", "claim"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, (value if value is not None else ("UNKNOWN" if field_name == "source" else "")).strip())
