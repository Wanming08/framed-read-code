"""Atomic Redis locks and the original rolling permit behavior.

The Java backend uses Redisson-specific Redis formats. These Python keys are
valid only after the Java workers are stopped and the identified lock/limiter
state has been retired. No two runtimes may coordinate through mixed formats.
"""

from __future__ import annotations

import secrets
from typing import Any


RENEW_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

ACQUIRE_PERMIT = """
if redis.call('hget', KEYS[1], 'type') ~= false then
    return -1
end
redis.call('hsetnx', KEYS[1], 'rate', ARGV[1])
redis.call('hsetnx', KEYS[1], 'interval', ARGV[2])
local rate = tonumber(redis.call('hget', KEYS[1], 'rate'))
local interval = tonumber(redis.call('hget', KEYS[1], 'interval'))
local stamp = redis.call('time')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
redis.call('zremrangebyscore', KEYS[2], '-inf', now - interval)
if redis.call('zcard', KEYS[2]) >= rate then
    return 0
end
redis.call('zadd', KEYS[2], 'NX', now, ARGV[3])
return 1
"""


class LimiterStateError(RuntimeError):
    """Existing Redisson rate state needs an explicit controlled cutover."""


class LeaseLock:
    def __init__(self, redis_client: Any, key: str, *, lease_ms: int) -> None:
        if lease_ms < 1:
            raise ValueError("lease_ms must be positive")
        self.redis = redis_client
        self.key = key
        self.lease_ms = lease_ms
        self.token = secrets.token_urlsafe(24)

    def acquire(self) -> bool:
        return bool(self.redis.set(self.key, self.token, nx=True, px=self.lease_ms))

    def renew(self) -> bool:
        return bool(self.redis.eval(RENEW_IF_OWNER, 1, self.key, self.token, self.lease_ms))

    def release(self) -> bool:
        return bool(self.redis.eval(RELEASE_IF_OWNER, 1, self.key, self.token))


class SlidingWindowLimiter:
    """One permit per timestamp, matching RRateLimiter OVERALL for these calls.

    Java only calls tryAcquire() for one permit and never release(). The
    original Redisson algorithm counts unexpired permits in a rolling interval;
    a Redis Lua transaction gives the same admission decisions on fresh state.
    """

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    def try_acquire(self, key: str, *, rate: int, interval_ms: int = 60_000) -> bool:
        if rate < 1 or interval_ms < 1:
            raise ValueError("rate and interval_ms must be positive")
        result = int(
            self.redis.eval(
                ACQUIRE_PERMIT,
                2,
                key,
                key + ":permits",
                rate,
                interval_ms,
                secrets.token_hex(16),
            )
        )
        if result == -1:
            raise LimiterStateError("Redisson limiter state requires a controlled cutover")
        return result == 1

    def try_acquire_pair(
        self,
        user_key: str,
        user_rate: int,
        global_key: str,
        global_rate: int,
        *,
        interval_ms: int = 60_000,
    ) -> bool:
        if not self.try_acquire(user_key, rate=user_rate, interval_ms=interval_ms):
            return False
        return self.try_acquire(global_key, rate=global_rate, interval_ms=interval_ms)
