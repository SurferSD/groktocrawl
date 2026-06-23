"""Unit tests for the CircuitBreaker in common/circuit_breaker.py.

Covers all state transitions, fast-fail behavior, concurrent probe
handling, and configurable parameters.
"""

import asyncio
import time

import pytest

from common.circuit_breaker import CircuitBreaker, CircuitBreakerState, CircuitOpenError

# ── Basic state transitions ──────────────────────────────────────────────────


class TestStateTransitions:
    """Circuit breaker state transitions on success/failure."""

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self):
        """A new circuit breaker starts in CLOSED state."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        assert cb.state is CircuitBreakerState.CLOSED
        assert cb.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_closed_to_open_after_threshold_failures(self):
        """After N consecutive failures, the circuit transitions to OPEN."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        for _ in range(3):
            await cb.record_failure()

        assert cb.state is CircuitBreakerState.OPEN
        assert cb.consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_success_resets_failure_count_in_closed(self):
        """A success in CLOSED resets the consecutive failure counter."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()  # Resets
        assert cb.consecutive_failures == 0
        assert cb.state is CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_check_passes_in_closed(self):
        """check() does not raise in CLOSED state."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        await cb.check()  # Should not raise

    @pytest.mark.asyncio
    async def test_check_raises_in_open(self):
        """check() raises CircuitOpenError in OPEN state."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        with pytest.raises(CircuitOpenError):
            await cb.check()

    @pytest.mark.asyncio
    async def test_check_does_not_raise_after_cooldown(self):
        """check() does not raise after cooldown — transitions to HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        # Wait for cooldown to elapse
        await asyncio.sleep(0.06)
        await cb.check()  # Should transition to HALF_OPEN, not raise

        assert cb.state is CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_cooldown(self):
        """OPEN transitions to HALF_OPEN after cooldown."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        await asyncio.sleep(0.06)
        await cb.check()
        assert cb.state is CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_probe_success(self):
        """HALF_OPEN transitions to CLOSED after successful probe."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN
        assert cb.state is CircuitBreakerState.HALF_OPEN

        await cb.record_success()  # → CLOSED
        assert cb.state is CircuitBreakerState.CLOSED
        assert cb.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_to_open_on_probe_failure(self):
        """HALF_OPEN transitions back to OPEN after failed probe."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN

        await cb.record_failure()  # → OPEN again
        assert cb.state is CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Full CLOSED → OPEN → HALF_OPEN → CLOSED cycle."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.05)

        # CLOSED → OPEN after 3 failures
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.CLOSED  # Not yet
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        # OPEN → HALF_OPEN after cooldown
        await asyncio.sleep(0.06)
        await cb.check()
        assert cb.state is CircuitBreakerState.HALF_OPEN

        # HALF_OPEN → CLOSED on probe success
        await cb.record_success()
        assert cb.state is CircuitBreakerState.CLOSED

        # Normal flow resumes
        await cb.check()  # Should not raise


# ── Fast-fail behavior ──────────────────────────────────────────────────────


class TestFastFail:
    """CircuitBreaker fast-fails requests when OPEN."""

    @pytest.mark.asyncio
    async def test_fast_fail_returns_immediately(self):
        """A fast-failed request returns instantly (not after timeout)."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        await cb.record_failure()
        await cb.record_failure()

        start = time.monotonic()
        with pytest.raises(CircuitOpenError):
            await cb.check()
        elapsed = time.monotonic() - start

        # Should be sub-10ms — no actual network call is made
        assert elapsed < 0.01, f"Fast-fail took {elapsed * 1000:.1f}ms, expected <10ms"

    @pytest.mark.asyncio
    async def test_fast_fail_does_not_call_downstream(self):
        """While open, the underlying function is never called."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        await cb.record_failure()
        await cb.record_failure()

        call_count = 0

        async def downstream():
            nonlocal call_count
            call_count += 1
            return "ok"

        with pytest.raises(CircuitOpenError):
            await cb.check()

        # The downstream function should NOT have been called
        # (check() fast-fails before the caller attempts the call)
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_fast_fail_circuit_open_error_detail(self):
        """CircuitOpenError has proper status_code and detail."""
        err = CircuitOpenError()
        assert err.status_code == 503
        assert err.detail["error"] == "circuit_open"
        assert "message" in err.detail

    @pytest.mark.asyncio
    async def test_resumes_after_cooldown_and_probe(self):
        """After cooldown + successful probe, normal flow resumes."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        # After cooldown, check transitions to HALF_OPEN
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN
        await cb.record_success()  # → CLOSED

        # Now normal flow should work
        await cb.check()  # Should not raise


# ── HALF_OPEN probe behavior ────────────────────────────────────────────────


