"""Redis Pub/Sub task events and SSE subscribers from TaskEventService.java."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.schemas.mode import AnalysisMode
from app.services.analysis_status import TaskStage, TaskStatus
from app.services.task_keys import goal_digest


LOG = logging.getLogger(__name__)
REDIS_CHANNEL = "dovideo:task-events"
STREAM_TIMEOUT_SECONDS = 30 * 60
ANALYSIS = "analysis"
TRANSCRIPTION = "transcription"


async def _close_async(resource: Any) -> None:
    close = getattr(resource, "aclose", None) or resource.close
    await close()


@dataclass(frozen=True)
class TaskEvent:
    state: str
    result: str | None
    message: str
    stage: str | None

    @classmethod
    def of(cls, status: TaskStatus | dict[str, Any], stage: TaskStage | str | None) -> "TaskEvent":
        value = status.to_dict() if isinstance(status, TaskStatus) else status
        return cls(value["state"], value.get("result"), value["message"], stage.value if isinstance(stage, TaskStage) else stage)

    def terminal(self) -> bool:
        return self.state in ("COMPLETED", "FAILED")

    def to_dict(self) -> dict[str, str | None]:
        return {"state": self.state, "result": self.result, "message": self.message, "stage": self.stage}


class TaskEventService:
    def __init__(self, redis_publisher: Any, redis_subscriber: Any) -> None:
        self.redis_publisher = redis_publisher
        self.redis_subscriber = redis_subscriber
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent]]] = {}
        self._listener: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @staticmethod
    def key(media_id: int, task_type: str, goal: str, mode: AnalysisMode | str = AnalysisMode.GENERAL) -> str:
        suffix = goal_digest(goal, AnalysisMode.from_nullable(mode)) if task_type == ANALYSIS else "default"
        return f"{task_type}:{media_id}:{suffix}"

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("task event service is closed")
        if self._listener is None:
            self._loop = asyncio.get_running_loop()
            self._ready = asyncio.Event()
            self._listener = asyncio.create_task(self._listen(), name="dovideo-task-event-listener")
        assert self._ready is not None
        await self._ready.wait()

    async def close(self) -> None:
        self._closed = True
        if self._listener is not None:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None
        await _close_async(self.redis_subscriber)
        self._subscribers.clear()

    async def subscribe(
        self,
        media_id: int,
        task_type: str,
        goal: str,
        mode: AnalysisMode | str,
        initial_status: TaskStatus | dict[str, Any],
        stage: TaskStage | str | None,
    ) -> AsyncIterator[TaskEvent]:
        await self.start()
        key = self.key(media_id, task_type, goal, mode)
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=128)
        self._subscribers.setdefault(key, set()).add(queue)
        try:
            initial = TaskEvent.of(initial_status, stage)
            yield initial
            if initial.terminal():
                return
            expires_at = time.monotonic() + STREAM_TIMEOUT_SECONDS
            while True:
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    return
                yield event
                if event.terminal():
                    return
        finally:
            queues = self._subscribers.get(key)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._subscribers.pop(key, None)

    def publish_analysis(
        self, media_id: int, goal: str, mode: AnalysisMode | str,
        status: TaskStatus | dict[str, Any], stage: TaskStage | str | None,
    ) -> None:
        self._publish(self.key(media_id, ANALYSIS, goal, mode), TaskEvent.of(status, stage))

    def publish_transcription(
        self, media_id: int, status: TaskStatus | dict[str, Any], stage: TaskStage | str | None,
    ) -> None:
        self._publish(self.key(media_id, TRANSCRIPTION, "", AnalysisMode.GENERAL), TaskEvent.of(status, stage))

    def _publish(self, key: str, event: TaskEvent) -> None:
        payload = json.dumps({"key": key, "event": event.to_dict()}, ensure_ascii=False, separators=(",", ":"))
        try:
            receivers = self.redis_publisher.publish(REDIS_CHANNEL, payload)
            if not receivers:
                self._publish_local_threadsafe(key, event)
        except Exception:
            LOG.warning("task_event_redis_publish_failed key=%s", key, exc_info=True)
            self._publish_local_threadsafe(key, event)

    def _publish_local_threadsafe(self, key: str, event: TaskEvent) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._publish_local, key, event)

    def _publish_local(self, key: str, event: TaskEvent) -> None:
        for queue in tuple(self._subscribers.get(key, ())):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    async def _listen(self) -> None:
        assert self._ready is not None
        while not self._closed:
            pubsub = None
            try:
                pubsub = self.redis_subscriber.pubsub()
                await pubsub.subscribe(REDIS_CHANNEL)
                self._ready.set()
                while not self._closed:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    if not message or message.get("type") != "message":
                        continue
                    try:
                        raw = message["data"]
                        value = json.loads(raw)
                        event = value["event"]
                        self._publish_local(value["key"], TaskEvent(**event))
                    except (ValueError, TypeError, KeyError):
                        LOG.warning("task_event_redis_message_invalid", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ready.set()
                LOG.warning("task_event_redis_listener_failed", exc_info=True)
                await asyncio.sleep(1)
            finally:
                if pubsub is not None:
                    try:
                        await _close_async(pubsub)
                    except Exception:
                        LOG.warning("task_event_pubsub_close_failed", exc_info=True)
