"""Nested Agent execution deadline from AgentExecutionBudget.java."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_deadline_ns: ContextVar[int | None] = ContextVar("agent_deadline_ns", default=None)


class DeadlineExceededError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


class AgentExecutionBudget:
    @staticmethod
    @contextmanager
    def open(max_duration_ms: int) -> Iterator[None]:
        if max_duration_ms < 1:
            raise ValueError("Agent 执行时长预算必须大于 0")
        previous = _deadline_ns.get()
        requested = time.monotonic_ns() + max_duration_ms * 1_000_000
        token = _deadline_ns.set(requested if previous is None else min(previous, requested))
        try:
            yield
        finally:
            _deadline_ns.reset(token)

    @staticmethod
    def remaining_millis() -> int:
        deadline = _deadline_ns.get()
        if deadline is None:
            return 2**63 - 1
        remaining_ns = deadline - time.monotonic_ns()
        if remaining_ns <= 0:
            raise DeadlineExceededError("Agent 已耗尽执行时长预算")
        return max(1, remaining_ns // 1_000_000)

    @classmethod
    def check(cls, stage: str) -> None:
        try:
            cls.remaining_millis()
        except DeadlineExceededError as error:
            raise DeadlineExceededError(stage + " 后终止：" + str(error)) from error