class TestHalfOpenProbe:
    """Exactly one probe is allowed in HALF_OPEN; concurrent requests are fast-failed."""

    @pytest.mark.asyncio
    async def test_one_probe_allowed(self):
        """Exactly one request passes check() in HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN, probe 1 passes

        # Second concurrent probe should be fast-failed
        with pytest.raises(CircuitOpenError):
            await cb.check()

    @pytest.mark.asyncio
    async def test_concurrent_probes_fast_failed(self):
        """Concurrent requests during probe window are fast-failed."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)

        # Simulate concurrent requests
        async def attempt_check():
            try:
                await cb.check()
                return "allowed"
            except CircuitOpenError:
                return "fast-failed"

        results = await asyncio.gather(*[attempt_check() for _ in range(5)])

        # Exactly one should be "allowed", the rest "fast-failed"
        allowed = [r for r in results if r == "allowed"]
        fast_failed = [r for r in results if r == "fast-failed"]

        assert len(allowed) == 1, f"Expected 1 allowed, got {len(allowed)}"
        assert len(fast_failed) == 4, f"Expected 4 fast-failed, got {len(fast_failed)}"

    @pytest.mark.asyncio
    async def test_probe_success_closes_circuit(self):
        """After a successful probe, the circuit closes."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN
        assert cb.state is CircuitBreakerState.HALF_OPEN

        await cb.record_success()  # → CLOSED
        assert cb.state is CircuitBreakerState.CLOSED

        # New requests should pass through
        await cb.check()

    @pytest.mark.asyncio
    async def test_probe_failure_reopens_circuit(self):
        """After a failed probe, the circuit re-opens."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN

        await cb.record_failure()  # Probe failed
        assert cb.state is CircuitBreakerState.OPEN

        # Should fast-fail now
        with pytest.raises(CircuitOpenError):
            await cb.check()

    @pytest.mark.asyncio
    async def test_probe_state_reset_on_new_open(self):
        """After probe failure re-opens circuit, next cooldown allows new probe."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.check()  # → HALF_OPEN
        await cb.record_failure()  # → OPEN again

        # Wait for cooldown again
        await asyncio.sleep(0.06)
        await cb.check()  # Should transition to HALF_OPEN again
        assert cb.state is CircuitBreakerState.HALF_OPEN


# ── Configuration ────────────────────────────────────────────────────────────


class TestConfiguration:
    """CircuitBreaker respects configurable parameters."""

    @pytest.mark.asyncio
    async def test_custom_failure_threshold(self):
        """failure_threshold determines when circuit opens."""
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        assert cb.state is CircuitBreakerState.CLOSED
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN  # Opens after just 1 failure

    @pytest.mark.asyncio
    async def test_high_threshold(self):
        """A high failure threshold requires many failures to open."""
        cb = CircuitBreaker(failure_threshold=10, cooldown_seconds=60)
        for _ in range(9):
            await cb.record_failure()
        assert cb.state is CircuitBreakerState.CLOSED  # Not yet open
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN  # Now open

    @pytest.mark.asyncio
    async def test_custom_cooldown(self):
        """cooldown_seconds controls how long circuit stays open."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        # Should still be open before cooldown
        with pytest.raises(CircuitOpenError):
            await cb.check()

        await asyncio.sleep(0.12)
        await cb.check()  # Should not raise (cooldown elapsed)
        assert cb.state is CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_zero_cooldown_immediate_half_open_check(self):
        """Zero cooldown means immediate transition to HALF_OPEN on check()."""
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.0)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state is CircuitBreakerState.OPEN

        # With zero cooldown, check() should immediately transition
        await cb.check()
        assert cb.state is CircuitBreakerState.HALF_OPEN


# ── Reusability (design intent) ─────────────────────────────────────────────


class TestReusability:
    """CircuitBreaker is not coupled to any specific service."""

    @pytest.mark.asyncio
    async def test_multiple_independent_breakers(self):
        """Multiple circuit breakers operate independently."""
        cb1 = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb2 = CircuitBreaker(failure_threshold=5, cooldown_seconds=60)

        # Fail cb1 but not cb2
        await cb1.record_failure()
        await cb1.record_failure()
        assert cb1.state is CircuitBreakerState.OPEN
        assert cb2.state is CircuitBreakerState.CLOSED  # Unaffected

        # cb2 still allows requests
        await cb2.check()  # Should not raise

    @pytest.mark.asyncio
    async def test_works_with_any_async_caller_pattern(self):
        """The check/record_success/record_failure API works with any caller."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)

        # Simulate a caller that uses check() before making a call
        # and record_{success,failure} after

        async def make_call(succeed: bool):
            await cb.check()
            if not succeed:
                await cb.record_failure()
                raise RuntimeError("call failed")
            await cb.record_success()
            return "ok"

        # Success
        result = await make_call(succeed=True)
        assert result == "ok"
        assert cb.state is CircuitBreakerState.CLOSED

        # Two failures
        with pytest.raises(RuntimeError):
            await make_call(succeed=False)
        with pytest.raises(RuntimeError):
            await make_call(succeed=False)
        assert cb.state is CircuitBreakerState.CLOSED  # threshold=3

        # Third failure → circuit opens
        with pytest.raises(RuntimeError):
            await make_call(succeed=False)
        assert cb.state is CircuitBreakerState.OPEN

        # Next call is fast-failed
        with pytest.raises(CircuitOpenError):
            await make_call(succeed=True)  # Would succeed but circuit is open
