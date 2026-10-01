"""Analysis status JSON and named SSE stream from AnalysisController.java."""

from __future__ import annotations

import json
from threading import Lock

import redis.asyncio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_redis, get_session
from app.api.media import get_media_service
from app.api.responses import ok
from app.schemas.mode import AnalysisMode
from app.services.analysis_status import AnalysisStatusService, DatabaseCheckpointReader, RedisActiveReader
from app.services.media import MediaService
from app.services.task_events import ANALYSIS, TaskEventService


router = APIRouter(prefix="/analysis")
_EVENT_LOCK = Lock()


def get_status_user(user_id: int = Depends(get_current_user)) -> int:
    return user_id


def get_media_for_status(media: MediaService = Depends(get_media_service)) -> MediaService:
    return media


def get_status_service(
    session: Session = Depends(get_session),
    redis_client=Depends(get_redis),
    media: MediaService = Depends(get_media_for_status),
) -> AnalysisStatusService:
    return AnalysisStatusService(DatabaseCheckpointReader(session), RedisActiveReader(redis_client, media))


def get_task_event_service(request: Request, redis_client=Depends(get_redis)) -> TaskEventService:
    if not hasattr(request.app.state, "task_event_service"):
        with _EVENT_LOCK:
            if not hasattr(request.app.state, "task_event_service"):
                settings = request.app.state.settings
                subscriber = redis.asyncio.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    db=settings.redis_database,
                    password=settings.redis_password or None,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                    decode_responses=True,
                )
                request.app.state.task_event_service = TaskEventService(redis_client, subscriber)
    return request.app.state.task_event_service


def _normalized_goal(goal: str) -> str:
    blank = not goal or all(char.isspace() and char not in "\u00a0\u2007\u202f" for char in goal)
    if blank or len(goal.encode("utf-16-le", "surrogatepass")) // 2 > 500:
        raise ValueError("分析目标不能为空且不能超过 500 字")
    return goal.strip("".join(chr(i) for i in range(33)))


@router.get("/analysis-status")
def analysis_status(
    id: int = Query(...), goal: str = Query(...), mode: str | None = Query(default=None),
    user_id: int = Depends(get_status_user),
    media: MediaService = Depends(get_media_for_status),
    status: AnalysisStatusService = Depends(get_status_service),
):
    media.require_owned_media(id, user_id)
    normalized = _normalized_goal(goal)
    resolved_mode = AnalysisMode.from_request(mode)
    return ok(status.current(id, normalized, resolved_mode).to_dict())


@router.get("/analysis-events")
def analysis_events(
    id: int = Query(...), goal: str = Query(...), mode: str | None = Query(default=None),
    user_id: int = Depends(get_status_user),
    media: MediaService = Depends(get_media_for_status),
    status: AnalysisStatusService = Depends(get_status_service),
    events: TaskEventService = Depends(get_task_event_service),
):
    media.require_owned_media(id, user_id)
    normalized = _normalized_goal(goal)
    resolved_mode = AnalysisMode.from_request(mode)
    initial_status = status.current(id, normalized, resolved_mode)
    initial_stage = status.stage(id, normalized, resolved_mode)

    async def stream():
        async for event in events.subscribe(id, ANALYSIS, normalized, resolved_mode, initial_status, initial_stage):
            payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            yield "event: task-status\ndata: " + payload + "\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
