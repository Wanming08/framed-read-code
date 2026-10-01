"""AnalysisMode.fromNullable and fromRequest behavior from the Java enum."""

from enum import Enum


class AnalysisMode(str, Enum):
    GENERAL = "GENERAL"
    LEARNING = "LEARNING"
    REVIEW = "REVIEW"
    CREATION = "CREATION"

    @classmethod
    def from_nullable(cls, value: str | None) -> "AnalysisMode":
        if value is None or not value.strip():
            return cls.GENERAL
        try:
            return cls(value.strip().upper())
        except ValueError:
            return cls.GENERAL

    @classmethod
    def from_request(cls, value: str | None) -> "AnalysisMode":
        if value is None or not value.strip():
            return cls.GENERAL
        try:
            return cls(value.strip().upper())
        except ValueError as exc:
            raise ValueError("不支持的分析模式: " + value) from exc
