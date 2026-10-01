"""Current analysis status and Markdown rendering from the Java DTO/services."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentCheckpoint
from app.schemas.mode import AnalysisMode
from app.services.task_keys import active_key, goal_digest, normalize_content_hash


class TaskStage(str, Enum):
    QUEUED = "QUEUED"
    CONSUMING = "CONSUMING"
    VIDEO_CONTEXT = "VIDEO_CONTEXT"
    CONTEXT_COMPLETED = "CONTEXT_COMPLETED"
    CHUNKS_COMPLETED = "CHUNKS_COMPLETED"
    RETRIEVAL = "RETRIEVAL"
    AGENT_LOOP = "AGENT_LOOP"
    PLAN_COMPLETED = "PLAN_COMPLETED"
    EXECUTOR_STARTED = "EXECUTOR_STARTED"
    EXECUTOR_COMPLETED = "EXECUTOR_COMPLETED"
    CRITIC_STARTED = "CRITIC_STARTED"
    CRITIC_PASSED = "CRITIC_PASSED"
    CRITIC_RETRY_REQUIRED = "CRITIC_RETRY_REQUIRED"
    EVIDENCE_REFRESHED = "EVIDENCE_REFRESHED"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
    ANALYSIS_COMPLETED_WITH_WARNINGS = "ANALYSIS_COMPLETED_WITH_WARNINGS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    COMPLETED_REUSED = "COMPLETED_REUSED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"
    MANUAL_REPLAY = "MANUAL_REPLAY"
    REVISION_PENDING = "REVISION_PENDING"
    REVISION_APPLIED = "REVISION_APPLIED"
    TRANSCRIPTION = "TRANSCRIPTION"
    ASR = "ASR"
    DISPATCH_FAILED = "DISPATCH_FAILED"

    @classmethod
    def from_value(cls, value: str | None) -> "TaskStage | None":
        if value is None or not value.strip():
            return None
        try:
            return cls(value)
        except ValueError:
            return None


class TaskState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TaskStatus:
    state: TaskState
    result: str | None
    message: str

    @classmethod
    def of(cls, state: TaskState, message: str) -> "TaskStatus":
        return cls(state, None, message)

    @classmethod
    def completed(cls, agent_state: dict[str, Any]) -> "TaskStatus":
        result = _result_markdown(agent_state["result"])
        critique = agent_state.get("critique")
        if critique is not None and critique.get("passed") is True:
            return cls(TaskState.COMPLETED, result, "任务完成")
        warning = "分析已完成，但部分结论未通过 Critic 校验，请结合时间戳证据人工核验。"
        return cls(TaskState.COMPLETED, "> **结果提示：** " + warning + "\n\n" + result, warning)

    def to_dict(self) -> dict[str, str | None]:
        return {"state": self.state.value, "result": self.result, "message": self.message}


def _result_markdown(result: dict[str, Any]) -> str:
    title = result.get("title")
    title = "未命名分析" if title is None else title.strip()
    output = "## " + title + "\n\n## 核心结论\n"
    for conclusion in result.get("conclusions") or []:
        output += "- " + conclusion + "\n"
    output += "\n## 视频证据\n"
    for evidence in result.get("evidence") or []:
        timestamp_ms = evidence.get("timestampMs") or 0
        if timestamp_ms < 0:
            raise ValueError("evidence timestamp cannot be negative")
        seconds = timestamp_ms // 1000
        source = (evidence.get("source") or "UNKNOWN").strip()
        content = (evidence.get("content") or "").strip()
        output += f"- [{seconds // 60:02d}:{seconds % 60:02d}] {source}：{content}\n"
    output += "\n## 建议\n"
    for suggestion in result.get("suggestions") or []:
        output += "- " + suggestion + "\n"
    for section in result.get("sections") or []:
        output += "\n## " + (section.get("title") or "").strip() + "\n"
        for item in section.get("items") or []:
            output += "- " + item + "\n"
    return output


class DatabaseCheckpointReader:
    """Read committed rows only; MySQL remains the checkpoint source of truth."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _row(self, media_id: int, goal: str, mode: AnalysisMode | str, field: str) -> AgentCheckpoint | None:
        digest = goal_digest(goal, _mode(mode))
        return self.session.get(AgentCheckpoint, (media_id, f"goal:{digest}:{field}"))

    def load_result(self, media_id: int, goal: str, mode: AnalysisMode | str) -> dict[str, Any] | None:
        row = self._row(media_id, goal, mode, "result")
        return json.loads(row.payload) if row is not None and row.payload is not None else None

    def load_stage(self, media_id: int, goal: str, mode: AnalysisMode | str) -> TaskStage | None:
        row = self._row(media_id, goal, mode, "stage")
        return TaskStage.from_value(row.stage if row is not None else None)


