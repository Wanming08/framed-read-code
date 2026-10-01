"""Bounded RocketMQ receive loop and fail-stop message lease handling.

The injected handler owns business status, retry, poison-message and checkpoint
rules. It returns ACK only after its durable terminal work is complete.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


logger = logging.getLogger(__name__)


class Disposition(Enum):
    ACK = "ack"
    RETRY = "retry"


class LeaseLostError(RuntimeError):
    """The message/lock lease is no longer safe for this process to use."""


@dataclass(frozen=True)
class LeasePolicy:
    visibility_seconds: int = 180
    lock_ttl_seconds: int = 90
    renew_interval_seconds: float = 15
    mq_rpc_timeout_seconds: float = 30
    lock_rpc_timeout_seconds: float = 5
    safety_margin_seconds: float = 10

    def __post_init__(self) -> None:
        values = (
            self.visibility_seconds,
            self.lock_ttl_seconds,
            self.renew_interval_seconds,
            self.mq_rpc_timeout_seconds,
            self.lock_rpc_timeout_seconds,
            self.safety_margin_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("RocketMQ lease policy values must be positive")
        if self.visibility_seconds < 10:
            raise ValueError("RocketMQ Broker requires at least 10 seconds of invisibility")
        if self.lock_ttl_seconds + self.safety_margin_seconds >= self.visibility_seconds:
            raise ValueError("task lock TTL must expire before message becomes visible")
        renewal_budget = (
            self.renew_interval_seconds
            + self.mq_rpc_timeout_seconds
            + self.lock_rpc_timeout_seconds
            + self.safety_margin_seconds
        )
        if renewal_budget >= self.lock_ttl_seconds:
            raise ValueError("renewal timing can outlive the task lock TTL")


class _LeaseGuard:
    def __init__(
        self,
        consumer: Any,
        message: Any,
        policy: LeasePolicy,
        renew_lock: Callable[[Any, int], bool] | None,
        abort_process: Callable[[int], None],
    ) -> None:
        self._consumer = consumer
        self._message = message
        self._policy = policy
        self._renew_lock = renew_lock
        self._abort_process = abort_process
        self._stopped = threading.Event()
        self._state_lock = threading.Lock()
        self._safe_until = (
            time.monotonic() + policy.lock_ttl_seconds - policy.safety_margin_seconds
        )
        self._failure: BaseException | None = None
        self._renew_thread = threading.Thread(
            target=self._renew_loop, name="analysis-mq-lease-renew", daemon=True
        )
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="analysis-mq-lease-watchdog", daemon=True
        )

    def start(self) -> None:
        self._renew_thread.start()
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._renew_thread.join(
            timeout=self._policy.mq_rpc_timeout_seconds
            + self._policy.lock_rpc_timeout_seconds
            + 1
        )
        if self._renew_thread.is_alive():
            self._fail(TimeoutError("RocketMQ lease renewal did not stop"))
        self._watchdog_thread.join(timeout=1)

    def raise_if_lost(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise LeaseLostError("RocketMQ/lock lease was lost; message was not ACKed") from failure

    def _fail(self, error: BaseException) -> None:
        with self._state_lock:
            if self._failure is not None:
                return
            self._failure = error
        self._stopped.set()
        logger.critical("analysis worker lease lost; exiting before lock expiry", exc_info=error)
        # A callback running in the main thread cannot be safely killed by a
        # Python thread. Exit the entire worker process before the old lock
        # expires, so a later Broker redelivery cannot overlap its writes.
        self._abort_process(70)

    def _renew_loop(self) -> None:
        while not self._stopped.wait(self._policy.renew_interval_seconds):
            try:
                # SDK 5.1.2 synchronously replaces message.receipt_handle.
                self._consumer.renew(self._message, self._policy.visibility_seconds)
                if self._renew_lock is not None and not self._renew_lock(
                    self._message, self._policy.lock_ttl_seconds
                ):
                    raise LeaseLostError("task lock renewal was rejected")
                with self._state_lock:
                    self._safe_until = (
                        time.monotonic()
                        + self._policy.lock_ttl_seconds
                        - self._policy.safety_margin_seconds
                    )
            except BaseException as exc:
                self._fail(exc)
                return

    def _watchdog_loop(self) -> None:
        while not self._stopped.is_set():
            with self._state_lock:
                remaining = self._safe_until - time.monotonic()
            if remaining <= 0:
                self._fail(TimeoutError("task lock safety deadline reached"))
                return
            self._stopped.wait(min(remaining, 1.0))


class AnalysisConsumerWorker:
    def __init__(
        self,
        consumer: Any,
        handler: Callable[[Any], Disposition],
        *,
        policy: LeasePolicy | None = None,
        renew_lock: Callable[[Any, int], bool] | None = None,
        finalize: Callable[[Any], None] | None = None,
        abort_process: Callable[[int], None] = os._exit,
    ) -> None:
        self._consumer = consumer
        self._handler = handler
        self._policy = policy or LeasePolicy()
        self._renew_lock = renew_lock
        self._finalize = finalize
        self._abort_process = abort_process
        if consumer.invisible_duration != self._policy.visibility_seconds:
            raise ValueError("consumer invisibility must match the lease policy")
        if consumer.rpc_timeout_seconds > self._policy.mq_rpc_timeout_seconds:
            raise ValueError("consumer RPC timeout exceeds the lease timing budget")

    def run_once(self) -> Disposition | None:
        message = self._consumer.receive_one()
        if message is None:
            return None
        guard = _LeaseGuard(
            self._consumer,
            message,
            self._policy,
            self._renew_lock,
            self._abort_process,
        )
        try:
            guard.start()
            try:
                disposition = self._handler(message)
            except BaseException:
                guard.stop()
                guard.raise_if_lost()
                raise
            # Wait for an in-flight renew to finish before using the message's
            # latest receipt handle for ACK.
            guard.stop()
            guard.raise_if_lost()
            if not isinstance(disposition, Disposition):
                raise TypeError("analysis handler must return Disposition.ACK or RETRY")
            if disposition is Disposition.ACK:
                self._consumer.ack(message)
            return disposition
        finally:
            if self._finalize is not None:
                self._finalize(message)

    def run_forever(self, stop: threading.Event) -> None:
        self._consumer.startup()
        try:
            while not stop.is_set():
                try:
                    self.run_once()
                except LeaseLostError:
                    raise
                except Exception:
                    logger.exception("analysis message left unacknowledged for redelivery")
                    stop.wait(1)
        finally:
            self._consumer.shutdown()
