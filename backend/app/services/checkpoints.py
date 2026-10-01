"""Media and goal scoped checkpoint operations from AgentCheckpointService.java."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.db.checkpoints import AgentCheckpointRepository
from app.schemas.mode import AnalysisMode
from app.services.task_keys import goal_digest


logger = logging.getLogger(__name__)
FEEDBACK_TTL = 30 * 86400
GOAL_INDEX_TTL = 7 * 86400
MAX_FEEDBACK_SAMPLES = 200


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _stage_from_critique(state: Any, success: str, warning: str) -> str:
    critique = _field(state, "critique")
    return success if critique is not None and bool(_field(critique, "passed", False)) else warning


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("checkpoint value must be an object")
    return value


def _text(value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError("checkpoint text field has the wrong type")


def _integer(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("checkpoint integer field has the wrong type")


def _list(value: Any, item_validator) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("checkpoint list field has the wrong type")
    for item in value:
        if item is None:
            raise ValueError("checkpoint list cannot contain null")
        item_validator(item)


def _segment(value: Any) -> None:
    segment = _object(value)
    start, end = segment.get("startMs", 0), segment.get("endMs", 0)
    _integer(start)
    _integer(end)
    if start < 0 or end <= start:
        raise ValueError("invalid segment range")
    _text(segment.get("transcript"))
    _list(segment.get("ocrTexts"), _text)
    _list(segment.get("evidenceFrames"), _text)


def _context(value: Any) -> None:
    context = _object(value)
    source = context.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("video source is required")
    _text(context.get("userGoal"))
    _list(context.get("segments"), _segment)


def _chunk(value: Any) -> None:
    chunk = _object(value)
    start, end = chunk.get("startTime", 0), chunk.get("endTime", 0)
    _integer(start)
    _integer(end)
    if start < 0 or end <= start:
        raise ValueError("invalid chunk range")
    _text(chunk.get("segmentSummary"))
    _list(chunk.get("keywords"), _text)
    _list(chunk.get("rawSegments"), _segment)
    _list(chunk.get("embedding"), _number)


def _number(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("checkpoint number field has the wrong type")


def _chunks(value: Any) -> None:
    _list(value, _chunk)
    if value is None:
        raise ValueError("checkpoint chunks must be a list")


def _plan(value: Any) -> None:
    plan = _object(value)
    _text(plan.get("understoodGoal"))
    _list(plan.get("tasks"), _text)


def _evidence(value: Any) -> None:
    evidence = _object(value)
    timestamp = evidence.get("timestampMs", 0)
    _integer(timestamp)
    if timestamp < 0:
        raise ValueError("evidence timestamp cannot be negative")
    for field in ("source", "content", "claim"):
        _text(evidence.get(field))


def _section(value: Any) -> None:
    section = _object(value)
    _text(section.get("key"))
    _text(section.get("title"))
    _list(section.get("items"), _text)


def _result(value: Any) -> None:
    result = _object(value)
    _text(result.get("title"))
    _list(result.get("conclusions"), _text)
    _list(result.get("evidence"), _evidence)
    _list(result.get("suggestions"), _text)
    _list(result.get("sections"), _section)


def _critique(value: Any) -> None:
    critique = _object(value)
    if not isinstance(critique.get("passed", False), bool):
        raise ValueError("checkpoint critic passed flag has the wrong type")
    for field in ("feedback", "missingRequirements", "unsupportedClaims"):
        _list(critique.get(field), _text)
    _list(critique.get("requiredTimestamps"), _integer)


def _state(value: Any) -> None:
    state = _object(value)
    goal = state.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("agent goal is required")
    round_number = state.get("round", 0)
    _integer(round_number)
    if round_number < 0:
        raise ValueError("agent round cannot be negative")
    if state.get("plan") is not None:
        _plan(state["plan"])
    if state.get("result") is not None:
        _result(state["result"])
    if state.get("critique") is not None:
        _critique(state["critique"])


def _revision(value: Any) -> None:
    revision = _object(value)
    if revision.get("plan") is not None:
        _plan(revision["plan"])
    if not isinstance(revision.get("applied", False), bool):
        raise ValueError("checkpoint revision applied flag has the wrong type")


class AgentCheckpointService:
    def __init__(self, repository: AgentCheckpointRepository, redis_client):
        self.repository = repository
        self.redis = redis_client

    @staticmethod
    def checkpoint_key(media_id: int) -> str:
        return f"agent:checkpoint:{media_id}"

    @classmethod
    def goal_key(cls, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> str:
        return cls.checkpoint_key(media_id) + ":goal:" + goal_digest(goal, mode)

    @classmethod
    def revision_key(cls, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> str:
        return cls.goal_key(media_id, goal, mode) + ":revision"

    @staticmethod
    def media_checkpoint(field: str) -> str:
        return "media:" + field

    @staticmethod
    def goal_checkpoint(goal: str, mode: AnalysisMode, field: str) -> str:
        return f"goal:{goal_digest(goal, mode)}:{field}"

    @staticmethod
    def revision_checkpoint(goal: str, mode: AnalysisMode) -> str:
        return f"revision:{goal_digest(goal, mode)}"

    def _remember_goal_key(self, media_id: int, key: str) -> None:
        index = self.checkpoint_key(media_id) + ":goals"

        def remember():
            try:
                self.redis.sadd(index, key)
                self.redis.expire(index, GOAL_INDEX_TTL)
            except Exception:
                logger.warning("checkpoint_goal_index_failed mediaId=%s key=%s", media_id, key, exc_info=True)

        self.repository.after_commit(remember)

    def load_context(self, media_id: int):
        return self.repository.read(media_id, "media:context", self.checkpoint_key(media_id), "context", _context)

    def save_context(self, media_id: int, context: Any) -> None:
        reusable = {
            "source": _field(context, "source"),
            "userGoal": "",
            "segments": _field(context, "segments", []) or [],
        }
        self.repository.write(media_id, "media:context", "media:stage", self.checkpoint_key(media_id), "context", "CONTEXT_COMPLETED", reusable)
        self.repository.session.commit()

    def load_chunks(self, media_id: int):
        return self.repository.read(media_id, "media:chunks", self.checkpoint_key(media_id), "chunks", _chunks)

    def save_chunks(self, media_id: int, chunks: Any) -> None:
        self.repository.write(media_id, "media:chunks", "media:stage", self.checkpoint_key(media_id), "chunks", "CHUNKS_COMPLETED", list(chunks))
        self.repository.session.commit()

    def load_result(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL):
        return self.repository.read(media_id, self.goal_checkpoint(goal, mode, "result"), self.goal_key(media_id, goal, mode), "result", _state)

    def save_result(self, media_id: int, state: Any, mode: AnalysisMode = AnalysisMode.GENERAL) -> None:
        goal = _field(state, "goal")
        stage = _stage_from_critique(state, "ANALYSIS_COMPLETED", "ANALYSIS_COMPLETED_WITH_WARNINGS")
        key = self.goal_key(media_id, goal, mode)
        self.repository.write(media_id, self.goal_checkpoint(goal, mode, "result"), self.goal_checkpoint(goal, mode, "stage"), key, "result", stage, state)
        self._remember_goal_key(media_id, key)
        self.repository.session.commit()

    def load_plan(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL):
        return self.repository.read(media_id, self.goal_checkpoint(goal, mode, "plan"), self.goal_key(media_id, goal, mode), "plan", _plan)

    def _save_plan(self, media_id: int, goal: str, mode: AnalysisMode, plan: Any) -> None:
        key = self.goal_key(media_id, goal, mode)
        self.repository.write(media_id, self.goal_checkpoint(goal, mode, "plan"), self.goal_checkpoint(goal, mode, "stage"), key, "plan", "PLAN_COMPLETED", plan)
        self._remember_goal_key(media_id, key)

    def save_plan(self, media_id: int, goal: str, mode: AnalysisMode, plan: Any) -> None:
        self._save_plan(media_id, goal, mode, plan)
        self.repository.session.commit()

    def load_critic_state(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL):
        return self.repository.read(media_id, self.goal_checkpoint(goal, mode, "criticState"), self.goal_key(media_id, goal, mode), "criticState", _state)

    def _save_critic_state(self, media_id: int, state: Any, mode: AnalysisMode, stage: str) -> None:
        goal = _field(state, "goal")
        key = self.goal_key(media_id, goal, mode)
        self.repository.write(media_id, self.goal_checkpoint(goal, mode, "criticState"), self.goal_checkpoint(goal, mode, "stage"), key, "criticState", stage, state)
        self._remember_goal_key(media_id, key)
        self.repository.session.commit()

    def save_critic_state(self, media_id: int, state: Any, mode: AnalysisMode = AnalysisMode.GENERAL) -> None:
        self._save_critic_state(media_id, state, mode, _stage_from_critique(state, "CRITIC_PASSED", "CRITIC_RETRY_REQUIRED"))

    def save_execution_state(self, media_id: int, state: Any, mode: AnalysisMode = AnalysisMode.GENERAL) -> None:
        self._save_critic_state(media_id, state, mode, "EXECUTOR_COMPLETED")

    def load_stage(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL):
        return self.repository.read_stage(media_id, self.goal_checkpoint(goal, mode, "stage"), self.goal_key(media_id, goal, mode))

    def save_stage(self, media_id: int, goal: str, mode: AnalysisMode, stage: str) -> None:
        key = self.goal_key(media_id, goal, mode)
        self.repository.write_stage(media_id, self.goal_checkpoint(goal, mode, "stage"), key, stage)
        self._remember_goal_key(media_id, key)
        self.repository.session.commit()

    def stage_revision(self, media_id: int, goal: str, mode: AnalysisMode, plan: Any) -> None:
        key = self.revision_key(media_id, goal, mode)
        self.repository.write_standalone(media_id, self.revision_checkpoint(goal, mode), key, "revision", "REVISION_PENDING", {"plan": plan, "applied": False})
        self._remember_goal_key(media_id, key)
        self.repository.session.commit()

    def begin_staged_revision(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> bool:
        key = self.revision_key(media_id, goal, mode)
        revision = self.repository.read(media_id, self.revision_checkpoint(goal, mode), key, "revision", _revision)
        if revision is None:
            return False
        if revision.get("applied"):
            return True

        goal_key = self.goal_key(media_id, goal, mode)
        self.repository.delete_by_prefix(media_id, self.goal_checkpoint(goal, mode, ""))
        self.repository.after_commit(lambda: self.redis.delete(goal_key), goal_key)
        plan = revision.get("plan")
        if plan is not None:
            self._save_plan(media_id, goal, mode, plan)
        self.repository.write_standalone(media_id, self.revision_checkpoint(goal, mode), key, "revision", "REVISION_APPLIED", {"plan": plan, "applied": True})
        self.repository.session.commit()
        return True

    def complete_staged_revision(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> None:
        self.repository.delete(media_id, self.revision_checkpoint(goal, mode), self.revision_key(media_id, goal, mode))
        self.repository.session.commit()

    def cancel_staged_revision(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> None:
        self.complete_staged_revision(media_id, goal, mode)

    def save_failure(self, media_id: int, goal: str, mode: AnalysisMode, failed_stage: str, error: Exception) -> None:
        key = self.goal_key(media_id, goal, mode)
        self.repository.write_stage(media_id, self.goal_checkpoint(goal, mode, "stage"), key, "FAILED")

        def cache_failure():
            try:
                stage_name = failed_stage.value if isinstance(failed_stage, Enum) else str(failed_stage)
                self.redis.hset(key, mapping={"failedStage": stage_name, "errorType": type(error).__name__})
                self.redis.expire(key, GOAL_INDEX_TTL)
            except Exception:
                logger.warning("checkpoint_failure_cache_write_failed key=%s", key, exc_info=True)

        self.repository.after_commit(cache_failure, key)
        self._remember_goal_key(media_id, key)
        self.repository.session.commit()

    @staticmethod
    def _feedback_key(media_id: int) -> str:
        return f"agent:feedback:{media_id}"

    def save_feedback(self, feedback: dict[str, Any]) -> None:
        media_id = feedback["mediaId"]
        normalized = dict(feedback)
        normalized["goal"] = normalized["goal"].strip() if normalized.get("goal") is not None else None
        normalized["mode"] = AnalysisMode.from_nullable(normalized.get("mode")).value
        for field in ("errorType", "comment", "correctedGoal"):
            if normalized.get(field) is not None:
                normalized[field] = normalized[field].strip()
        normalized["correctedTasks"] = [task.strip() for task in normalized.get("correctedTasks") or [] if task and task.strip()]
        if normalized.get("createdAt") is None:
            normalized["createdAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        elif isinstance(normalized["createdAt"], datetime):
            normalized["createdAt"] = normalized["createdAt"].isoformat().replace("+00:00", "Z")
        key = self._feedback_key(media_id)
        try:
            self.redis.rpush(key, json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
            self.redis.ltrim(key, -MAX_FEEDBACK_SAMPLES, -1)
            self.redis.expire(key, FEEDBACK_TTL)
        except Exception as exc:
            raise RuntimeError("保存 Agent 用户反馈失败") from exc

    def load_feedback(self, media_id: int) -> list[dict[str, Any]]:
        values = self.redis.lrange(self._feedback_key(media_id), 0, -1) or []
        result = []
        for value in values:
            try:
                result.append(json.loads(value))
            except (TypeError, ValueError):
                logger.warning("checkpoint_feedback_deserialize_failed mediaId=%s", media_id)
        return result

    def delete_media(self, media_id: int) -> None:
        self.repository.delete_by_media_id(media_id)
        self.repository.session.commit()