class RedisActiveReader:
    def __init__(self, redis_client: Any, media_service: Any) -> None:
        self.redis = redis_client
        self.media_service = media_service

    def is_active(self, media_id: int, goal: str, mode: AnalysisMode | str) -> bool:
        digest = goal_digest(goal, _mode(mode))
        content_hash = normalize_content_hash(media_id, self.media_service.content_hash(media_id))
        return bool(self.redis.exists(active_key(content_hash, digest))) or bool(
            self.redis.exists(active_key(f"media-{media_id}", digest))
        )


def _mode(value: AnalysisMode | str) -> AnalysisMode:
    if isinstance(value, AnalysisMode):
        return value
    return AnalysisMode.from_request(value)


class AnalysisStatusService:
    def __init__(self, checkpoints: Any, dispatch: Any) -> None:
        self.checkpoints = checkpoints
        self.dispatch = dispatch

    def current(self, media_id: int, goal: str, mode: AnalysisMode | str = AnalysisMode.GENERAL) -> TaskStatus:
        result = self.checkpoints.load_result(media_id, goal, mode)
        if result is not None and result.get("result") is not None:
            return TaskStatus.completed(result)
        stage = self.stage(media_id, goal, mode)
        if self.dispatch.is_active(media_id, goal, mode):
            state = TaskState.QUEUED if stage is None else TaskState.PROCESSING
            return TaskStatus.of(state, self._status_message(stage))
        if stage == TaskStage.BUDGET_EXHAUSTED:
            return TaskStatus.of(TaskState.FAILED, "Agent 已达到本次任务预算，请调整目标后重试")
        if stage in (TaskStage.FAILED, TaskStage.DEAD_LETTERED):
            return TaskStatus.of(TaskState.FAILED, "分析失败，请稍后重试")
        return TaskStatus.of(TaskState.NOT_STARTED, "尚未提交分析任务")

    def stage(self, media_id: int, goal: str, mode: AnalysisMode | str = AnalysisMode.GENERAL) -> TaskStage | None:
        return self.checkpoints.load_stage(media_id, goal, mode)

    @staticmethod
    def _status_message(stage: TaskStage | str | None) -> str:
        if stage is None or stage == TaskStage.QUEUED:
            return "任务已排队"
        if stage in (TaskStage.VIDEO_CONTEXT, TaskStage.CONTEXT_COMPLETED):
            return "正在解析视频语音和关键画面"
        if stage == TaskStage.CHUNKS_COMPLETED:
            return "正在检索与目标相关的视频证据"
        if stage == TaskStage.PLAN_COMPLETED:
            return "Planner 已完成任务拆解"
        if stage in (TaskStage.EXECUTOR_STARTED, TaskStage.EXECUTOR_COMPLETED):
            return "Executor 正在生成结构化产物"
        if stage == TaskStage.CRITIC_STARTED:
            return "Critic 正在核验结论和证据"
        if stage in (TaskStage.CRITIC_RETRY_REQUIRED, TaskStage.EVIDENCE_REFRESHED):
            return "正在根据 Critic 反馈补充证据"
        if stage == TaskStage.RETRYING:
            return "任务执行异常，正在自动重试"
        return "正在分析视频"
