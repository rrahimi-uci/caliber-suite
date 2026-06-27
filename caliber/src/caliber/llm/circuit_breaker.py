"""Circuit breaker decorator around :class:`LLMProvider`.

The breaker isolates the orchestrator from a misbehaving LLM provider.
Without it, a single LLM outage burns the retry budget of every job in
the queue: each tick claims a job, calls the failing provider, marks
the job ``failed``, and moves on. Within minutes the queue drains into
a failed pile that has to be manually re-queued once the provider
recovers.

With the breaker in place, repeated failures within a sliding window
trip the circuit ``OPEN``. While open, calls fail fast with
:class:`LLMCircuitOpenError` instead of touching the provider. The
worker recognizes this distinct exception type and re-queues the job
(status='queued') rather than marking it failed — the "defer jobs
without consuming retry budget" semantic from parity checklist §5.24.

After ``open_duration_seconds`` elapses the breaker transitions to
``HALF_OPEN`` and lets exactly one call through as a probe. Success
closes the circuit; failure re-opens it for another full cooldown.

State machine
-------------

::

    CLOSED   ──(failures ≥ threshold in window)──▶  OPEN
    OPEN     ──(open_duration elapsed)──────────▶  HALF_OPEN
    HALF_OPEN──(probe call succeeds)─────────────▶  CLOSED
    HALF_OPEN──(probe call fails)────────────────▶  OPEN

Design notes
------------

* Per-provider circuit (one wrapper instance per app) rather than
  per-agent. The failure mode we're protecting against — provider
  outage, auth failure, rate limit — is global to the provider, not
  scoped to a particular agent.
* Both :meth:`diagnose` and :meth:`generate_candidate` share the same
  circuit. Either call type's failure trips the breaker; either call
  type can serve as the half-open probe.
* :class:`LLMCircuitOpenError` subclasses :class:`LLMProviderError` so
  call sites that catch the generic error type continue to work; the
  worker catches the specific subclass *first* for the re-queue path.
* Thread-safe via a single :class:`threading.Lock`. The worker calls
  the provider from ``asyncio.to_thread``; the lock protects against
  concurrent failures racing the state transition. Holding the lock
  across the actual LLM call would serialize every request, so we
  release it before delegating and re-acquire it to record the
  outcome.
* Failure history is a deque trimmed each access — bounded by the
  threshold, so memory is O(threshold) not O(failures-ever).
* Time source is parameterized (``time_source`` callable) so tests can
  pin the wall clock without monkeypatching ``time``.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Final

from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMProvider,
    LLMProviderError,
    LLMUsage,
    PromptCandidate,
    TriageClassification,
    TriageContext,
    WorkflowEdit,
    WorkflowEditContext,
    WorkflowGenerationContext,
)

logger = logging.getLogger("caliber.llm.circuit_breaker")


class CircuitState(str, enum.Enum):
    """Three-state circuit per the classic Nygard breaker pattern."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class LLMCircuitOpenError(LLMProviderError):
    """Raised by :class:`CircuitBreakerLLMProvider` when the circuit is open.

    Subclasses :class:`LLMProviderError` so existing ``except
    LLMProviderError`` blocks continue to work. The worker catches
    this subclass *first* to take the re-queue path instead of the
    mark-failed path.
    """


# Sentinel time source — module-level so we don't allocate a closure
# per wrapper instance.
def _wall_clock() -> float:
    return time.monotonic()


_DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
_DEFAULT_WINDOW_SECONDS: Final[float] = 60.0
_DEFAULT_OPEN_DURATION_SECONDS: Final[float] = 30.0


