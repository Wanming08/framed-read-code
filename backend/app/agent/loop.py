"""Controlled Planner -> Executor -> Critic loop from AgentLoopService.java."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.budget import AgentExecutionBudget, BudgetExceededError, DeadlineExceededError
from app.agent.modes import ModeProfile
from app.schemas.mode import AnalysisMode
from app.schemas.video import VideoContext, VideoEvidence, VideoSegment


LOG = logging.getLogger(__name__)
MAX_PLAN_TASKS = 5


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _java_blank(value: str | None) -> bool:
    return value is None or not value or all(
        character.isspace() and character not in "\u00a0\u2007\u202f" for character in value
    )


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _list(value: Any) -> list[Any]:
    return [] if value is None else list(value)


def _verification_context(context: Any) -> VideoContext:
    if isinstance(context, VideoContext):
        return context
    segments = [
        segment if isinstance(segment, VideoSegment) else VideoSegment(
            startMs=_field(segment, "startMs"),
            endMs=_field(segment, "endMs"),
            transcript=_field(segment, "transcript", ""),
            ocrTexts=_field(segment, "ocrTexts", []),
            evidenceFrames=_field(segment, "evidenceFrames", []),
        )
        for segment in _field(context, "segments", [])
    ]
    return VideoContext(_field(context, "source"), _field(context, "userGoal", ""), segments)


def _verification_evidence(item: dict[str, Any]) -> VideoEvidence:
    return VideoEvidence(item["timestampMs"], item["source"], item["content"], item["claim"])


def _normal_plan(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    understood = _field(value, "understoodGoal", _field(value, "understood_goal"))
    return {
        "understoodGoal": "" if understood is None else understood.strip(),
        "tasks": _list(_field(value, "tasks")),
    }


def _normal_result(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    title = _field(value, "title")
    evidence = []
    for item in _list(_field(value, "evidence")):
        timestamp = _field(item, "timestampMs", _field(item, "timestamp_ms", 0))
        if timestamp < 0:
            raise ValueError("evidence timestamp cannot be negative")
        evidence.append({
            "timestampMs": timestamp,
            "source": (_field(item, "source") or "UNKNOWN").strip(),
            "content": (_field(item, "content") or "").strip(),
            "claim": (_field(item, "claim") or "").strip(),
        })
    sections = []
    for item in _list(_field(value, "sections")):
        sections.append({
            "key": (_field(item, "key") or "").strip(),
            "title": (_field(item, "title") or "").strip(),
            "items": _list(_field(item, "items")),
        })
    return {
        "title": "未命名分析" if title is None else title.strip(),
        "conclusions": _list(_field(value, "conclusions")),
        "evidence": evidence,
        "suggestions": _list(_field(value, "suggestions")),
        "sections": sections,
    }


def _normal_critique(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "passed": False,
            "feedback": ["Critic 未返回有效结果"],
            "missingRequirements": [],
            "unsupportedClaims": [],
            "requiredTimestamps": [],
        }
    return {
        "passed": bool(_field(value, "passed")),
        "feedback": _list(_field(value, "feedback")),
        "missingRequirements": _list(_field(value, "missingRequirements", _field(value, "missing_requirements"))),
        "unsupportedClaims": _list(_field(value, "unsupportedClaims", _field(value, "unsupported_claims"))),
        "requiredTimestamps": _list(_field(value, "requiredTimestamps", _field(value, "required_timestamps"))),
    }


def _normal_state(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    raw_critique = _field(value, "critique")
    return {
        "goal": _field(value, "goal"),
        "plan": _normal_plan(_field(value, "plan")),
        "result": _normal_result(_field(value, "result")),
        "critique": _normal_critique(raw_critique) if raw_critique is not None else None,
        "round": _field(value, "round", 0),
    }


def _state(goal: str, plan: dict[str, Any], result: dict[str, Any] | None,
           critique: dict[str, Any] | None, round_number: int) -> dict[str, Any]:
    if _java_blank(goal):
        raise ValueError("agent goal is required")
    if round_number < 0:
        raise ValueError("agent round cannot be negative")
    return {"goal": goal.strip("".join(chr(i) for i in range(33))),
            "plan": plan, "result": result, "critique": critique, "round": round_number}


class AgentLoopService:
    def __init__(
        self, llm: Any, long_context: Any, checkpoints: Any, telemetry: Any,
        evidence: Any, events: Any, *, max_rounds: int = 2,
        max_duration_ms: int = 120_000, max_estimated_tokens: int = 50_000,
        max_estimated_cost: float = 0,
    ) -> None:
        if max_rounds < 1 or max_duration_ms < 1 or max_estimated_tokens < 1 or max_estimated_cost < 0:
            raise ValueError("Agent 终止预算配置无效")
        self.llm = llm
        self.long_context = long_context
        self.checkpoints = checkpoints
        self.telemetry = telemetry
        self.evidence = evidence
        self.events = events
        self.max_rounds = max_rounds
        self.max_duration_ms = max_duration_ms
        self.max_estimated_tokens = max_estimated_tokens
        self.max_estimated_cost = max_estimated_cost

    def run(
        self, media_id: int | Any,
        context: Any | None = None,
        profile: ModeProfile | None = None,
    ) -> dict[str, Any]:
        if context is None:
            context, media_id = media_id, None
        self._validate_context(context)
        try:
            with AgentExecutionBudget.open(self.max_duration_ms):
                return self._run_within_budget(media_id, context, profile)
        except BudgetExceededError:
            raise
        except RuntimeError as error:
            deadline = self._find_deadline(error)
            if deadline is None:
                raise
            self.telemetry.increment_current("budgetTerminations", 1)
            raise BudgetExceededError(str(deadline)) from error

    def _run_within_budget(self, media_id: int | None, context: Any, profile: ModeProfile | None) -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        mode = self._mode(profile)
        goal = self._goal(context)
        saved = _normal_state(self.checkpoints.load_critic_state(media_id, goal, mode)) if media_id is not None else None
        terminal = (
            saved is not None and saved["result"] is not None and saved["critique"] is not None
            and (saved["round"] >= self.max_rounds or saved["critique"]["passed"])
        )
        if terminal and self._plan_valid(saved["plan"]) and self._result_valid(saved["result"], profile):
            self.checkpoints.save_result(media_id, saved, mode)
            self.telemetry.increment_current("terminalCheckpointHits", 1)
            return saved
        if terminal:
            self.telemetry.increment_current("invalidTerminalCheckpointRepairs", 1)
            saved = _state(saved["goal"], saved["plan"], saved["result"], saved["critique"], 0)

        relevant = self.long_context.select_relevant(media_id, context)
        plan = self._resolve_plan(media_id, relevant, saved, profile)
        self._check_budget(started_ns, "Planner")
        self._publish(media_id, self._goal(relevant), mode, "Planner 已完成任务拆解", "PLAN_COMPLETED")
        state = _state(self._goal(relevant), plan, None, None, 0) if saved is None else saved
        if state["critique"] is not None and not state["critique"]["passed"]:
            relevant = self._context_for_retry(media_id, context, relevant, state["critique"], profile)
            plan = self._revise_plan_for_retry(media_id, relevant, plan, state["critique"], profile)

        if state["result"] is not None and state["critique"] is None and state["round"] > 0:
            self.telemetry.increment_current("criticCheckpointResumes", 1)
            self._check_budget(started_ns, "Executor Checkpoint")
            state = self._critique_round(media_id, relevant, plan, state["result"], state["round"], profile)
            if not state["critique"]["passed"] and state["round"] < self.max_rounds:
                relevant = self._context_for_retry(media_id, context, relevant, state["critique"], profile)
                plan = self._revise_plan_for_retry(media_id, relevant, plan, state["critique"], profile)

        for round_number in range(state["round"] + 1, self.max_rounds + 1):
            self._check_budget(started_ns, f"Agent Round {round_number}")
            state = self._execute_round(media_id, relevant, plan, state["critique"], round_number, started_ns, profile)
            if state["critique"]["passed"]:
                break
            if round_number < self.max_rounds:
                relevant = self._context_for_retry(media_id, context, relevant, state["critique"], profile)
                plan = self._revise_plan_for_retry(media_id, relevant, plan, state["critique"], profile)
        if not self._result_valid(state["result"], profile):
            raise RuntimeError("Executor 未生成完整结构化结果")
        if media_id is not None:
            self.checkpoints.save_result(media_id, state, mode)
        return state

    def _resolve_plan(self, media_id: int | None, context: Any,
                      saved: dict[str, Any] | None, profile: ModeProfile | None) -> dict[str, Any]:
        mode = self._mode(profile)
        goal = self._goal(context)
        plan = _normal_plan(self.checkpoints.load_plan(media_id, goal, mode)) if media_id is not None else None
        if plan is None and saved is not None:
            plan = saved["plan"]
        should_persist = False
        if plan is None:
            plan = _normal_plan(self.llm.plan(context, self._instruction(profile, "plan_instruction")))
            should_persist = True
        if not self._plan_valid(plan):
            plan = _normal_plan(self.llm.repair_plan(context, plan, self._instruction(profile, "plan_instruction")))
            self.telemetry.increment_current("planStructureRepairs", 1)
            should_persist = True
        if not self._plan_valid(plan):
            raise RuntimeError("Planner 返回了无效任务列表")
        if media_id is not None and should_persist:
            self.checkpoints.save_plan(media_id, goal, mode, plan)
        return plan

    def _execute_round(self, media_id: int | None, context: Any, plan: dict[str, Any],
                       previous_critique: dict[str, Any] | None, round_number: int,
                       started_ns: int, profile: ModeProfile | None) -> dict[str, Any]:
        goal, mode = self._goal(context), self._mode(profile)
        self._publish(media_id, goal, mode, "Executor 正在按计划生成结构化产物", "EXECUTOR_STARTED")
        result = _normal_result(self.llm.execute(context, plan, previous_critique, self._instruction(profile, "execute_instruction")))
        draft = _state(goal, plan, result, None, round_number)
        if media_id is not None:
            self.checkpoints.save_execution_state(media_id, draft, mode)
            self._publish(media_id, goal, mode, "Executor 草稿已保存，开始校验证据", "EXECUTOR_COMPLETED")
        self._check_budget(started_ns, "Executor")
        return self._critique_round(media_id, context, plan, result, round_number, profile)

    def _critique_round(self, media_id: int | None, context: Any, plan: dict[str, Any],
                        result: dict[str, Any], round_number: int, profile: ModeProfile | None) -> dict[str, Any]:
        goal, mode = self._goal(context), self._mode(profile)
        self._publish(media_id, goal, mode, "Critic 正在核验目标覆盖与时间戳证据", "CRITIC_STARTED")
        critique = _normal_critique(self.llm.critique(context, plan, result, self._instruction(profile, "critic_instruction")))
        critique = self._enforce_structure(result, critique, profile)
        critique = self._enforce_evidence(context, result, critique)
        self.telemetry.increment_current("criticRounds", 1)
        if critique["passed"]:
            self.telemetry.increment_current("criticPassed", 1)
        state = _state(goal, plan, result, critique, round_number)
        if media_id is not None:
            self.checkpoints.save_critic_state(media_id, state, mode)
            if critique["passed"]:
                message, stage = "Critic 校验通过，正在整理结构化结果", "CRITIC_PASSED"
            elif round_number >= self.max_rounds:
                message, stage = "Critic 达到最大校验轮次，正在保留警告并生成结果", "ANALYSIS_COMPLETED_WITH_WARNINGS"
            elif self._requires_evidence_refresh(critique):
                message, stage = "Critic 发现证据缺口，正在定向补充证据", "CRITIC_RETRY_REQUIRED"
            else:
                message, stage = "Critic 发现目标覆盖或结构问题，正在按反馈重写", "CRITIC_RETRY_REQUIRED"
            self._publish(media_id, goal, mode, message, stage)
        return state

    @staticmethod
    def _validate_context(context: Any) -> None:
        goal, segments = _field(context, "userGoal", _field(context, "user_goal")), _field(context, "segments")
        if context is None or _java_blank(goal) or not segments or any(segment is None for segment in segments):
            raise ValueError("Agent 需要目标和至少一个视频片段")

    @staticmethod
    def _plan_valid(plan: dict[str, Any] | None) -> bool:
        if plan is None or _java_blank(plan.get("understoodGoal")):
            return False
        tasks = plan.get("tasks")
        return bool(tasks) and len(tasks) <= MAX_PLAN_TASKS and all(
            task is not None and not _java_blank(task) and _java_length(task) <= 500 for task in tasks
        )

    @classmethod
    def _result_valid(cls, result: dict[str, Any] | None, profile: ModeProfile | None) -> bool:
        if result is None or _java_blank(result.get("title")) or not result.get("conclusions") or not result.get("evidence"):
            return False
        return not cls._missing_section_keys(result, profile)

    @staticmethod
    def _missing_section_keys(result: dict[str, Any] | None, profile: ModeProfile | None) -> list[str]:
        if profile is None or not profile.required_section_keys:
            return []
        sections = [] if result is None else result.get("sections") or []
        present = {
            section["key"].strip() for section in sections
            if section is not None and section.get("key") and not _java_blank(section["key"]) and section.get("items")
        }
        return [key for key in profile.required_section_keys if key not in present]

    def _enforce_structure(self, result: dict[str, Any] | None,
                           critique: dict[str, Any], profile: ModeProfile | None) -> dict[str, Any]:
        feedback = list(critique["feedback"])
        if result is None or _java_blank(result.get("title")):
            feedback.append("补充明确的产物标题")
        if result is None or not result.get("conclusions"):
            feedback.append("补充覆盖 Planner 任务的核心结论")
        if result is None or not result.get("evidence"):
            feedback.append("为核心结论补充带时间戳的 ASR 或 OCR 证据")
        missing = self._missing_section_keys(result, profile)
        if missing:
            feedback.append("补充当前分析模式要求的结构化段落: " + ", ".join(missing))
        if feedback == critique["feedback"]:
            return critique
        return {**critique, "passed": False, "feedback": feedback}

    def _enforce_evidence(self, context: Any, result: dict[str, Any] | None,
                          critique: dict[str, Any]) -> dict[str, Any]:
        problems = any(critique[field] for field in
                       ("feedback", "missingRequirements", "unsupportedClaims", "requiredTimestamps"))
        if critique["passed"] and problems:
            critique = {**critique, "passed": False}
        if not critique["passed"] and not problems:
            critique = {**critique, "feedback": ["重新检查目标覆盖、结构完整性和证据绑定"]}
        if result is None or not result.get("evidence"):
            return critique
        verified_context = _verification_context(context)
        evidence = [(item, _verification_evidence(item)) for item in result["evidence"]]
        invalid = [item for item, typed in evidence if not self.evidence.supported(verified_context, typed)]
        unsupported_claims = [
            claim for claim in result["conclusions"]
            if not any(self.evidence.supports_claim(verified_context, claim, typed) for _, typed in evidence)
        ]
        if not invalid and not unsupported_claims:
            return critique
        unsupported = list(critique["unsupportedClaims"])
        for claim in unsupported_claims:
            if claim not in unsupported:
                unsupported.append(claim)
        unsupported.extend("证据无法在原始 ASR/OCR 中核验: " + str(item["timestampMs"]) for item in invalid)
        required = list(critique["requiredTimestamps"])
        for item in invalid:
            if item["timestampMs"] not in required:
                required.append(item["timestampMs"])
        return {
            **critique, "passed": False,
            "feedback": [*critique["feedback"], "为每条结论重新检索并绑定可核验的时间戳证据"],
            "unsupportedClaims": unsupported,
            "requiredTimestamps": required,
        }

    @staticmethod
    def _requires_evidence_refresh(critique: dict[str, Any] | None) -> bool:
        return critique is not None and any(critique.get(field) for field in
                                         ("requiredTimestamps", "missingRequirements", "unsupportedClaims"))

    def _context_for_retry(self, media_id: int | None, full: Any, selected: Any,
                           critique: dict[str, Any], profile: ModeProfile | None) -> Any:
        if not self._requires_evidence_refresh(critique):
            self.telemetry.increment_current("criticRewriteOnlyRetries", 1)
            return selected
        self.telemetry.increment_current("criticEvidenceRefreshes", 1)
        refined = self.long_context.refine_for_critique(media_id, full, selected, critique)
        self._publish(media_id, self._goal(full), self._mode(profile), "已按 Critic 反馈补充定向证据", "EVIDENCE_REFRESHED")
        return refined

    def _revise_plan_for_retry(self, media_id: int | None, context: Any, plan: dict[str, Any],
                               critique: dict[str, Any], profile: ModeProfile | None) -> dict[str, Any]:
        if not critique["missingRequirements"]:
            return plan
        try:
            revised = _normal_plan(self.llm.replan(context, plan, critique, self._instruction(profile, "plan_instruction")))
            if not self._plan_valid(revised):
                raise RuntimeError("Planner 返回了无效任务列表")
            self.telemetry.increment_current("planRevisions", 1)
            if media_id is not None:
                mode, goal = self._mode(profile), self._goal(context)
                self.checkpoints.save_plan(media_id, goal, mode, revised)
                self._publish(media_id, goal, mode, "Planner 根据 Critic 反馈补充了遗漏任务", "PLAN_COMPLETED")
            return revised
        except RuntimeError:
            self.telemetry.increment_current("planRevisionFallbacks", 1)
            LOG.warning("agent_replan_failed mediaId=%s, fallback to current plan", media_id, exc_info=True)
            return plan

    def _check_budget(self, started_ns: int, stage: str) -> None:
        AgentExecutionBudget.check(stage)
        elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        usage = self.telemetry.current_usage()
        tokens = _field(usage, "estimatedTokens", _field(usage, "estimated_tokens", 0))
        cost = _field(usage, "estimatedCost", _field(usage, "estimated_cost", 0))
        reason = None
        if elapsed_ms > self.max_duration_ms:
            reason = f"Agent 超过最大执行时长 {self.max_duration_ms}ms"
        elif tokens > self.max_estimated_tokens:
            reason = f"Agent 超过最大 Token 预算 {self.max_estimated_tokens}"
        elif self.max_estimated_cost > 0 and cost > self.max_estimated_cost:
            reason = f"Agent 超过最大成本预算 {self.max_estimated_cost}"
        if reason is not None:
            self.telemetry.increment_current("budgetTerminations", 1)
            raise BudgetExceededError(stage + " 后终止：" + reason)

    def _publish(self, media_id: int | None, goal: str, mode: AnalysisMode,
                 message: str, stage: str) -> None:
        if media_id is not None:
            self.events.publish_analysis(media_id, goal, mode, {"state": "PROCESSING", "result": None, "message": message}, stage)

    @staticmethod
    def _mode(profile: ModeProfile | None) -> AnalysisMode:
        return AnalysisMode.GENERAL if profile is None else profile.mode

    @staticmethod
    def _instruction(profile: ModeProfile | None, field: str) -> str:
        return "" if profile is None else getattr(profile, field)

    @staticmethod
    def _goal(context: Any) -> str:
        return _field(context, "userGoal", _field(context, "user_goal"))

    @staticmethod
    def _find_deadline(error: BaseException) -> BaseException | None:
        current: BaseException | None = error
        for _ in range(16):
            if current is None:
                return None
            if isinstance(current, DeadlineExceededError) or type(current).__name__ == "ModelDeadlineExceeded":
                return current
            current = current.__cause__
        return None
