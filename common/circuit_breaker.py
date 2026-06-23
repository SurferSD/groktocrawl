"""Circuit breaker pattern for resilience against cascading failures.

Provides a reusable :class:`CircuitBreaker` that monitors consecutive
failures (e.g. 5xx HTTP responses) and temporarily short-circuits
requests to give the downstream service time to recover.

States
------
CLOSED
    Normal operation — all requests pass through. Failures increment an
    internal counter; when the counter reaches *failure_threshold* the
    circuit transitions to OPEN.

OPEN
    Requests are fast-failed with a :class:`CircuitOpenError` without
    making an HTTP call. After *cooldown_seconds* the circuit transitions
    to HALF_OPEN.

HALF_OPEN
    Exactly one probe request is allowed through. Concurrent requests
    during the probe window are fast-failed. If the probe succeeds (caller
    calls :meth:`record_success`) the circuit closes; if it fails
    (:meth:`record_failure`) the circuit re-opens.

Usage
-----
.. code-block:: python

    from common.circuit_breaker import CircuitBreaker, CircuitOpenError

    cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=30)

    async def proxy_request():
        await cb.check()              # may raise CircuitOpenError
        try:
            response = await http_client.get(...)
            if response.status_code >= 500:
                await cb.record_failure()
            else:
                await cb.record_success()
            return response
        except httpx.ConnectError:
            await cb.record_failure()
            raise
"""

import asyncio
import enum
import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreakerState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised when the circuit is open and a request is fast-failed.

    Attributes:
        status_code: HTTP status code suitable for upstream error responses (always 503).
        detail: Structured error detail for API consumers.
    """

    status_code = 503

    def __init__(self, message: str = "Service temporarily unavailable"):
        self.detail = {"error": "circuit_open", "message": message}
        super().__init__(message)


class CircuitBreaker:
    """Async circuit breaker with configurable threshold and cooldown.

    Thread-safe via ``asyncio.Lock``. Designed for reuse across all
    GroktoCrawl services.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

        self._state = CircuitBreakerState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._probe_in_progress = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # ── Public API ────────────────────────────────────────────────────────

    async def check(self) -> None:
        """Check if the circuit allows a request through.

        Raises:
            CircuitOpenError: If the circuit is OPEN (fast-fail) or
                HALF_OPEN with a probe already in progress.
        """
        async with self._lock:
            if self._state is CircuitBreakerState.CLOSED:
                return

            if self._state is CircuitBreakerState.OPEN:
                # Check if cooldown has elapsed → transition to HALF_OPEN
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.cooldown_seconds:
                    logger.info(
                        "Circuit breaker: OPEN -> HALF_OPEN after %.1fs cooldown",
                        elapsed,
                    )
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._probe_in_progress = False
                else:
                    raise CircuitOpenError(
                        f"Circuit is open, {self.cooldown_seconds - elapsed:.0f}s remaining"
                    )

            if self._state is CircuitBreakerState.HALF_OPEN:
                if not self._probe_in_progress:
                    self._probe_in_progress = True
                    logger.info("Circuit breaker: allowing probe request in HALF_OPEN")
                    return  # Allow this single probe
                raise CircuitOpenError("Circuit is half-open, probe in progress")

    async def record_success(self) -> None:
        """Record a successful request.

        In HALF_OPEN: transitions to CLOSED and resets failures.
        In CLOSED: resets the consecutive failure counter.
        """
        async with self._lock:
            if self._state is CircuitBreakerState.HALF_OPEN:
                logger.info("Circuit breaker: HALF_OPEN -> CLOSED (probe succeeded)")
                self._state = CircuitBreakerState.CLOSED
                self._consecutive_failures = 0
                self._probe_in_progress = False
            elif self._state is CircuitBreakerState.CLOSED:
                self._consecutive_failures = 0
            # In OPEN state this call is a no-op (should not normally happen)

    async def record_failure(self) -> None:
        """Record a failed request.

        In HALF_OPEN: transitions back to OPEN.
        In CLOSED: increments failure counter; opens circuit if threshold
        reached.
        """
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state is CircuitBreakerState.HALF_OPEN:
                logger.warning(
                    "Circuit breaker: HALF_OPEN -> OPEN (probe failed, %d consecutive failures)",
                    self._consecutive_failures,
                )
                self._state = CircuitBreakerState.OPEN
                self._probe_in_progress = False
            elif self._state is CircuitBreakerState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    logger.warning(
                        "Circuit breaker: CLOSED -> OPEN after %d consecutive failures",
                        self._consecutive_failures,
                    )
                    self._state = CircuitBreakerState.OPEN
            # In OPEN state: just update the timestamp (stays open)
