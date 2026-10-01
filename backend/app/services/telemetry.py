"""Agent trace metrics and Redis snapshots compatible with the Java service."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from app.schemas.mode import AnalysisMode
from app.services.task_keys import goal_digest


LOGGER = logging.getLogger(__name__)
TRACE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TRACES = 500


@dataclass(frozen=True)
class BudgetUsage:
    estimated_tokens: int
    estimated_cost: float


@dataclass
class _TraceData:
    trace_id: str
    task_id: int
    digest: str
    task_key: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stage_durations: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)
    estimated_cost: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "taskId": self.task_id,
            "goalDigest": self.digest,
            "startedAt": self.started_at,
            "stageDurationMs": self.stage_durations.copy(),
            "counters": self.counters.copy(),
            "values": self.values.copy(),
            "estimatedCost": self.estimated_cost,
        }


class AgentTelemetry:
    def __init__(self, redis_client: Any):
        self.redis = redis_client
        self._traces: dict[str, _TraceData] = {}
        self._latest_by_task: dict[str, str] = {}
        self._current = threading.local()
        self._lock = threading.RLock()

    def start(self, task_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> str:
        digest = goal_digest(goal, mode)
        task_key = f"{task_id}:{digest}"
        with self._lock:
            if len(self._traces) >= MAX_TRACES:
                oldest = min(self._traces.values(), key=lambda trace: trace.started_at)
                self._traces.pop(oldest.trace_id, None)
                if self._latest_by_task.get(oldest.task_key) == oldest.trace_id:
                    self._latest_by_task.pop(oldest.task_key, None)
            trace_id = str(uuid.uuid4())
            trace = _TraceData(trace_id, task_id, digest, task_key)
            self._traces[trace_id] = trace
            self._latest_by_task[task_key] = trace_id
            self._current.trace_id = trace_id
            snapshot = trace.snapshot()
        self._persist(snapshot)
        return trace_id

    def bind(self, trace_id: str) -> None:
        self._current.trace_id = trace_id

    @contextmanager
    def trace_scope(self, trace_id: str) -> Iterator[None]:
        previous = getattr(self._current, "trace_id", None)
        self._current.trace_id = trace_id
        try:
            yield
        finally:
            if previous is None:
                self.clear()
            else:
                self._current.trace_id = previous

    def clear(self) -> None:
        if hasattr(self._current, "trace_id"):
            del self._current.trace_id

    def flush(self, trace_id: str) -> None:
        with self._lock:
            trace = self._traces.get(trace_id)
            snapshot = trace.snapshot() if trace else None
        if snapshot is not None:
            self._persist(snapshot)

    def stage(self, trace_id: str, stage: str, started_nanos: int, success: bool) -> None:
        duration_ms = max(0, (time.monotonic_ns() - started_nanos) // 1_000_000)
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                return
            trace.stage_durations[stage] = trace.stage_durations.get(stage, 0) + duration_ms
            self._increment_trace(trace, stage + "Calls", 1)
            if not success:
                self._increment_trace(trace, "failedStages", 1)
            snapshot = trace.snapshot()
        self._persist(snapshot)

    def increment(self, trace_id: str | None, metric: str, amount: int) -> None:
        with self._lock:
            trace = self._traces.get(trace_id) if trace_id else None
            if trace is not None:
                self._increment_trace(trace, metric, amount)

    def increment_current(self, name: str, count: int) -> None:
        self.increment(getattr(self._current, "trace_id", None), name, count)

    def value_current(self, name: str, value: float) -> None:
        with self._lock:
            trace = self._traces.get(getattr(self._current, "trace_id", None))
            if trace is not None:
                trace.values[name] = value

    def provider_request_current(self, provider: str, started_nanos: int) -> None:
        metrics = {
            "llm": ("llmRequests", "llmDurationMs"),
            "asr": ("asrRequests", "asrDurationMs"),
            "embedding": ("embeddingRequests", "embeddingDurationMs"),
        }
        calls_metric, duration_metric = metrics[provider]
        self.increment_current(calls_metric, 1)
        self.increment_current(duration_metric, max(0, (time.monotonic_ns() - started_nanos) // 1_000_000))

    def provider_usage_current(self, provider: str, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        metrics = {
            "llm": {
                "prompt_tokens": "llmPromptTokensActual",
                "completion_tokens": "llmCompletionTokensActual",
                "total_tokens": "llmTotalTokensActual",
            },
            "embedding": {
                "prompt_tokens": "embeddingPromptTokensActual",
                "total_tokens": "embeddingTotalTokensActual",
            },
        }[provider]
        valid = False
        for field, counter in metrics.items():
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.increment_current(counter, value)
                valid = True
        if valid:
            self.increment_current(provider + "UsageResponses", 1)

    def fail_current_stage(self, stage: str, started_nanos: int) -> None:
        trace_id = getattr(self._current, "trace_id", None)
        if trace_id is not None:
            self.stage(trace_id, stage, started_nanos, False)

    def model_call(
        self,
        stage: str,
        prompt: str | None,
        response: str | None,
        input_price_per_million: float,
        output_price_per_million: float,
        started_nanos: int,
    ) -> None:
        trace_id = getattr(self._current, "trace_id", None)
        input_tokens = self.estimate_tokens(prompt)
        output_tokens = self.estimate_tokens(response)
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                return
            self._increment_trace(trace, "modelCalls", 1)
            self._increment_trace(trace, "inputTokensEstimated", input_tokens)
            self._increment_trace(trace, "outputTokensEstimated", output_tokens)
            trace.estimated_cost += (
                input_tokens * input_price_per_million + output_tokens * output_price_per_million
            ) / 1_000_000
        self.stage(trace_id, stage, started_nanos, True)

    def current_usage(self) -> BudgetUsage:
        with self._lock:
            trace = self._traces.get(getattr(self._current, "trace_id", None))
            if trace is None:
                return BudgetUsage(0, 0.0)
            tokens = trace.counters.get("inputTokensEstimated", 0) + trace.counters.get("outputTokensEstimated", 0)
            return BudgetUsage(tokens, trace.estimated_cost)

    def latest(self, task_id: int, goal: str, mode: AnalysisMode = AnalysisMode.GENERAL) -> dict[str, Any]:
        digest = goal_digest(goal, mode)
        with self._lock:
            trace_id = self._latest_by_task.get(f"{task_id}:{digest}")
            trace = self._traces.get(trace_id) if trace_id else None
            if trace is not None:
                return trace.snapshot()
        try:
            if trace_id is None:
                trace_id = self._decode(self.redis.get(self._latest_key(task_id, digest)))
            payload = self.redis.get(self._trace_key(trace_id)) if trace_id else None
            return json.loads(payload) if payload else {}
        except Exception:
            LOGGER.warning("agent_trace_read_failed task_id=%s", task_id, exc_info=True)
            return {}

    def delete_task(self, task_id: int) -> None:
        prefix = f"{task_id}:"
        with self._lock:
            trace_ids = set()
            for task_key, trace_id in list(self._latest_by_task.items()):
                if task_key.startswith(prefix):
                    trace_ids.add(trace_id)
                    self._latest_by_task.pop(task_key, None)
        try:
            index_key = self._index_key(task_id)
            latest_keys = {self._decode(key) for key in self.redis.smembers(index_key)}
            for latest_key in latest_keys:
                trace_id = self._decode(self.redis.get(latest_key))
                if trace_id:
                    trace_ids.add(trace_id)
            if latest_keys:
                self.redis.delete(*latest_keys)
            if trace_ids:
                self.redis.delete(*(self._trace_key(trace_id) for trace_id in trace_ids))
            self.redis.delete(index_key)
        except Exception:
            LOGGER.warning("agent_trace_cleanup_failed task_id=%s", task_id, exc_info=True)
        with self._lock:
            for trace_id in trace_ids:
                self._traces.pop(trace_id, None)

    @staticmethod
    def estimate_tokens(value: str | None) -> int:
        if not value:
            return 0
        non_ascii = sum(ord(character) > 127 for character in value)
        ascii_count = len(value) - non_ascii
        return max(1, non_ascii + (ascii_count + 3) // 4)

    @staticmethod
    def _increment_trace(trace: _TraceData, metric: str, amount: int) -> None:
        trace.counters[metric] = trace.counters.get(metric, 0) + amount

    def _persist(self, snapshot: dict[str, Any]) -> None:
        try:
            trace_id = snapshot["traceId"]
            task_id = snapshot["taskId"]
            digest = snapshot["goalDigest"]
            latest_key = self._latest_key(task_id, digest)
            self.redis.set(self._trace_key(trace_id), json.dumps(snapshot, ensure_ascii=False), ex=TRACE_TTL_SECONDS)
            self.redis.set(latest_key, trace_id, ex=TRACE_TTL_SECONDS)
            index_key = self._index_key(task_id)
            self.redis.sadd(index_key, latest_key)
            self.redis.expire(index_key, TRACE_TTL_SECONDS)
        except Exception:
            LOGGER.warning("agent_trace_persist_failed trace_id=%s", snapshot["traceId"], exc_info=True)

    @staticmethod
    def _decode(value: Any) -> str | None:
        return value.decode("utf-8") if isinstance(value, bytes) else value

    @staticmethod
    def _trace_key(trace_id: str) -> str:
        return f"agent:trace:{trace_id}"

    @staticmethod
    def _latest_key(task_id: int, digest: str) -> str:
        return f"agent:trace:task:{task_id}:{digest}"

    @staticmethod
    def _index_key(task_id: int) -> str:
        return f"agent:trace:task:{task_id}:goals"
