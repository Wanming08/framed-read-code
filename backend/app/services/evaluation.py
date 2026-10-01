"""The seven existing AgentEvaluationService metrics and offline golden-task runner."""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.mode import AnalysisMode
from app.schemas.video import VideoContext
from app.services.analysis_status import _result_markdown


LOG = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class AgentEvaluationService:
    def __init__(self, checkpoints: Any, evidence: Any) -> None:
        self.checkpoints = checkpoints
        self.evidence = evidence

    def evaluate(self, media_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> dict[str, Any]:
        mode = mode or AnalysisMode.GENERAL
        context = self.checkpoints.load_context(media_id)
        state = self.checkpoints.load_result(media_id, goal, mode)
        if state is None:
            state = self.checkpoints.load_critic_state(media_id, goal, mode)
        feedback = [
            item for item in self.checkpoints.load_feedback(media_id)
            if (goal == _field(item, "goal") or goal == _field(item, "correctedGoal"))
            and AnalysisMode.from_nullable(_field(item, "mode")) == mode
        ]
        metrics = self.evaluate_state(context, state)
        rated = [item for item in feedback if _field(item, "rating") is not None]
        metrics["userAcceptanceRate"] = (
            sum(_field(item, "rating") > 0 for item in rated) / len(rated) if rated else 0.0
        )
        metrics["feedbackSamples"] = len(feedback)
        return metrics

    def evaluate_state(self, context: Any, state: Any) -> dict[str, Any]:
        result = _field(state, "result")
        title = _field(result, "title")
        conclusions = _field(result, "conclusions") or []
        evidence = _field(result, "evidence") or []
        critique = _field(state, "critique")
        return {
            "structuredValid": bool(result is not None and title and title.strip() and conclusions and evidence),
            "timestampCoverageRate": (
                sum(self.evidence.timestamp_covered(context, item) for item in evidence) / len(evidence)
                if context is not None and evidence else 0.0
            ),
            "evidenceSupportRate": (
                sum(self.evidence.supported(context, item) for item in evidence) / len(evidence)
                if context is not None and evidence else 0.0
            ),
            "claimEvidenceSupportRate": (
                sum(any(self.evidence.supports_claim(context, claim, item) for item in evidence)
                    for claim in conclusions) / len(conclusions)
                if context is not None and conclusions else 0.0
            ),
            "criticPassed": bool(critique is not None and _field(critique, "passed", False)),
        }


class OfflineAgentEvaluationRunner:
    """Run the unchanged golden task rubric when explicitly invoked."""

    def __init__(self, agent_loop: Any, evaluation: AgentEvaluationService, telemetry: Any) -> None:
        self.agent_loop = agent_loop
        self.evaluation = evaluation
        self.telemetry = telemetry

    def run(self, tasks: list[dict[str, Any]]) -> dict[str, int]:
        passed = 0
        for index, task in enumerate(tasks):
            name = task.get("name")
            if not name or not str(name).strip():
                raise ValueError("task name is required")
            if not task.get("context"):
                raise ValueError("task context is required")
            context = VideoContext.from_dict(task["context"])
            expected = task.get("expectedKeywords") or []
            trace_id = self.telemetry.start(-1 - index, context.userGoal)
            self.telemetry.bind(trace_id)
            try:
                state = self.agent_loop.run(context)
                metrics = self.evaluation.evaluate_state(context, state)
                result = _field(state, "result")
                output = _result_markdown(result)
                coverage = self.keyword_coverage(output, expected)
                success = bool(
                    metrics["structuredValid"]
                    and metrics["claimEvidenceSupportRate"] >= 0.8
                    and coverage >= 0.8
                )
                passed += int(success)
                LOG.info("offline_agent_evaluation name=%s success=%s keywordCoverage=%s metrics=%s",
                         name, success, coverage, metrics)
            except RuntimeError:
                LOG.warning("offline_agent_evaluation_failed name=%s", name, exc_info=True)
            finally:
                self.telemetry.flush(trace_id)
                self.telemetry.clear()
        LOG.info("offline_agent_evaluation_completed passed=%s total=%s", passed, len(tasks))
        return {"passed": passed, "total": len(tasks)}

    @staticmethod
    def keyword_coverage(output: str, expected: list[str]) -> float:
        if not expected:
            return 1.0
        normalized = output.lower()
        return sum(keyword.lower() in normalized for keyword in expected) / len(expected)
