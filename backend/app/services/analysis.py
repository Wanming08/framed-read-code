"""Application orchestration and content-level context reuse from AiService.java."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.agent.budget import BudgetExceededError
from app.agent.modes import ModeRegistry
from app.schemas.mode import AnalysisMode
from app.schemas.video import VideoContext, VideoSegment
from app.services.analysis_status import _result_markdown
from app.services.redis_coordination import LeaseLock
from app.services.task_keys import context_lock_key, context_owner_key, normalize_content_hash


LOG = logging.getLogger(__name__)
CONTEXT_LOCK_WAIT_SECONDS = 300
CONTEXT_OWNER_TTL_SECONDS = 7 * 24 * 60 * 60
CONTEXT_LOCK_LEASE_MS = 30_000


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _context(value: Any) -> VideoContext:
    return VideoContext.from_dict(value) if isinstance(value, dict) else value


class VideoContextNotReadyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("视频内容尚未解析完成，请先完成一次 Video Agent 分析")


class _ContextBuildLock:
    """A renewable process lock, equivalent to Redisson's watchdog behavior."""

    def __init__(self, redis_client: Any, key: str) -> None:
        self._lease = LeaseLock(redis_client, key, lease_ms=CONTEXT_LOCK_LEASE_MS)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._renewal: threading.Thread | None = None
        self._acquired = False

    def try_lock(self, wait_seconds: int) -> bool:
        deadline = time.monotonic() + wait_seconds
        while True:
            if self._lease.acquire():
                self._acquired = True
                self._renewal = threading.Thread(target=self._renew, daemon=True, name="analysis-context-lock")
                self._renewal.start()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop.wait(min(0.2, remaining))

    def _renew(self) -> None:
        while not self._stop.wait(CONTEXT_LOCK_LEASE_MS / 3000):
            try:
                if not self._lease.renew():
                    self._lost.set()
                    return
            except Exception:
                self._lost.set()
                LOG.exception("context_lock_renewal_failed")
                return

    def ensure_held(self) -> None:
        if not self._acquired or self._lost.is_set():
            raise RuntimeError("视频上下文构建锁已失效，稍后重试")

    def release(self) -> None:
        self._stop.set()
        if self._renewal is not None:
            self._renewal.join(timeout=1)
        if self._acquired:
            try:
                self._lease.release()
            except Exception:
                LOG.warning("context_lock_release_failed", exc_info=True)


