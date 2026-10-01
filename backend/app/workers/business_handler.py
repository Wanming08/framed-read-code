"""Analysis task decisions ported from VideoAnalysisConsumer.java.

The transport owns ACK. This handler returns ACK only after terminal work and
returns/raises without ACK for temporary failures. The owned Redis lock remains
held through the ACK RPC, then ``finalize`` releases it.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any, Callable

from app.agent.budget import BudgetExceededError
from app.integrations.rocketmq import decode_task
from app.schemas.mode import AnalysisMode
from app.services.analysis_status import TaskStage, TaskState, TaskStatus
from app.services.redis_coordination import LeaseLock
from app.services.task_keys import active_key, attempts_key, completed_key, goal_digest, lock_key, normalize_content_hash
from app.workers.analysis_consumer import Disposition


LOG = logging.getLogger(__name__)
ACTIVE_TTL_SECONDS = 6 * 3600
COMPLETED_TTL_SECONDS = 7 * 86400
MAX_DELIVERY_ATTEMPTS = 3


def is_permanent_failure(error: BaseException) -> bool:
    seen: set[int] = set()
    for _ in range(16):
        if isinstance(error, (ValueError, PermissionError, LookupError)):
            return True
        if id(error) in seen or error.__cause__ is None:
            break
        seen.add(id(error))
        error = error.__cause__
    return False


class AnalysisBusinessHandler:
    def __init__(self, redis_client: Any, producer: Any, checkpoints: Any,
                 failed_tasks: Any, events: Any, *, media_exists: Callable[[int], bool],
                 purge_media: Callable[[int], None], analyze: Callable[..., None],
                 reuse_result: Callable[..., bool],
                 lock_factory: Callable[[str, int], Any] | None = None,
                 dead_topic: str = "video-analysis-dead-topic",
                 lock_ttl_seconds: int = 90) -> None:
        self.redis = redis_client
        self.producer = producer
        self.checkpoints = checkpoints
        self.failed_tasks = failed_tasks
        self.events = events
        self.media_exists = media_exists
        self.purge_media = purge_media
        self.analyze = analyze
        self.reuse_result = reuse_result
        self.lock_factory = lock_factory or (
            lambda key, ttl: LeaseLock(redis_client, key, lease_ms=ttl * 1000)
        )
        self.dead_topic = dead_topic
        self.lock_ttl_seconds = lock_ttl_seconds
        self._leases: dict[int, Any] = {}
        self._leases_lock = threading.Lock()

    def renew_lock(self, message: Any, ttl_seconds: int) -> bool:
        with self._leases_lock:
            lock = self._leases.get(id(message))
        # During a receive/lock conflict, no business lock has been obtained.
        if lock is None:
            return True
        if ttl_seconds != self.lock_ttl_seconds:
            raise ValueError("lock renewal TTL differs from acquisition TTL")
        return bool(lock.renew())

    def finalize(self, message: Any) -> None:
        with self._leases_lock:
            lock = self._leases.pop(id(message), None)
        if lock is not None:
            try:
                lock.release()
            except Exception:
                LOG.warning("analysis_lock_release_failed", exc_info=True)

    def __call__(self, message: Any) -> Disposition:
        try:
            payload = decode_task(message)
        except ValueError as error:
            # Preserve the undecodable bytes in the manual failure topic.
            payload = {"rawBodyBase64": base64.b64encode(message.body).decode("ascii")}
            return self._poison(payload, str(error))
        reason = self._rejection_reason(payload)
        if reason is not None:
            return self._poison(payload, reason)

        media_id = payload["mediaId"]
        goal = payload["userGoal"]
        mode = AnalysisMode.from_nullable(payload.get("mode"))
        content_hash = normalize_content_hash(media_id, payload.get("contentHash"))
        digest = goal_digest(goal, mode)
        task_lock = self.lock_factory(lock_key(content_hash, digest), self.lock_ttl_seconds)
        active = active_key(content_hash, digest)
        completed = completed_key(content_hash, digest)
        attempts = attempts_key(content_hash, digest)
        acquired = False
        retrying = False
        attempt = 0
        try:
            acquired = bool(task_lock.acquire())
            if not acquired:
                LOG.info("video_analysis_skipped mediaId=%s acquired=False", media_id)
                return Disposition.ACK
            with self._leases_lock:
                self._leases[id(message)] = task_lock
            if not self.media_exists(media_id):
                LOG.info("video_analysis_discarded_deleted_media mediaId=%s", media_id)
                return Disposition.ACK
            attempt = int(self.redis.incr(attempts))
            self.redis.expire(attempts, ACTIVE_TTL_SECONDS)
            self.events.publish_analysis(media_id, goal, mode,
                TaskStatus.of(TaskState.PROCESSING, "视频分析任务开始执行"), TaskStage.CONSUMING)
            if payload["action"] == "REVISE_ANALYSIS":
                if not self.checkpoints.begin_staged_revision(media_id, goal, mode):
                    raise RuntimeError("修订任务状态不存在，等待消息队列重试")
                self.redis.delete(completed)
            else:
                source_value = self.redis.get(completed)
                if source_value is not None:
                    try:
                        source_id = int(source_value)
                    except (TypeError, ValueError):
                        source_id = None
                        self.redis.delete(completed)
                    reusable = self.checkpoints.load_result(source_id, goal, mode) if source_id is not None else None
                    if reusable is not None and reusable.get("result") is not None and self.reuse_result(
                        media_id, source_id, reusable, mode
                    ):
                        self.events.publish_analysis(media_id, goal, mode,
                            TaskStatus.completed(reusable), TaskStage.COMPLETED_REUSED)
                        return Disposition.ACK
                    self.redis.delete(completed)
            self._save_stage(media_id, goal, mode, TaskStage.CONSUMING)
            self.analyze(media_id, goal, mode)
            if payload["action"] == "REVISE_ANALYSIS":
                self.checkpoints.complete_staged_revision(media_id, goal, mode)
            if not self.media_exists(media_id):
                self.purge_media(media_id)
                return Disposition.ACK
            self.redis.set(completed, str(media_id), ex=COMPLETED_TTL_SECONDS)
            result = self.checkpoints.load_result(media_id, goal, mode)
            if result is not None and result.get("result") is not None:
                self.events.publish_analysis(media_id, goal, mode,
                    TaskStatus.completed(result), TaskStage.COMPLETED)
            return Disposition.ACK
        except BudgetExceededError as error:
            self._save_stage(media_id, goal, mode, TaskStage.BUDGET_EXHAUSTED)
            self.events.publish_analysis(media_id, goal, mode,
                TaskStatus.of(TaskState.FAILED, str(error)), TaskStage.BUDGET_EXHAUSTED)
            return Disposition.ACK
        except Exception as error:
            permanent = is_permanent_failure(error)
            if not permanent and acquired and 0 < attempt < MAX_DELIVERY_ATTEMPTS:
                retrying = True
                self.redis.expire(active, ACTIVE_TTL_SECONDS)
                self._save_stage(media_id, goal, mode, TaskStage.RETRYING)
                self.events.publish_analysis(media_id, goal, mode,
                    TaskStatus.of(TaskState.PROCESSING, "本次执行失败，等待消息队列重试"),
                    TaskStage.RETRYING)
                LOG.warning("video_analysis_retry_scheduled mediaId=%s attempt=%s", media_id, attempt)
                raise RuntimeError("temporary analysis failure") from error
            if acquired and (permanent or attempt >= MAX_DELIVERY_ATTEMPTS):
                try:
                    try:
                        self.failed_tasks.record(payload, attempt, error)
                    except Exception:
                        LOG.error("failed_analysis_record_write_failed mediaId=%s", media_id, exc_info=True)
                    self.producer.send_task(payload, topic=self.dead_topic)
                    self._save_stage(media_id, goal, mode, TaskStage.DEAD_LETTERED)
                    self.events.publish_analysis(media_id, goal, mode,
                        TaskStatus.of(TaskState.FAILED, "分析失败，已进入人工处理队列"),
                        TaskStage.DEAD_LETTERED)
                    return Disposition.ACK
                except Exception:
                    retrying = True
                    LOG.error("video_analysis_dead_letter_dispatch_failed mediaId=%s", media_id, exc_info=True)
                    raise
            raise
        finally:
            if acquired and not retrying:
                self.redis.delete(active, attempts)

    @staticmethod
    def _rejection_reason(payload: dict[str, Any]) -> str | None:
        if payload.get("mediaId") is None:
            return "缺少 mediaId"
        if not isinstance(payload.get("mediaId"), int):
            return "mediaId 类型错误"
        if not isinstance(payload.get("userGoal"), str) or not payload["userGoal"].strip():
            return "缺少分析目标"
        if payload.get("action") not in ("START_ANALYSIS", "REVISE_ANALYSIS"):
            return "不支持的 action"
        return None

    def _poison(self, payload: dict[str, Any], reason: str) -> Disposition:
        LOG.error("video_analysis_poison_message reason=%s", reason)
        recorded = dead_lettered = False
        try:
            self.failed_tasks.record(payload, 0, ValueError("invalid video analysis message: " + reason))
            recorded = True
        except Exception:
            LOG.error("poison_message_record_failed", exc_info=True)
        try:
            self.producer.send_task(payload, topic=self.dead_topic)
            dead_lettered = True
        except Exception:
            LOG.error("poison_message_dead_letter_failed", exc_info=True)
        if not recorded and not dead_lettered:
            raise RuntimeError("毒消息无法收敛：失败台账与失败主题均不可用")
        media_id, goal = payload.get("mediaId"), payload.get("userGoal")
        if isinstance(media_id, int) and isinstance(goal, str) and goal.strip():
            try:
                mode = AnalysisMode.from_nullable(payload.get("mode"))
                content_hash = normalize_content_hash(media_id, payload.get("contentHash"))
                digest = goal_digest(goal, mode)
                self.redis.delete(active_key(content_hash, digest), attempts_key(content_hash, digest))
                self._save_stage(media_id, goal, mode, TaskStage.DEAD_LETTERED)
                self.events.publish_analysis(media_id, goal, mode,
                    TaskStatus.of(TaskState.FAILED, "任务消息非法，已终止"), TaskStage.DEAD_LETTERED)
            except Exception:
                LOG.warning("poison_message_state_release_failed mediaId=%s", media_id, exc_info=True)
        return Disposition.ACK

    def _save_stage(self, media_id: int, goal: str, mode: AnalysisMode, stage: TaskStage) -> None:
        try:
            self.checkpoints.save_stage(media_id, goal, mode, stage.value)
        except Exception:
            LOG.warning("analysis_stage_checkpoint_failed mediaId=%s stage=%s", media_id, stage.value,
                        exc_info=True)
