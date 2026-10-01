"""Original /analysis JSON routes; status/SSE and processing live in sibling routers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from concurrent.futures import Future
import re
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.orm import Session

from app.agent.budget import BudgetExceededError
from app.api.dependencies import get_current_user, get_redis, get_session
from app.api.errors import BusinessError, ErrorCode
from app.api.media import get_media_service
from app.api.responses import ok
from app.db.checkpoints import AgentCheckpointRepository
from app.schemas.mode import AnalysisMode
from app.services.analysis import VideoContextNotReadyError
from app.services.checkpoints import AgentCheckpointService
from app.services.dispatch import SubmissionResult
from app.services.media import MediaService
from app.workers.executors import BoundedExecutor, RejectedExecution


router = APIRouter(prefix="/analysis")
_EXECUTOR_LOCK = Lock()
DEFAULT_GOAL = "理解视频核心内容并生成结构化分析报告"
MAX_TEXT_LENGTH = 500


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _blank(value: str | None) -> bool:
    return value is None or not value or all(
        character.isspace() and character not in "\u00a0\u2007\u202f" for character in value
    )


def _trim(value: str) -> str:
    return value.strip("".join(chr(i) for i in range(33)))


def _normalize_text(value: str | None, field: str) -> str:
    if _blank(value) or _java_length(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{field}不能为空且不能超过 {MAX_TEXT_LENGTH} 字")
    return _trim(value)


class RouteRequest(BaseModel):
    goal: str

    @field_validator("goal")
    @classmethod
    def valid_goal(cls, value: str) -> str:
        if _blank(value):
            raise ValueError("分析目标不能为空")
        if _java_length(value) > MAX_TEXT_LENGTH:
            raise ValueError("分析目标不能超过 500 字")
        return value


class AgentFeedbackRequest(BaseModel):
    mediaId: int
    goal: str
    mode: str | None = None
    rating: int | None = None
    errorType: str | None = None
    comment: str | None = None
    correctedGoal: str | None = None
    correctedTasks: list[str] | None = None
    evidenceTimestamp: int | None = None
    evidenceAccepted: bool | None = None
    createdAt: str | None = None

    @model_validator(mode="before")
    @classmethod
    def keep_java_required_field_messages(cls, value):
        if isinstance(value, dict):
            return {"mediaId": None, "goal": None, **value}
        return value

    @field_validator("mediaId", mode="before")
    @classmethod
    def valid_media_id(cls, value):
        if value is None:
            raise PydanticCustomError("media_id_required", "mediaId 不能为空")
        return value

    @field_validator("goal")
    @classmethod
    def valid_goal(cls, value: str) -> str:
        if _blank(value):
            raise PydanticCustomError("goal_required", "分析目标不能为空")
        if _java_length(value) > 500:
            raise PydanticCustomError("goal_too_long", "分析目标不能超过 500 字")
        return value

    @field_validator("goal", mode="before")
    @classmethod
    def required_goal(cls, value):
        if value is None:
            raise PydanticCustomError("goal_required", "分析目标不能为空")
        return value

    @field_validator("errorType", "comment", "correctedGoal")
    @classmethod
    def valid_optional_text(cls, value: str | None, info) -> str | None:
        limit = {"errorType": 64, "comment": 2000, "correctedGoal": 500}[info.field_name]
        if value is not None and _java_length(value) > limit:
            raise ValueError(f"{info.field_name}不能超过 {limit} 字")
        return value

    @field_validator("correctedTasks")
    @classmethod
    def valid_tasks(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(value) > 5 or any(_java_length(task) > 500 for task in value)):
            raise ValueError("修正任务最多 5 条，且每条不能超过 500 字")
        return value

    @field_validator("evidenceTimestamp")
    @classmethod
    def valid_timestamp(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("证据时间戳不能为负数")
        return value


def get_analysis_ai(
    request: Request, session: Session = Depends(get_session), redis_client=Depends(get_redis),
) -> Any:
    factory = getattr(request.app.state, "analysis_ai_factory", None)
    return factory(session, redis_client) if factory is not None else request.app.state.analysis_ai


def get_analysis_dispatch(
    request: Request, session: Session = Depends(get_session), redis_client=Depends(get_redis),
) -> Any:
    factory = getattr(request.app.state, "analysis_dispatch_factory", None)
    return factory(session, redis_client) if factory is not None else request.app.state.analysis_dispatch


def get_analysis_checkpoints(
    session: Session = Depends(get_session), redis_client=Depends(get_redis),
) -> AgentCheckpointService:
    return AgentCheckpointService(AgentCheckpointRepository(session, redis_client), redis_client)


def get_analysis_media(media: MediaService = Depends(get_media_service)) -> MediaService:
    return media


def get_analysis_telemetry(request: Request) -> Any:
    return request.app.state.analysis_telemetry


def get_analysis_mode_router(request: Request) -> Any:
    return request.app.state.analysis_mode_router


def get_analysis_executor(request: Request) -> Any:
    if not hasattr(request.app.state, "analysis_executor"):
        with _EXECUTOR_LOCK:
            if not hasattr(request.app.state, "analysis_executor"):
                request.app.state.analysis_executor = BoundedExecutor("AI-Thread-", max_workers=8, queue_capacity=100)
    return request.app.state.analysis_executor


def _submission_response(result: SubmissionResult | str):
    if result == SubmissionResult.ACCEPTED:
        return JSONResponse(ok(), status_code=202)
    if result == SubmissionResult.RATE_LIMITED:
        raise BusinessError(ErrorCode.RATE_LIMITED, "系统繁忙，请稍后再试")
    if result == SubmissionResult.DUPLICATE:
        raise BusinessError(ErrorCode.CONFLICT, "相同视频和分析目标正在处理中")
    raise BusinessError(ErrorCode.INTERNAL_ERROR, "任务提交失败")


def _ensure_rating(feedback: AgentFeedbackRequest) -> None:
    if feedback.rating is not None and feedback.rating not in (-1, 1):
        raise BusinessError(ErrorCode.INVALID_ARGUMENT, "rating 只能是 -1 或 1")


_INSTANT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,9}))?(?:Z|[+-]\d{2}:\d{2})$"
)


def _feedback_data(payload: AgentFeedbackRequest) -> dict[str, Any]:
    """Parse Java Instant input before applying controller business validation."""
    feedback = payload.model_dump()
    if payload.createdAt is None:
        return feedback
    match = _INSTANT_PATTERN.fullmatch(payload.createdAt)
    if match is None:
        raise BusinessError(ErrorCode.INVALID_ARGUMENT, "请求参数不合法")
    try:
        instant = datetime.fromisoformat(payload.createdAt.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("timezone required")
        utc = instant.astimezone(timezone.utc)
    except ValueError as exc:
        raise BusinessError(ErrorCode.INVALID_ARGUMENT, "请求参数不合法") from exc
    fraction = match.group(1)
    nanos = fraction.ljust(9, "0") if fraction is not None else ""
    if nanos and int(nanos):
        precision = 3 if not int(nanos[3:]) else 6 if not int(nanos[6:]) else 9
        suffix = "." + nanos[:precision]
    else:
        suffix = ""
    feedback["createdAt"] = utc.strftime("%Y-%m-%dT%H:%M:%S") + suffix + "Z"
    return feedback


def _require_context(media_id: int, checkpoints: Any) -> None:
    if checkpoints.load_context(media_id) is None:
        raise BusinessError(ErrorCode.CONFLICT, str(VideoContextNotReadyError()))


async def _interactive(executor: Any, function, *args) -> Any:
    try:
        future: Future = executor.submit(function, *args)
    except RejectedExecution as error:
        raise BusinessError(ErrorCode.SERVICE_UNAVAILABLE, "AI 请求较多，请稍后再试") from error
    try:
        return await asyncio.wrap_future(future)
    except BudgetExceededError as error:
        raise BusinessError(ErrorCode.UNPROCESSABLE, str(error)) from error
    except VideoContextNotReadyError as error:
        raise BusinessError(ErrorCode.CONFLICT, str(error)) from error


@router.post("/route")
def route_mode(
    payload: RouteRequest,
    user_id: int = Depends(get_current_user),
    mode_router: Any = Depends(get_analysis_mode_router),
):
    return ok(mode_router.route(_normalize_text(payload.goal, "分析目标"), user_id))


@router.post("/ai")
def ai_analyze(
    id: int = Query(...), goal: str = Query(DEFAULT_GOAL), mode: str | None = Query(None),
    user_id: int = Depends(get_current_user),
    media: Any = Depends(get_analysis_media),
    checkpoints: Any = Depends(get_analysis_checkpoints),
    dispatch: Any = Depends(get_analysis_dispatch),
):
    normalized = _normalize_text(goal, "分析目标")
    resolved = AnalysisMode.from_request(mode)
    media_file = media.require_owned_media(id, user_id)
    if checkpoints.load_result(id, normalized, resolved) is not None:
        return ok()
    return _submission_response(dispatch.submit(media_file, normalized, None, resolved))


@router.post("/follow-up")
async def follow_up(
    id: int = Query(...), question: str = Query(...), goal: str | None = Query(None),
    mode: str | None = Query(None), user_id: int = Depends(get_current_user),
    media: Any = Depends(get_analysis_media), checkpoints: Any = Depends(get_analysis_checkpoints),
    dispatch: Any = Depends(get_analysis_dispatch), ai: Any = Depends(get_analysis_ai),
    executor: Any = Depends(get_analysis_executor),
):
    normalized_question = _normalize_text(question, "追问内容")
    normalized_goal = None if goal is None or _blank(goal) else _normalize_text(goal, "原始分析目标")
    media.require_owned_media(id, user_id)
    _require_context(id, checkpoints)
    dispatch.require_ai_quota(user_id)
    resolved = AnalysisMode.from_request(mode)
    return ok(await _interactive(executor, ai.follow_up, id, normalized_goal, normalized_question, resolved))


@router.get("/evidence-search")
async def evidence_search(
    id: int = Query(...), query: str = Query(...), user_id: int = Depends(get_current_user),
    media: Any = Depends(get_analysis_media), checkpoints: Any = Depends(get_analysis_checkpoints),
    dispatch: Any = Depends(get_analysis_dispatch), ai: Any = Depends(get_analysis_ai),
    executor: Any = Depends(get_analysis_executor),
):
    media.require_owned_media(id, user_id)
    _require_context(id, checkpoints)
    dispatch.require_ai_quota(user_id)
    normalized = _normalize_text(query, "检索问题")
    return ok(await _interactive(executor, ai.search_evidence, id, normalized))


@router.post("/agent-feedback")
def save_agent_feedback(
    payload: AgentFeedbackRequest, user_id: int = Depends(get_current_user),
    media: Any = Depends(get_analysis_media), checkpoints: Any = Depends(get_analysis_checkpoints),
):
    feedback = _feedback_data(payload)
    _ensure_rating(payload)
    media.require_owned_media(payload.mediaId, user_id)
    feedback["mode"] = AnalysisMode.from_request(payload.mode).value
    checkpoints.save_feedback(feedback)
    return ok()


@router.post("/agent-revise")
def revise_agent_result(
    payload: AgentFeedbackRequest, mode: str | None = Query(None),
    user_id: int = Depends(get_current_user), media: Any = Depends(get_analysis_media),
    dispatch: Any = Depends(get_analysis_dispatch), ai: Any = Depends(get_analysis_ai),
):
    feedback = _feedback_data(payload)
    _ensure_rating(payload)
    media_file = media.require_owned_media(payload.mediaId, user_id)
    revised_goal = ai.revision_goal(feedback)
    return _submission_response(dispatch.submit(media_file, revised_goal, feedback, AnalysisMode.from_request(mode)))


@router.get("/agent-feedback")
def list_agent_feedback(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: Any = Depends(get_analysis_media), checkpoints: Any = Depends(get_analysis_checkpoints),
):
    media.require_owned_media(id, user_id)
    return ok(checkpoints.load_feedback(id))


@router.get("/agent-plan")
def agent_plan(
    id: int = Query(...), goal: str = Query(...), mode: str | None = Query(None),
    user_id: int = Depends(get_current_user), media: Any = Depends(get_analysis_media),
    checkpoints: Any = Depends(get_analysis_checkpoints),
):
    media.require_owned_media(id, user_id)
    return ok(checkpoints.load_plan(id, _normalize_text(goal, "分析目标"), AnalysisMode.from_request(mode)))


@router.get("/agent-trace")
def agent_trace(
    id: int = Query(...), goal: str = Query(...), mode: str | None = Query(None),
    user_id: int = Depends(get_current_user), media: Any = Depends(get_analysis_media),
    telemetry: Any = Depends(get_analysis_telemetry),
):
    media.require_owned_media(id, user_id)
    return ok(telemetry.latest(id, _normalize_text(goal, "分析目标"), AnalysisMode.from_request(mode)))
