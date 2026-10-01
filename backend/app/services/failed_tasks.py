"""Failure ledger and administrator replay from FailedAnalysisTaskService.java."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FailedAnalysisTask
from app.schemas.mode import AnalysisMode
from app.services.analysis_status import TaskStage, TaskState, TaskStatus
from app.services.task_keys import active_key, attempts_key, goal_digest, normalize_content_hash


LOG = logging.getLogger(__name__)
ACTIVE_TTL_SECONDS = 6 * 3600
_BEARER_SECRET = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_NAMED_SECRET = re.compile(r"(?i)((?:api[-_ ]?key|token|secret)\s*[=:]\s*)[^\s,;]{8,}")
_PREFIXED_SECRET = re.compile(r"(?i)sk-[A-Za-z0-9_-]{16,}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _column(value: str | None, fallback: str, max_length: int) -> str:
    return (value if value and value.strip() else fallback)[:max_length]


def _sanitize_error(value: str | None) -> str | None:
    if value is None:
        return None
    value = _BEARER_SECRET.sub(r"\1****", value)
    value = _NAMED_SECRET.sub(r"\1****", value)
    return _PREFIXED_SECRET.sub("****", value)[:1000]


def _root_cause(error: BaseException) -> BaseException:
    seen = set()
    while id(error) not in seen:
        seen.add(id(error))
        next_error = error.__cause__
        if next_error is None:
            break
        error = next_error
    return error


class FailedTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, row: FailedAnalysisTask) -> None:
        self.session.add(row)

    def commit(self) -> None:
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def get(self, task_id: int) -> FailedAnalysisTask | None:
        return self.session.get(FailedAnalysisTask, task_id)

    def latest(self, limit: int = 100) -> list[FailedAnalysisTask]:
        return list(self.session.scalars(select(FailedAnalysisTask).order_by(FailedAnalysisTask.id.desc()).limit(limit)))

    def update(self, row: FailedAnalysisTask) -> None:
        self.session.add(row)
        self.commit()


class FailedAnalysisTaskService:
    def __init__(self, repository: Any, producer: Any, redis_client: Any, events: Any,
                 *, topic: str = "video-analysis-topic") -> None:
        self.repository = repository
        self.producer = producer
        self.redis = redis_client
        self.events = events
        self.topic = topic

    def record(self, message: Any, attempts: int, error: BaseException) -> FailedAnalysisTask:
        root = _root_cause(error)
        row = FailedAnalysisTask(
            media_id=_field(message, "mediaId", None) if _field(message, "mediaId", None) is not None else -1,
            action=_column(_field(message, "action"), "UNKNOWN", 32),
            mode=AnalysisMode.from_nullable(_field(message, "mode")).value,
            content_hash=_column(_field(message, "contentHash"), "unknown", 128),
            user_goal=_column(_field(message, "userGoal"), "(消息缺少分析目标)", 500),
            attempt_count=int(attempts),
            error_type=_column(type(root).__name__, "UnknownError", 128),
            error_message=_sanitize_error(str(root) if root.args else None),
            status="FAILED", created_at=datetime.now(), updated_at=datetime.now(),
        )
        self.repository.add(row)
        self.repository.commit()
        return row

    def latest(self) -> list[FailedAnalysisTask]:
        return self.repository.latest(100)

    def replay(self, task_id: int) -> None:
        row = self.repository.get(task_id)
        if row is None:
            raise LookupError("失败任务不存在")
        if row.status != "FAILED":
            raise ValueError("该失败任务已经重放")
        if row.media_id is None or row.media_id == -1 or row.action not in ("START_ANALYSIS", "REVISE_ANALYSIS"):
            raise ValueError("该记录来自非法任务消息，缺少可重放的原始参数")
        mode = AnalysisMode.from_nullable(row.mode)
        content_hash = normalize_content_hash(row.media_id, row.content_hash)
        digest = goal_digest(row.user_goal, mode)
        key = active_key(content_hash, digest)
        if not self.redis.set(key, str(row.media_id), nx=True, ex=ACTIVE_TTL_SECONDS):
            raise ValueError("相同任务正在处理中")
        dispatched = False
        try:
            self.redis.delete(attempts_key(content_hash, digest))
            self.producer.send_task({"mediaId": row.media_id, "action": row.action,
                                     "contentHash": content_hash, "userGoal": row.user_goal,
                                     "mode": mode.value}, topic=self.topic)
            dispatched = True
            row.status = "REQUEUED"
            row.updated_at = datetime.now()
            self.repository.update(row)
        except Exception:
            if not dispatched:
                self.redis.delete(key)
            else:
                LOG.exception("failed_analysis_replay_bookkeeping_failed taskId=%s", task_id)
            raise
        try:
            self.events.publish_analysis(row.media_id, row.user_goal, mode,
                                         TaskStatus.of(TaskState.QUEUED, "失败任务已由管理员重新入队"),
                                         TaskStage.MANUAL_REPLAY)
        except Exception:
            LOG.warning("failed_analysis_replay_event_failed taskId=%s", task_id, exc_info=True)
