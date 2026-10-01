"""Administrator failure list and manual replay from AdminController.java."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.analysis_status import get_task_event_service
from app.api.dependencies import get_admin_user, get_redis, get_session
from app.api.errors import BusinessError, ErrorCode
from app.api.responses import ok
from app.integrations.rocketmq import RocketMQProducer
from app.services.failed_tasks import FailedAnalysisTaskService, FailedTaskRepository
from app.services.task_events import TaskEventService


router = APIRouter(prefix="/admin/failed-analysis")


class _OnDemandProducer:
    """Keep the read-only list endpoint available when RocketMQ is down."""

    def __init__(self, endpoints: str, topics: tuple[str, str]) -> None:
        self.endpoints = endpoints
        self.topics = topics
        self._producer: RocketMQProducer | None = None

    def send_task(self, payload: dict[str, Any], *, topic: str | None = None) -> Any:
        if self._producer is None:
            producer = RocketMQProducer(self.endpoints, topics=self.topics)
            producer.startup()
            self._producer = producer
        return self._producer.send_task(payload, topic=topic)

    def close(self) -> None:
        if self._producer is not None:
            self._producer.shutdown()


def get_failed_task_service(
    request: Request,
    session: Session = Depends(get_session),
    redis_client: Any = Depends(get_redis),
    events: TaskEventService = Depends(get_task_event_service),
) -> Iterator[FailedAnalysisTaskService]:
    settings = request.app.state.settings
    producer = _OnDemandProducer(settings.rocketmq_endpoints,
                                 (settings.rocketmq_analysis_topic,
                                  settings.rocketmq_analysis_dead_topic))
    try:
        yield FailedAnalysisTaskService(FailedTaskRepository(session), producer,
                                        redis_client, events,
                                        topic=settings.rocketmq_analysis_topic)
    finally:
        producer.close()


def _view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "mediaId": row.media_id,
        "action": row.action,
        "mode": row.mode,
        "userGoal": row.user_goal,
        "attemptCount": row.attempt_count,
        "errorType": row.error_type,
        "errorMessage": row.error_message,
        "status": row.status,
        "createdAt": row.created_at.isoformat(timespec="milliseconds") if row.created_at else None,
        "updatedAt": row.updated_at.isoformat(timespec="milliseconds") if row.updated_at else None,
    }


@router.get("")
def latest(
    _admin_user: int = Depends(get_admin_user),
    failed_tasks: FailedAnalysisTaskService = Depends(get_failed_task_service),
) -> dict[str, Any]:
    return ok([_view(row) for row in failed_tasks.latest()])


@router.post("/{id}/replay", status_code=202)
def replay(
    id: int,
    _admin_user: int = Depends(get_admin_user),
    failed_tasks: FailedAnalysisTaskService = Depends(get_failed_task_service),
) -> dict[str, Any]:
    try:
        failed_tasks.replay(id)
    except LookupError as exc:
        raise BusinessError(ErrorCode.NOT_FOUND, str(exc)) from exc
    return ok()
