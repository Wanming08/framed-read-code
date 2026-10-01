"""OpenAI-compatible chat adapter mirroring DeepSeekUtils.java decisions."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import Executor, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore
from typing import Any, Callable

import httpx

from app.agent import prompts


class ModelNonRetryableError(ValueError):
    """The provider rejected the request or returned an invalid response."""


class ModelCallError(RuntimeError):
    """Three retryable model requests all failed."""


class ModelDeadlineExceeded(ModelNonRetryableError):
    """The current Agent budget is too short for another model call."""


class _RetryableModelError(RuntimeError):
    pass


def _text(value: Any) -> str | None:
    """Accept Jackson's scalar-to-String coercion, but never collections."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError("model field must be text")


def _long(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("model field must be an integer")
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as error:
            raise TypeError("model field must be an integer") from error
    raise TypeError("model field must be an integer")


def _boolean(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise TypeError("model field must be a boolean")


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("model field must be an object")
    return value


def _list(value: Any, item_parser: Callable[[Any], Any], *, nullable_items: bool = False) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise TypeError("model field must be an array")
    if not nullable_items and any(item is None for item in value):
        raise TypeError("model array cannot contain null")
    return [item_parser(item) for item in value]


def _typed_object(value: Any, fields: dict[str, Callable[[Any], Any]]) -> dict[str, Any]:
    result = dict(_object(value))
    for name, parser in fields.items():
        if name in result:
            result[name] = parser(result[name])
    return result


def _evidence(value: Any) -> dict[str, Any]:
    result = _typed_object(value, {
        "timestampMs": _long, "source": _text, "content": _text, "claim": _text,
    })
    if result.get("timestampMs", 0) < 0:
        raise ValueError("evidence timestamp cannot be negative")
    return result


def _section(value: Any) -> dict[str, Any]:
    return _typed_object(value, {
        "key": _text, "title": _text, "items": lambda items: _list(items, _text),
    })


_TYPED_FIELDS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "PLANNER": {"understoodGoal": _text, "tasks": lambda items: _list(items, _text)},
    "REPLANNER": {"understoodGoal": _text, "tasks": lambda items: _list(items, _text)},
    "PLANNER_REPAIR": {"understoodGoal": _text, "tasks": lambda items: _list(items, _text)},
    "RETRIEVAL_PLANNER": {
        "semanticQuery": _text,
        "keywords": lambda items: _list(items, _text, nullable_items=True),
        "visualKeywords": lambda items: _list(items, _text, nullable_items=True),
    },
    "MODE_ROUTER": {"mode": _text, "reason": _text},
    "CHUNK_SUMMARY": {"segmentSummary": _text, "keywords": lambda items: _list(items, _text)},
    "EXECUTOR": {
        "title": _text,
        "conclusions": lambda items: _list(items, _text),
        "evidence": lambda items: _list(items, _evidence),
        "suggestions": lambda items: _list(items, _text),
        "sections": lambda items: _list(items, _section),
    },
    "CRITIC": {
        "passed": _boolean,
        "feedback": lambda items: _list(items, _text),
        "missingRequirements": lambda items: _list(items, _text),
        "unsupportedClaims": lambda items: _list(items, _text),
        "requiredTimestamps": lambda items: _list(items, _long),
    },
}


class LLMClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        base_url: str,
        model: str = "deepseek-ai/DeepSeek-V3.2",
        timeout_seconds: float = 300,
        input_price_per_million: float = 0,
        output_price_per_million: float = 0,
        max_estimated_cost: float = 0,
        remaining_ms: Callable[[], float] = lambda: float("inf"),
        sleep: Callable[[float], None] = time.sleep,
        telemetry: Any = None,
        model_executor: Executor | None = None,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("模型超时时间必须大于 0")
        if input_price_per_million < 0 or output_price_per_million < 0:
            raise ValueError("模型 Token 单价不能为负数")
        if max_estimated_cost > 0 and (input_price_per_million == 0 or output_price_per_million == 0):
            raise ValueError("启用 Agent 成本预算时必须配置输入和输出 Token 单价")
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.remaining_ms = remaining_ms
        self.sleep = sleep
        self.telemetry = telemetry
        self._owned_executor = model_executor is None
        self.model_executor = model_executor or ThreadPoolExecutor(max_workers=8, thread_name_prefix="dovideo-model")
        # Java's model pool has at most eight threads plus twenty queued calls.
        self._model_slots = BoundedSemaphore(28)

    def close(self) -> None:
        if self._owned_executor:
            self.model_executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def parse_json(response: str | None) -> Any:
        if response is None or not response.strip():
            raise ValueError("模型返回空响应")
        cleaned = response.replace("```json", "").replace("```", "").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型未返回 JSON 对象")
        return json.loads(cleaned[start : end + 1])

    def structured_chat(self, stage: str, prompt: str) -> Any:
        response = self.chat(stage, prompt)
        try:
            return self.parse_typed_json(response, stage)
        except (ValueError, TypeError):
            self._telemetry_increment("structuredOutputRetries", 1)
            return self.parse_typed_json(
                self.chat(stage, prompt + "\n请严格返回合法 JSON，不要添加解释或代码块。"), stage
            )

    @classmethod
    def parse_typed_json(cls, response: str | None, stage: str) -> dict[str, Any]:
        return _typed_object(cls.parse_json(response), _TYPED_FIELDS.get(stage, {}))

    def chat(self, stage: str, prompt: str) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            started_ns = time.monotonic_ns()
            try:
                result = self._invoke(prompt)
                if not result.strip():
                    raise _RetryableModelError("模型返回空响应")
                self._telemetry_model_call(stage, prompt, result, started_ns)
                return result
            except (ModelNonRetryableError, _RetryableModelError) as error:
                last_error = error
                self._telemetry_increment("modelCallFailures", 1)
                if isinstance(error, ModelNonRetryableError) or attempt == 2:
                    self._telemetry_fail(stage, started_ns)
                    if isinstance(error, ModelNonRetryableError):
                        raise
                    break
                self.sleep(1 << attempt)
            except Exception as error:
                self._telemetry_increment("modelCallFailures", 1)
                self._telemetry_fail(stage, started_ns)
                raise ModelNonRetryableError("模型请求不可重试") from error
        raise ModelCallError("模型调用达到最大重试次数") from last_error

    def _invoke(self, prompt: str) -> str:
        remaining = self.remaining_ms()
        if remaining <= 0:
            raise ModelDeadlineExceeded("模型调用超过 Agent 剩余时间预算")
        timeout_s = min(self.timeout_seconds, remaining / 1000)
        timeout = httpx.Timeout(
            connect=min(30, timeout_s),
            read=timeout_s,
            write=timeout_s,
            pool=min(5, timeout_s),
        )
        if not self._model_slots.acquire(blocking=False):
            raise _RetryableModelError("模型调用线程池繁忙")
        request_started_ns = time.monotonic_ns()
        try:
            future = self.model_executor.submit(self._post, prompt, timeout)
        except RuntimeError as error:
            self._model_slots.release()
            raise _RetryableModelError("模型调用线程池繁忙") from error
        future.add_done_callback(lambda _: self._model_slots.release())
        try:
            try:
                response = future.result(timeout=timeout_s)
            except FutureTimeoutError as error:
                future.cancel()
                if remaining <= self.timeout_seconds * 1000:
                    raise ModelDeadlineExceeded("模型调用超过 Agent 剩余时间预算") from error
                raise _RetryableModelError("模型调用超时") from error
            except httpx.TimeoutException as error:
                if remaining <= self.timeout_seconds * 1000:
                    raise ModelDeadlineExceeded("模型调用超过 Agent 剩余时间预算") from error
                raise _RetryableModelError("模型调用超时") from error
            except httpx.RequestError as error:
                raise _RetryableModelError("模型网络请求失败") from error
            if not response.is_success:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    raise _RetryableModelError(f"模型 HTTP {response.status_code}")
                raise ModelNonRetryableError(f"模型请求不可重试：HTTP {response.status_code}")
            try:
                payload = response.json()
                if self.telemetry is not None and isinstance(payload, dict):
                    self.telemetry.provider_usage_current("llm", payload.get("usage"))
                content = payload["choices"][0]["message"]["content"]
                if content is None:
                    return ""
                if not isinstance(content, str):
                    raise TypeError("chat content must be text")
                return content
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise ModelNonRetryableError("模型响应格式无效") from error
        finally:
            if self.telemetry is not None:
                self.telemetry.provider_request_current("llm", request_started_ns)

    def _post(self, prompt: str, timeout: httpx.Timeout) -> httpx.Response:
        return self.client.post(
            self.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": prompts.SYSTEM_POLICY},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )

    def _telemetry_increment(self, name: str, count: int) -> None:
        if self.telemetry is not None:
            self.telemetry.increment_current(name, count)

    def _telemetry_model_call(self, stage: str, prompt: str, response: str, started_ns: int) -> None:
        if self.telemetry is not None:
            self.telemetry.model_call(
                stage,
                prompts.SYSTEM_POLICY + "\n" + prompt,
                response,
                self.input_price_per_million,
                self.output_price_per_million,
                started_ns,
            )

    def _telemetry_fail(self, stage: str, started_ns: int) -> None:
        if self.telemetry is not None:
            self.telemetry.fail_current_stage(stage, started_ns)

    def plan(self, context: Any, mode_instruction: str = "") -> Any:
        try:
            return self.structured_chat("PLANNER", prompts.plan_prompt(context, mode_instruction))
        except Exception as error:
            raise RuntimeError("Agent 任务规划失败") from error

    def replan(self, context: Any, current_plan: Any, critique: Any, mode_instruction: str = "") -> Any:
        try:
            return self.structured_chat("REPLANNER", prompts.replan_prompt(context, current_plan, critique, mode_instruction))
        except Exception as error:
            raise RuntimeError("Agent 任务重规划失败") from error

    def repair_plan(self, context: Any, invalid_plan: Any, mode_instruction: str = "") -> Any:
        try:
            return self.structured_chat("PLANNER_REPAIR", prompts.repair_plan_prompt(context, invalid_plan, mode_instruction))
        except Exception as error:
            raise RuntimeError("Agent 任务计划修复失败") from error

    def plan_retrieval(self, goal: str) -> Any:
        try:
            return self.structured_chat("RETRIEVAL_PLANNER", prompts.retrieval_prompt(goal))
        except Exception as error:
            raise RuntimeError("视频检索目标拆解失败") from error

    def classify_mode(self, goal: str) -> Any:
        try:
            return self.structured_chat("MODE_ROUTER", prompts.classify_mode_prompt(goal))
        except Exception as error:
            raise RuntimeError("意图路由分类失败") from error

    def summarize_chunk(self, segments: Any) -> Any:
        try:
            return self.parse_typed_json(self.chat("CHUNK_SUMMARY", prompts.chunk_summary_prompt(segments)), "CHUNK_SUMMARY")
        except Exception as error:
            raise RuntimeError("视频片段摘要失败") from error

    def execute(self, context: Any, plan: Any, previous_critique: Any, mode_instruction: str = "") -> Any:
        try:
            return self.structured_chat("EXECUTOR", prompts.execute_prompt(context, plan, previous_critique, mode_instruction))
        except Exception as error:
            raise RuntimeError("Agent 执行失败") from error

    def critique(self, context: Any, plan: Any, result: Any, mode_instruction: str = "") -> Any:
        try:
            return self.structured_chat("CRITIC", prompts.critique_prompt(context, plan, result, mode_instruction))
        except Exception as error:
            raise RuntimeError("Critic 校验失败") from error