class CircuitBreakerLLMProvider:
    """Wrap an :class:`LLMProvider` with a circuit breaker.

    The wrapper preserves the Protocol shape: both ``diagnose`` and
    ``generate_candidate`` have the same signatures and return types
    as the inner provider. Callers don't know the breaker exists
    unless they catch :class:`LLMCircuitOpenError` specifically.

    Parameters
    ----------
    inner:
        The provider to wrap (typically the result of one of the
        ``build_provider`` branches before the wrapper is applied).
    failure_threshold:
        Number of failures within ``window_seconds`` that trips the
        circuit. Inclusive — exactly ``failure_threshold`` failures
        opens the breaker.
    window_seconds:
        Rolling window for failure counting. Failures older than
        ``now - window_seconds`` are forgotten so a slow trickle of
        failures over hours doesn't accumulate into a trip.
    open_duration_seconds:
        Time the circuit stays ``OPEN`` before transitioning to
        ``HALF_OPEN`` and letting a probe call through.
    time_source:
        Callable returning a monotonic float. Parameterized for tests;
        defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        inner: LLMProvider,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        open_duration_seconds: float = _DEFAULT_OPEN_DURATION_SECONDS,
        time_source: Callable[[], float] = _wall_clock,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if open_duration_seconds <= 0:
            raise ValueError("open_duration_seconds must be > 0")

        self._inner = inner
        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._open_duration_seconds = open_duration_seconds
        self._now = time_source

        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        # Bounded deque so a long-lived CLOSED circuit doesn't grow
        # without bound. ``maxlen`` is threshold so we keep enough
        # history to evaluate the trip condition; older entries fall
        # off automatically.
        self._failures: deque[float] = deque(maxlen=failure_threshold)
        self._opened_at: float | None = None
        # HALF_OPEN admits exactly one probe call. Without this flag,
        # concurrent calls arriving after the OPEN→HALF_OPEN transition
        # would all observe ``state is HALF_OPEN`` (no rejection branch)
        # and stampede the still-unhealthy provider. The flag is set
        # under the lock by whichever call promotes the state, and
        # cleared on the next ``_after_success`` / ``_after_failure``.
        self._probe_in_flight: bool = False

    # ------------------------------------------------------------------
    # Public state introspection (used by tests and the future
    # `/caliber/health` integration).
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current state without forcing a transition check.

        Tests use this; the runtime calls :meth:`_before_call` instead
        so the state is recomputed (e.g. OPEN→HALF_OPEN promotion)
        before any decision is made.
        """
        with self._lock:
            return self._state

    # ------------------------------------------------------------------
    # LLMProvider Protocol surface.
    # ------------------------------------------------------------------

    def classify_triage(self, context: TriageContext) -> tuple[TriageClassification, LLMUsage]:
        self._before_call()
        try:
            result = self._inner.classify_triage(context)
        except Exception:
            # Any failure (including non-LLMProviderError, e.g. a malformed-output
            # ValidationError during a HALF_OPEN probe) must release the probe slot
            # so the breaker can recover; otherwise it wedges open permanently.
            self._after_failure()
            raise
        self._after_success()
        return result

    def diagnose(self, evidence: EvidenceContext) -> tuple[Diagnosis, LLMUsage]:
        self._before_call()
        try:
            result = self._inner.diagnose(evidence)
        except Exception:
            # Any failure (including non-LLMProviderError, e.g. a malformed-output
            # ValidationError during a HALF_OPEN probe) must release the probe slot
            # so the breaker can recover; otherwise it wedges open permanently.
            self._after_failure()
            raise
        self._after_success()
        return result

    def generate_candidate(self, context: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
        self._before_call()
        try:
            result = self._inner.generate_candidate(context)
        except Exception:
            # Any failure (including non-LLMProviderError, e.g. a malformed-output
            # ValidationError during a HALF_OPEN probe) must release the probe slot
            # so the breaker can recover; otherwise it wedges open permanently.
            self._after_failure()
            raise
        self._after_success()
        return result

    def propose_workflow_edit(self, context: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        self._before_call()
        try:
            result = self._inner.propose_workflow_edit(context)
        except Exception:
            # Any failure (including non-LLMProviderError, e.g. a malformed-output
            # ValidationError during a HALF_OPEN probe) must release the probe slot
            # so the breaker can recover; otherwise it wedges open permanently.
            self._after_failure()
            raise
        self._after_success()
        return result

    def generate_workflow_from_goal(
        self, context: WorkflowGenerationContext
    ) -> tuple[WorkflowEdit, LLMUsage]:
        self._before_call()
        try:
            result = self._inner.generate_workflow_from_goal(context)
        except Exception:
            # Any failure (including non-LLMProviderError, e.g. a malformed-output
            # ValidationError during a HALF_OPEN probe) must release the probe slot
            # so the breaker can recover; otherwise it wedges open permanently.
            self._after_failure()
            raise
        self._after_success()
        return result

    # ------------------------------------------------------------------
    # State machine internals.
    # ------------------------------------------------------------------

    def _before_call(self) -> None:
        """Check the breaker before delegating; raise if it's open.

        Promotes ``OPEN`` to ``HALF_OPEN`` when the cooldown has
        elapsed so the next call goes through as a probe. Only one
        probe is admitted at a time; concurrent callers arriving
        while a probe is in flight are rejected as if the circuit
        were still open.
        """
        with self._lock:
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None  # for type-checker
                if self._now() - self._opened_at >= self._open_duration_seconds:
                    logger.info("llm circuit half-open after cooldown")
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    return
                raise LLMCircuitOpenError(
                    "llm provider circuit is open; call rejected without delegation"
                )
            if self._state is CircuitState.HALF_OPEN:
                # A probe is already in flight (whoever promoted the
                # state holds the slot). Reject so we don't stampede
                # a still-unhealthy provider with concurrent probes.
                if self._probe_in_flight:
                    raise LLMCircuitOpenError(
                        "llm provider circuit is half-open; probe in flight, call rejected"
                    )
                # Defensive: HALF_OPEN with no probe in flight should
                # not occur because every state-machine transition
                # in/out of HALF_OPEN sets or clears the flag. Treat
                # this call as the probe rather than admitting silently.
                self._probe_in_flight = True

    def _after_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                logger.info("llm circuit closing after successful probe")
                self._state = CircuitState.CLOSED
                self._failures.clear()
                self._opened_at = None
                self._probe_in_flight = False
            elif self._state is CircuitState.CLOSED:
                # A successful call doesn't clear prior failures —
                # they age out via the sliding window. This way a
                # provider that flaps "fail, fail, success, fail,
                # fail" still trips the breaker when the failure
                # density crosses the threshold.
                pass

    def _after_failure(self) -> None:
        with self._lock:
            now = self._now()
            if self._state is CircuitState.HALF_OPEN:
                logger.warning("llm circuit re-opening; probe call failed")
                self._state = CircuitState.OPEN
                self._opened_at = now
                # Reset the deque so the next cooldown→probe cycle
                # starts fresh.
                self._failures.clear()
                self._probe_in_flight = False
                return

            # CLOSED path: append the failure timestamp, trim
            # everything older than the window, then evaluate the trip
            # condition against what's left.
            self._failures.append(now)
            cutoff = now - self._window_seconds
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()

            if len(self._failures) >= self._failure_threshold:
                logger.warning(
                    "llm circuit opening: %d failures within %.1fs window",
                    len(self._failures),
                    self._window_seconds,
                )
                self._state = CircuitState.OPEN
                self._opened_at = now


def maybe_wrap(
    provider: LLMProvider,
    *,
    enabled: bool,
    failure_threshold: int,
    window_seconds: float,
    open_duration_seconds: float,
) -> LLMProvider:
    """Conditionally wrap ``provider`` with a circuit breaker.

    Centralizes the "is the breaker enabled?" branch so
    :func:`caliber.llm.provider.build_provider` stays a single
    expression and tests can construct wrapped providers without
    going through the config object.
    """
    if not enabled:
        return provider
    return CircuitBreakerLLMProvider(
        provider,
        failure_threshold=failure_threshold,
        window_seconds=window_seconds,
        open_duration_seconds=open_duration_seconds,
    )
