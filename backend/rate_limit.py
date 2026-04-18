"""Per-session token-bucket rate limiter.

Lightweight in-memory implementation suitable for single-process deployments.
For multi-worker deployments, replace with Redis-backed bucket.
"""
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    capacity: int
    refill_per_sec: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()

    def try_consume(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def wait_seconds(self, cost: float = 1.0) -> float:
        """Seconds until enough tokens exist for `cost`."""
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / self.refill_per_sec


class SessionRateLimiter:
    """Holds one or more named buckets per session."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], TokenBucket] = {}

    def _get(self, session_id: str, name: str, capacity: int, refill_per_sec: float) -> TokenBucket:
        key = (session_id, name)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity=capacity, refill_per_sec=refill_per_sec)
            self._buckets[key] = bucket
        return bucket

    def check(self, session_id: str, name: str, capacity: int, refill_per_sec: float, cost: float = 1.0) -> tuple[bool, float]:
        bucket = self._get(session_id, name, capacity, refill_per_sec)
        allowed = bucket.try_consume(cost)
        retry_after = 0.0 if allowed else bucket.wait_seconds(cost)
        return allowed, retry_after

    def clear(self, session_id: str) -> None:
        keys = [k for k in self._buckets if k[0] == session_id]
        for k in keys:
            self._buckets.pop(k, None)


rate_limiter = SessionRateLimiter()

LIMITS = {
    "llm": (60, 1.0),
    "ws": (120, 2.0),
}


def check_limit(session_id: str, kind: str) -> tuple[bool, float]:
    capacity, refill = LIMITS.get(kind, (60, 1.0))
    return rate_limiter.check(session_id, kind, capacity, refill)