class AiService:
    def __init__(
        self, media_repository: Any, video_context: Any, long_context: Any,
        agent_loop: Any, checkpoints: Any, telemetry: Any, media_service: Any,
        events: Any, redis_client: Any, mode_registry: ModeRegistry,
        *, context_lock_factory: Callable[[Any, str], Any] | None = None,
    ) -> None:
        self.media_repository = media_repository
        self.video_context = video_context
        self.long_context = long_context
        self.agent_loop = agent_loop
        self.checkpoints = checkpoints
        self.telemetry = telemetry
        self.media_service = media_service
        self.events = events
        self.redis = redis_client
        self.mode_registry = mode_registry
        self.context_lock_factory = context_lock_factory or _ContextBuildLock

    def async_analyze(self, media_id: int, user_goal: str,
                      mode: AnalysisMode | None = None) -> None:
        resolved_mode = mode or AnalysisMode.GENERAL
        trace_id = self.telemetry.start(media_id, user_goal, resolved_mode)
        self.telemetry.bind(trace_id)
        stage = "VIDEO_CONTEXT"
        media = self.media_repository.get_by_id(media_id)
        if media is None:
            self.telemetry.flush(trace_id)
            self.telemetry.clear()
            raise ValueError(f"media does not exist: {media_id}")
        try:
            state = self.checkpoints.load_result(media_id, user_goal, resolved_mode)
            if state is not None and _field(state, "result") is not None:
                self._persist_result(media, state)
                self.telemetry.increment(trace_id, "checkpointHits", 1)
                return

            context = self._resolve_context(media, user_goal, trace_id, resolved_mode)
            transcript_text = context.transcript_text()
            stage = "AGENT_LOOP"
            self._publish(media_id, user_goal, resolved_mode, "多模态上下文已就绪，Agent 开始分析", stage)
            started = time.monotonic_ns()
            try:
                state = self.agent_loop.run(media_id, context, self.mode_registry.of(resolved_mode))
                self.telemetry.stage(trace_id, stage, started, True)
            except Exception:
                self.telemetry.stage(trace_id, stage, started, False)
                raise
            self._persist_result(media, state, transcript_text)
            LOG.info("agent_analysis_completed traceId=%s mediaId=%s rounds=%s", trace_id, media_id, _field(state, "round"))
        except Exception as error:
            try:
                self.checkpoints.save_failure(media_id, user_goal, resolved_mode, stage, error)
            except Exception as checkpoint_error:
                error.add_note(f"failure checkpoint write failed: {checkpoint_error}")
                LOG.error("agent_failure_checkpoint_write_failed traceId=%s mediaId=%s", trace_id, media_id, exc_info=True)
            LOG.error("agent_analysis_failed traceId=%s mediaId=%s", trace_id, media_id, exc_info=True)
            if isinstance(error, BudgetExceededError):
                raise
            raise RuntimeError("AI analysis failed") from error
        finally:
            self.telemetry.flush(trace_id)
            self.telemetry.clear()

    def _resolve_context(self, media: Any, goal: str, trace_id: str,
                         mode: AnalysisMode) -> VideoContext:
        cached = self.checkpoints.load_context(media.id)
        if cached is not None:
            self.telemetry.increment(trace_id, "contextCheckpointHits", 1)
            context = _context(cached)
            return VideoContext(context.source, goal, context.segments)

        content_hash = normalize_content_hash(media.id, self.media_service.content_hash(media.id))
        reused = self._reuse_content_context(media, goal, trace_id, content_hash)
        if reused is not None:
            return reused

        lock = self.context_lock_factory(self.redis, context_lock_key(content_hash))
        locked = False
        try:
            locked = lock.try_lock(CONTEXT_LOCK_WAIT_SECONDS)
            own = self.checkpoints.load_context(media.id)
            if own is not None:
                self.telemetry.increment(trace_id, "contextCheckpointHits", 1)
                own = _context(own)
                return VideoContext(own.source, goal, own.segments)
            reused = self._reuse_content_context(media, goal, trace_id, content_hash)
            if reused is not None:
                return reused
            if not locked:
                self.telemetry.increment(trace_id, "contextLockContentions", 1)
                LOG.warning("context_build_in_progress mediaId=%s contentHash=%s waitedSeconds=%s", media.id, content_hash, CONTEXT_LOCK_WAIT_SECONDS)
                raise RuntimeError("同一视频的上下文正在构建中，稍后重试")
            return self._build_context(media, goal, trace_id, content_hash, mode, lock)
        finally:
            if locked:
                lock.release()

    def _reuse_content_context(self, media: Any, goal: str, trace_id: str,
                               content_hash: str) -> VideoContext | None:
        owner_id = self._context_owner(content_hash)
        if owner_id is None or owner_id == media.id:
            return None
        owner_context = self.checkpoints.load_context(owner_id)
        if owner_context is None:
            self.redis.delete(context_owner_key(content_hash))
            return None
        localized = self._reusable_context(media.file_path, _context(owner_context))
        self.checkpoints.save_context(media.id, localized)
        self.telemetry.increment(trace_id, "contextContentReuses", 1)
        LOG.info("video_context_reused mediaId=%s sourceMediaId=%s contentHash=%s", media.id, owner_id, content_hash)
        return VideoContext(localized.source, goal, localized.segments)

    def _build_context(self, media: Any, goal: str, trace_id: str,
                       content_hash: str, mode: AnalysisMode, lock: Any) -> VideoContext:
        self._publish(media.id, goal, mode, "正在并行提取语音与关键帧", "VIDEO_CONTEXT")
        started = time.monotonic_ns()
        try:
            context = _context(self.video_context.build(media.file_path, goal, trace_id))
            try:
                if hasattr(lock, "ensure_held"):
                    lock.ensure_held()
                self.checkpoints.save_context(media.id, context)
            except Exception:
                self.video_context.delete_evidence_frames(context)
                raise
            self._remember_context_owner(content_hash, media.id)
            self.telemetry.stage(trace_id, "VIDEO_CONTEXT", started, True)
            return context
        except Exception:
            self.telemetry.stage(trace_id, "VIDEO_CONTEXT", started, False)
            raise

    def _context_owner(self, content_hash: str) -> int | None:
        key = context_owner_key(content_hash)
        try:
            value = self.redis.get(key)
            if value is None:
                return None
            return int(value)
        except ValueError:
            self.redis.delete(key)
            return None
        except Exception:
            LOG.warning("context_owner_read_failed contentHash=%s", content_hash, exc_info=True)
            return None

    def _remember_context_owner(self, content_hash: str, media_id: int) -> None:
        try:
            self.redis.set(context_owner_key(content_hash), str(media_id), ex=CONTEXT_OWNER_TTL_SECONDS)
        except Exception:
            LOG.warning("context_owner_write_failed contentHash=%s mediaId=%s", content_hash, media_id, exc_info=True)

    @staticmethod
    def _reusable_context(target_source: str, source: VideoContext) -> VideoContext:
        return VideoContext(target_source, "", [
            VideoSegment(
                segment.startMs, segment.endMs, segment.transcript, segment.ocrTexts,
                [f"{target_source}#timestampMs={segment.startMs}"] if segment.evidenceFrames else [],
            )
            for segment in source.segments
        ])

    def reuse_result(self, media_id: int, source_media_id: int, state: Any,
                     mode: AnalysisMode | None = None) -> bool:
        media = self.media_repository.get_by_id(media_id)
        if media is None:
            raise ValueError(f"media does not exist: {media_id}")
        source_context = self.checkpoints.load_context(source_media_id)
        if source_context is None:
            return False
        self.checkpoints.save_context(media_id, self._reusable_context(media.file_path, _context(source_context)))
        self.checkpoints.save_result(media_id, state, mode or AnalysisMode.GENERAL)
        self._persist_result(media, state)
        return True

    def follow_up(self, media_id: int, original_goal: str | None, question: str,
                  mode: AnalysisMode | None = None) -> str:
        resolved = mode or AnalysisMode.GENERAL
        context = self.checkpoints.load_context(media_id)
        if context is None:
            raise VideoContextNotReadyError()
        context = _context(context)
        trace_id = self.telemetry.start(media_id, question, resolved)
        self.telemetry.bind(trace_id)
        try:
            previous = None if original_goal is None else self.checkpoints.load_result(media_id, original_goal, resolved)
            goal = self._contextual_question(original_goal, previous, question)
            follow_up_context = VideoContext(context.source, goal, context.segments)
            state = self.agent_loop.run(media_id, follow_up_context, self.mode_registry.of(resolved))
            return _result_markdown(_field(state, "result"))
        finally:
            self.telemetry.flush(trace_id)
            self.telemetry.clear()

    def search_evidence(self, media_id: int, query: str) -> list[Any]:
        context = self.checkpoints.load_context(media_id)
        if context is None:
            raise VideoContextNotReadyError()
        context = _context(context)
        trace_id = self.telemetry.start(media_id, query)
        self.telemetry.bind(trace_id)
        started = time.monotonic_ns()
        try:
            search_context = VideoContext(context.source, query, context.segments)
            hits = self.long_context.search_evidence(media_id, search_context)
            self.telemetry.stage(trace_id, "RETRIEVAL", started, True)
            return hits
        except Exception:
            self.telemetry.stage(trace_id, "RETRIEVAL", started, False)
            raise
        finally:
            self.telemetry.flush(trace_id)
            self.telemetry.clear()

    def stage_revision(self, feedback: Any, mode: AnalysisMode | None = None) -> None:
        resolved = mode or AnalysisMode.GENERAL
        normalized = self._normalized_feedback(feedback, resolved)
        self.checkpoints.save_feedback(normalized)
        goal = normalized.get("correctedGoal") or normalized["goal"]
        tasks = normalized["correctedTasks"]
        plan = {"understoodGoal": goal, "tasks": tasks} if tasks else None
        self.checkpoints.stage_revision(normalized["mediaId"], goal, resolved, plan)

    @classmethod
    def revision_goal(cls, feedback: Any) -> str:
        normalized = cls._normalized_feedback(feedback, AnalysisMode.GENERAL)
        return normalized.get("correctedGoal") or normalized["goal"]

    def cancel_staged_revision(self, media_id: int, goal: str,
                               mode: AnalysisMode | None = None) -> None:
        self.checkpoints.cancel_staged_revision(media_id, goal, mode or AnalysisMode.GENERAL)

    @staticmethod
    def _normalized_feedback(feedback: Any, mode: AnalysisMode) -> dict[str, Any]:
        value = dict(feedback) if isinstance(feedback, dict) else vars(feedback).copy()
        for key in ("goal", "errorType", "comment", "correctedGoal"):
            if value.get(key) is not None:
                value[key] = value[key].strip()
        value["mode"] = mode.value
        value["correctedTasks"] = [
            task.strip() for task in value.get("correctedTasks") or []
            if task is not None and task.strip()
        ]
        return value

    @staticmethod
    def _contextual_question(original_goal: str | None, previous: Any,
                             question: str) -> str:
        if original_goal is None or previous is None or _field(previous, "result") is None:
            return question
        previous_result = _result_markdown(_field(previous, "result"))[:4_000]
        return (
            "这是对同一视频的继续追问。请结合原始视频证据和已有分析回答当前问题。\n"
            f"原始目标：{original_goal}\n已有分析：{previous_result}\n当前追问：{question}\n"
        )

    def _persist_result(self, media: Any, state: Any, transcript_text: str | None = None) -> None:
        result = _field(state, "result")
        if result is None:
            raise RuntimeError("Agent 未生成分析结果")
        media.ai_summary = _result_markdown(result)
        if transcript_text is not None:
            media.transcript_text = transcript_text
        self.media_repository.commit()
        self.media_service.invalidate_user_list(media.user_id)

    def _publish(self, media_id: int, goal: str, mode: AnalysisMode,
                 message: str, stage: str) -> None:
        self.events.publish_analysis(media_id, goal, mode, {
            "state": "PROCESSING", "result": None, "message": message,
        }, stage)
