"""Independent video transcription queue and Redis state from the Java service."""

from __future__ import annotations

import logging

from app.services.analysis_status import TaskStage, TaskState, TaskStatus


LOG = logging.getLogger(__name__)
ACTIVE_TTL = 2 * 60 * 60
COMPLETED_TTL = 7 * 24 * 60 * 60
FAILED_TTL = 60 * 60
REJECTED_TTL = 10 * 60


class TranscriptionTaskService:
    def __init__(self, repository_factory, redis_client, media_service, transcriber, events, executor):
        self.repository_factory = repository_factory
        self.redis = redis_client
        self.media = media_service
        self.transcriber = transcriber
        self.events = events
        self.executor = executor

    def queue(self, media_id: int) -> bool:
        accepted = self.redis.set(self._active_key(media_id), "1", nx=True, ex=ACTIVE_TTL)
        if not accepted:
            return False
        self._set_state(media_id, TaskState.QUEUED, ACTIVE_TTL)
        self.events.publish_transcription(
            media_id, TaskStatus.of(TaskState.QUEUED, "文字提取任务已排队"), TaskStage.QUEUED,
        )
        return True

    def dispatch(self, media_id: int):
        return self.executor.submit(self.transcribe, media_id)

    def transcribe(self, media_id: int) -> None:
        repository = self.repository_factory()
        try:
            media_file = repository.get_by_id(media_id)
            if media_file is None:
                return
            try:
                self._set_state(media_id, TaskState.PROCESSING, ACTIVE_TTL)
                self.events.publish_transcription(
                    media_id, TaskStatus.of(TaskState.PROCESSING, "正在识别视频语音"), TaskStage.ASR,
                )
                readable = self._readable_source(media_file.file_path)
                media_file.transcript_text = self.transcriber.transcribe_to_text(readable)
                repository.commit()
                self.media.invalidate_user_list(media_file.user_id)
                self._set_state(media_id, TaskState.COMPLETED, COMPLETED_TTL)
                self.events.publish_transcription(
                    media_id, TaskStatus(TaskState.COMPLETED, media_file.transcript_text, "任务完成"), TaskStage.COMPLETED,
                )
                LOG.info("transcription_completed media_id=%s", media_id)
            except Exception:
                repository.rollback()
                self._set_state(media_id, TaskState.FAILED, FAILED_TTL)
                self.events.publish_transcription(
                    media_id, TaskStatus.of(TaskState.FAILED, "文字提取失败，请稍后重试"), TaskStage.FAILED,
                )
                LOG.exception("transcription_failed media_id=%s", media_id)
        finally:
            self._clear_active(media_id)
            close = getattr(repository, "close", None)
            if close is not None:
                close()

    def reject_queued(self, media_id: int) -> None:
        self._clear_active(media_id)
        self._set_state(media_id, TaskState.FAILED, REJECTED_TTL)
        self.events.publish_transcription(
            media_id, TaskStatus.of(TaskState.FAILED, "任务队列已满，请稍后重试"), TaskStage.DISPATCH_FAILED,
        )

    def status(self, media_file) -> TaskStatus:
        if media_file.transcript_text and media_file.transcript_text.strip():
            return TaskStatus(TaskState.COMPLETED, media_file.transcript_text, "任务完成")
        state_value = self.redis.get(self._state_key(media_file.id))
        if state_value is None:
            state = TaskState.PROCESSING if self.redis.exists(self._active_key(media_file.id)) else TaskState.NOT_STARTED
            message = "正在提取文字" if state is TaskState.PROCESSING else "尚未提交文字提取任务"
            return TaskStatus.of(state, message)
        if isinstance(state_value, bytes):
            state_value = state_value.decode("utf-8")
        try:
            state = TaskState(state_value)
        except ValueError:
            LOG.warning("invalid_transcription_state media_id=%s state=%s", media_file.id, state_value)
            return TaskStatus.of(TaskState.NOT_STARTED, "任务状态不可用")
        if state is TaskState.COMPLETED:
            return TaskStatus(TaskState.COMPLETED, media_file.transcript_text, "任务完成")
        return TaskStatus.of(state, {
            TaskState.FAILED: "文字提取失败，请稍后重试",
            TaskState.QUEUED: "文字提取任务已排队",
            TaskState.PROCESSING: "正在提取文字",
            TaskState.NOT_STARTED: "尚未提交文字提取任务",
        }[state])

    def _readable_source(self, source: str) -> str:
        storage = getattr(self.media, "storage", None)
        if storage is not None and hasattr(storage, "processing_source"):
            return storage.processing_source(source)
        if hasattr(self.media, "readable_source"):
            return self.media.readable_source(source)
        return storage.readable_source(source)

    def _set_state(self, media_id: int, state: TaskState, ttl: int) -> None:
        try:
            self.redis.set(self._state_key(media_id), state.value, ex=ttl)
        except Exception:
            LOG.warning("transcription_state_write_failed media_id=%s state=%s", media_id, state.value, exc_info=True)

    def _clear_active(self, media_id: int) -> None:
        try:
            self.redis.delete(self._active_key(media_id))
        except Exception:
            LOG.warning("transcription_active_cleanup_failed media_id=%s", media_id, exc_info=True)

    @staticmethod
    def _active_key(media_id: int) -> str:
        return f"transcription:active:{media_id}"

    @staticmethod
    def _state_key(media_id: int) -> str:
        return f"transcription:state:{media_id}"
