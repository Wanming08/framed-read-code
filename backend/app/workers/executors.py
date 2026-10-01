"""Bounded task pools corresponding to the four original Spring executors."""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable


class RejectedExecution(RuntimeError):
    pass


class BoundedExecutor:
    def __init__(self, prefix: str, *, max_workers: int, queue_capacity: int):
        if max_workers < 1 or queue_capacity < 0:
            raise ValueError("invalid executor bounds")
        self.max_workers = max_workers
        self.queue_capacity = queue_capacity
        self._slots = threading.BoundedSemaphore(max_workers + queue_capacity)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=prefix)
        self._futures: set[Future] = set()
        self._lock = threading.Lock()
        self._closing = False

    def submit(self, function: Callable, *args, **kwargs) -> Future:
        with self._lock:
            if self._closing:
                raise RejectedExecution("executor is shutting down")
            if not self._slots.acquire(blocking=False):
                raise RejectedExecution("executor worker and queue capacity exhausted")
            try:
                future = self._pool.submit(function, *args, **kwargs)
            except Exception:
                self._slots.release()
                raise
            self._futures.add(future)
        # add_done_callback can run immediately for a completed task. Never
        # register it while holding the lock that _on_done itself acquires.
        future.add_done_callback(self._on_done)
        return future

    def _on_done(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
            self._slots.release()

    def shutdown(self, *, timeout: float = 30) -> bool:
        with self._lock:
            self._closing = True
            pending = set(self._futures)
        _completed, not_done = wait(pending, timeout=timeout)
        self._pool.shutdown(wait=not bool(not_done), cancel_futures=False)
        return not not_done


@dataclass
class ExecutorPools:
    ai: BoundedExecutor
    asr: BoundedExecutor
    ocr: BoundedExecutor
    model: BoundedExecutor

    def shutdown(self, *, timeout: float = 30) -> bool:
        results = [pool.shutdown(timeout=timeout) for pool in (self.ai, self.asr, self.ocr, self.model)]
        return all(results)


def create_default_executors(cpu_count: int | None = None) -> ExecutorPools:
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    ocr_cores = min(8, max(1, cores // 2))
    return ExecutorPools(
        ai=BoundedExecutor("AI-Thread-", max_workers=8, queue_capacity=100),
        asr=BoundedExecutor("ASR-Thread-", max_workers=8, queue_capacity=50),
        ocr=BoundedExecutor("OCR-Thread-", max_workers=ocr_cores, queue_capacity=20),
        model=BoundedExecutor("LLM-Thread-", max_workers=8, queue_capacity=20),
    )
