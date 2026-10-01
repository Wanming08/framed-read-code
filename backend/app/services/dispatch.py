"""Admission and RocketMQ submission from AnalysisDispatchService.java."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from app.schemas.mode import AnalysisMode
from app.services.analysis_status import TaskStage, TaskState, TaskStatus
from app.services.task_keys import active_key, goal_digest, normalize_content_hash


LOG = logging.getLogger(__name__)
ACTIVE_TTL_SECONDS = 6 * 3600
USER_REQUESTS_PER_MINUTE = 5
GLOBAL_REQUESTS_PER_MINUTE = 30


class SubmissionResult(str, Enum):
    ACCEPTED = "ACCEPTED"
    RATE_LIMITED = "RATE_LIMITED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


class AnalysisDispatchService:
    def __init__(self, redis_client: Any, limiter: Any, producer: Any, media_service: Any,
                 checkpoints: Any, events: Any, *, topic: str = "video-analysis-topic") -> None:
        self.redis = redis_client
        self.limiter = limiter
        self.producer = producer
        self.media_service = media_service
        self.checkpoints = checkpoints
        self.events = events
        self.topic = topic

    def submit(self, media: Any, goal: str, revision: Any = None,
               mode: AnalysisMode | None = AnalysisMode.GENERAL) -> SubmissionResult:
        resolved = mode or AnalysisMode.GENERAL
        media_id, user_id = _field(media, "id"), _field(media, "user_id")
        action = "REVISE_ANALYSIS" if revision is not None else "START_ANALYSIS"
        content_hash = (f"media-{media_id}" if revision is not None else
                        normalize_content_hash(media_id, self.media_service.content_hash(media_id)))
        key = active_key(content_hash, goal_digest(goal, resolved))
        if not self.redis.set(key, str(media_id), nx=True, ex=ACTIVE_TTL_SECONDS):
            return SubmissionResult.DUPLICATE
        try:
            if not self.limiter.try_acquire_pair(
                f"limit:ai:user:{user_id}", USER_REQUESTS_PER_MINUTE,
                "limit:ai:global", GLOBAL_REQUESTS_PER_MINUTE,
            ):
                self.redis.delete(key)
                return SubmissionResult.RATE_LIMITED
            if revision is not None:
                self._stage_revision(media_id, goal, revision, resolved)
            self.producer.send_task({"mediaId": media_id, "action": action,
                                     "contentHash": content_hash, "userGoal": goal,
                                     "mode": resolved.value}, topic=self.topic)
        except Exception:
            self.redis.delete(key)
            if revision is not None:
                try:
                    self.checkpoints.cancel_staged_revision(media_id, goal, resolved)
                except Exception:
                    LOG.exception("analysis_revision_rollback_failed mediaId=%s", media_id)
            LOG.exception("analysis_dispatch_failed mediaId=%s userId=%s", media_id, user_id)
            return SubmissionResult.FAILED
        try:
            self.events.publish_analysis(media_id, goal, resolved,
                                         TaskStatus.of(TaskState.QUEUED, "任务已进入异步分析队列"),
                                         TaskStage.QUEUED)
        except Exception:
            LOG.warning("analysis_queued_event_failed mediaId=%s", media_id, exc_info=True)
        return SubmissionResult.ACCEPTED

    def _stage_revision(self, media_id: int, goal: str, revision: Any, mode: AnalysisMode) -> None:
        value = dict(revision) if isinstance(revision, dict) else vars(revision).copy()
        value["mediaId"] = media_id
        value["mode"] = mode.value
        value["goal"] = _field(revision, "goal", goal)
        self.checkpoints.save_feedback(value)
        tasks = [task.strip() for task in (_field(revision, "correctedTasks", []) or [])
                 if task and task.strip()]
        plan = {"understoodGoal": goal, "tasks": tasks} if tasks else None
        self.checkpoints.stage_revision(media_id, goal, mode, plan)

    def is_active(self, media_id: int, goal: str,
                  mode: AnalysisMode = AnalysisMode.GENERAL) -> bool:
        digest = goal_digest(goal, mode)
        content_hash = normalize_content_hash(media_id, self.media_service.content_hash(media_id))
        return bool(self.redis.exists(active_key(content_hash, digest))) or bool(
            self.redis.exists(active_key(f"media-{media_id}", digest)))

    def require_ai_quota(self, user_id: int) -> None:
        from app.api.errors import BusinessError, ErrorCode

        try:
            allowed = self.limiter.try_acquire_pair(
                f"limit:ai:user:{user_id}", USER_REQUESTS_PER_MINUTE,
                "limit:ai:global", GLOBAL_REQUESTS_PER_MINUTE,
            )
        except Exception as exc:
            raise BusinessError(ErrorCode.SERVICE_UNAVAILABLE, "AI 服务限流器暂不可用，请稍后再试") from exc
        if not allowed:
            raise BusinessError(ErrorCode.RATE_LIMITED, "AI 请求过于频繁，请稍后再试")
