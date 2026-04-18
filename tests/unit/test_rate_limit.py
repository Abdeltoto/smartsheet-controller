"""Unit tests for backend.rate_limit (TokenBucket + SessionRateLimiter)."""
import time

import pytest

from backend.rate_limit import (
    LIMITS,
    SessionRateLimiter,
    TokenBucket,
    check_limit,
)

pytestmark = pytest.mark.unit


class TestTokenBucket:
    def test_starts_full(self):
        b = TokenBucket(capacity=5, refill_per_sec=1.0)
        assert b.tokens == 5.0

    def test_consume_within_capacity(self):
        b = TokenBucket(capacity=3, refill_per_sec=1.0)
        assert b.try_consume(1) is True
        assert b.try_consume(1) is True
        assert b.try_consume(1) is True
        assert b.try_consume(1) is False  # bucket empty, no refill yet

    def test_partial_cost(self):
        b = TokenBucket(capacity=2, refill_per_sec=1.0)
        assert b.try_consume(0.5) is True
        assert b.try_consume(1.5) is True
        assert b.try_consume(0.1) is False

    def test_refills_over_time(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        b = TokenBucket(capacity=4, refill_per_sec=2.0)
        # Drain
        for _ in range(4):
            assert b.try_consume(1) is True
        assert b.try_consume(1) is False
        # Advance 1s -> +2 tokens
        clock["t"] += 1.0
        assert b.try_consume(1) is True
        assert b.try_consume(1) is True
        assert b.try_consume(1) is False

    def test_refill_capped_at_capacity(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        b = TokenBucket(capacity=3, refill_per_sec=10.0)
        # Don't consume anything; advance 100s. Tokens must NOT exceed capacity.
        clock["t"] += 100.0
        assert b.try_consume(3) is True
        assert b.try_consume(0.1) is False

    def test_wait_seconds(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        b = TokenBucket(capacity=2, refill_per_sec=2.0)
        b.try_consume(2)
        assert b.try_consume(1) is False
        assert b.wait_seconds(1) == pytest.approx(0.5, rel=1e-2)


class TestSessionRateLimiter:
    def test_isolated_per_session(self):
        srl = SessionRateLimiter()
        ok_a, _ = srl.check("sess-a", "ws", capacity=2, refill_per_sec=0.5)
        assert ok_a is True
        srl.check("sess-a", "ws", capacity=2, refill_per_sec=0.5)
        # Session A is now empty, but B has its own bucket.
        ok_b, _ = srl.check("sess-b", "ws", capacity=2, refill_per_sec=0.5)
        assert ok_b is True

    def test_exhausted_returns_retry_after(self):
        srl = SessionRateLimiter()
        for _ in range(3):
            srl.check("s", "llm", capacity=3, refill_per_sec=1.0)
        ok, retry = srl.check("s", "llm", capacity=3, refill_per_sec=1.0)
        assert ok is False
        assert retry > 0

    def test_clear_drops_buckets(self):
        srl = SessionRateLimiter()
        for _ in range(3):
            srl.check("victim", "ws", capacity=3, refill_per_sec=1.0)
        ok_before, _ = srl.check("victim", "ws", capacity=3, refill_per_sec=1.0)
        assert ok_before is False
        srl.clear("victim")
        ok_after, _ = srl.check("victim", "ws", capacity=3, refill_per_sec=1.0)
        assert ok_after is True


class TestCheckLimit:
    def test_known_kind_uses_limits_table(self):
        assert "llm" in LIMITS
        ok, retry = check_limit("test-session-known", "llm")
        assert isinstance(ok, bool)
        assert retry >= 0

    def test_unknown_kind_falls_back(self):
        ok, _ = check_limit("test-session-unknown", "this-kind-does-not-exist")
        assert ok is True
