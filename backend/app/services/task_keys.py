"""Byte-compatible identity and Redis key names from AnalysisTaskKeys.java."""

import hashlib
import re

from app.schemas.mode import AnalysisMode


MD5_PATTERN = re.compile(r"[a-fA-F0-9]{32}\Z")
UNIT_SEPARATOR = "␟"  # U+241F; deliberately not ASCII U+001F.


def _java_trim(value: str) -> str:
    return value.strip("".join(chr(i) for i in range(0x21)))


def _java_blank(value: str) -> bool:
    # Java Character.isWhitespace excludes the three non-breaking spaces.
    return not value or all(character.isspace() and character not in "\u00a0\u2007\u202f" for character in value)


def normalize_content_hash(media_id: int, content_hash: str | None) -> str:
    if content_hash is not None and MD5_PATTERN.fullmatch(content_hash):
        return content_hash.lower()
    return f"media-{media_id}"


def goal_digest(goal: str | None, mode: AnalysisMode | None = None) -> str:
    if goal is None or _java_blank(goal):
        raise ValueError("analysis goal is required")
    normalized = _java_trim(goal)
    source = normalized if mode is None or mode is AnalysisMode.GENERAL else mode.name + UNIT_SEPARATOR + normalized
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def active_key(content_hash: str, digest: str) -> str:
    return f"analysis:active:{content_hash}:{digest}"


def lock_key(content_hash: str, digest: str) -> str:
    return f"lock:analysis:{content_hash}:{digest}"


def completed_key(content_scope: str, digest: str) -> str:
    return f"analysis:completed:{content_scope}:{digest}"


def attempts_key(content_scope: str, digest: str) -> str:
    return f"analysis:attempts:{content_scope}:{digest}"


def context_owner_key(content_hash: str) -> str:
    return f"analysis:context-owner:{content_hash}"


def context_lock_key(content_hash: str) -> str:
    return f"lock:analysis-context:{content_hash}"
