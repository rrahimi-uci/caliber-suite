"""Tests for the LLM provider circuit breaker.

Four layers:

1. State machine — pin each transition with a clock-pinned breaker.
2. Sliding window — failures outside the window don't count.
3. Per-call delegation — the wrapper forwards inputs and outputs
   unchanged when the circuit is closed.
4. Half-open semantics — exactly one probe through, then success
   closes / failure re-opens for another full cooldown.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from caliber.llm.circuit_breaker import (
    CircuitBreakerLLMProvider,
    CircuitState,
    LLMCircuitOpenError,
    maybe_wrap,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMProviderError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(item_id: str = "item-1") -> EvidenceContext:
    return EvidenceContext(
        agent_id="agent-a",
        item_id=item_id,
        category="quality",
        severity="medium",
        free_text="reviewer feedback",
    )


def _candidate_ctx() -> CandidateContext:
    return CandidateContext(
        agent_id="agent-a",
        job_id="job-1",
        artifact_type="prompt",
        optimizer_type="metaprompt",
        diagnosis=Diagnosis(root_cause="x", confidence=0.5),
    )


class _Clock:
    """Monotonic clock stub. Tests advance ``now`` manually."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _always_raise_diagnose(_: EvidenceContext) -> tuple[Diagnosis, object]:
    raise LLMProviderError("simulated upstream failure")


def _make_failing_fake() -> FakeLLMProvider:
    """A fake whose diagnose() always raises ``LLMProviderError``."""
    fake = FakeLLMProvider()
    fake.diagnose_callable = _always_raise_diagnose  # type: ignore[assignment]
    return fake


# ---------------------------------------------------------------------------
# Closed → Open
# ---------------------------------------------------------------------------


def test_closed_circuit_passes_calls_through() -> None:
    """When closed, the wrapper delegates to the inner provider transparently."""
    inner = FakeLLMProvider()
    wrapper = CircuitBreakerLLMProvider(inner, failure_threshold=3)
    diagnosis, _usage = wrapper.diagnose(_evidence())
    assert diagnosis.confidence == 0.75  # from the fake's default
    assert wrapper.state is CircuitState.CLOSED
    assert len(inner.diagnose_calls) == 1


def test_threshold_failures_trip_breaker_open() -> None:
    """Hitting the threshold in-window flips the circuit to OPEN."""
    clock = _Clock()
    wrapper = CircuitBreakerLLMProvider(
        _make_failing_fake(),
        failure_threshold=3,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(3):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN


def test_below_threshold_does_not_trip() -> None:
    """One under threshold leaves the circuit closed."""
    clock = _Clock()
    wrapper = CircuitBreakerLLMProvider(
        _make_failing_fake(),
        failure_threshold=3,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.CLOSED


def test_open_circuit_rejects_without_delegation() -> None:
    """Once OPEN the wrapper raises LLMCircuitOpenError without calling the inner provider."""
    clock = _Clock()
    inner = _make_failing_fake()
    wrapper = CircuitBreakerLLMProvider(
        inner,
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN

    call_count_before = len(inner.diagnose_calls)
    with pytest.raises(LLMCircuitOpenError):
        wrapper.diagnose(_evidence())
    # Fast-fail: inner was NOT consulted.
    assert len(inner.diagnose_calls) == call_count_before


def test_llm_circuit_open_error_is_llm_provider_error() -> None:
    """Subclass relationship so generic ``except LLMProviderError`` blocks still catch."""
    assert issubclass(LLMCircuitOpenError, LLMProviderError)


def test_non_llm_error_during_probe_does_not_wedge_breaker() -> None:
    """Regression: a non-LLMProviderError raised during the HALF_OPEN probe must
    still release the probe slot, so the breaker can recover. Previously the
    wrapper only caught ``LLMProviderError``, leaving ``_probe_in_flight`` set
    forever and rejecting every later call (even against a healthy provider)."""
    clock = _Clock()
    inner = FakeLLMProvider()
    calls = {"n": 0}

    def diagnose(_: EvidenceContext) -> tuple[Diagnosis, object]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise LLMProviderError("trip the breaker")
        if calls["n"] == 3:
            # The HALF_OPEN probe raises a NON-LLMProviderError (e.g. a malformed
            # model output surfacing as ValueError / pydantic.ValidationError).
            raise ValueError("malformed output during probe")
        return Diagnosis(root_cause="ok", confidence=0.9), object()

    inner.diagnose_callable = diagnose  # type: ignore[assignment]
    wrapper = CircuitBreakerLLMProvider(
        inner,
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )

    # Trip OPEN with two LLMProviderError failures.
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN

    # Cooldown elapses → next call is the probe, which raises a raw ValueError.
    clock.advance(31.0)
    with pytest.raises(ValueError):
        wrapper.diagnose(_evidence())
    # The failed probe must have re-OPENed the breaker (not stuck HALF_OPEN with
    # the probe slot held).
    assert wrapper.state is CircuitState.OPEN

    # After another full cooldown the (now healthy) provider must be reachable
    # again — i.e. the breaker recovered rather than wedging forever.
    clock.advance(31.0)
    diagnosis, _usage = wrapper.diagnose(_evidence())
    assert diagnosis.root_cause == "ok"
    assert wrapper.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------


def test_failures_outside_window_do_not_count() -> None:
    """Two failures, advance past window, two more — still under threshold."""
    clock = _Clock()
    wrapper = CircuitBreakerLLMProvider(
        _make_failing_fake(),
        failure_threshold=3,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    # Age out the first two failures.
    clock.advance(61.0)
    with pytest.raises(LLMProviderError):
        wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Open → Half-Open → Closed
# ---------------------------------------------------------------------------


def test_half_open_after_cooldown_admits_probe() -> None:
    """After ``open_duration_seconds`` the breaker lets one call through."""
    clock = _Clock()
    inner = FakeLLMProvider()
    # First make the inner provider fail enough to open the circuit, then
    # swap it to a working provider so the probe succeeds.
    failing: Callable[[EvidenceContext], tuple[Diagnosis, object]] = _always_raise_diagnose
    inner.diagnose_callable = failing  # type: ignore[assignment]
    wrapper = CircuitBreakerLLMProvider(
        inner,
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN

    # Provider recovered.
    inner.diagnose_callable = None  # back to canned response
    clock.advance(31.0)
    diagnosis, _usage = wrapper.diagnose(_evidence())
    assert diagnosis.confidence == 0.75
    assert wrapper.state is CircuitState.CLOSED


def test_half_open_probe_failure_reopens_circuit() -> None:
    """A failing probe re-opens the circuit for another full cooldown."""
    clock = _Clock()
    wrapper = CircuitBreakerLLMProvider(
        _make_failing_fake(),
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN

    # Cooldown elapses; the probe fails and re-opens.
    clock.advance(31.0)
    with pytest.raises(LLMProviderError):
        wrapper.diagnose(_evidence())
    assert wrapper.state is CircuitState.OPEN

    # Without another full cooldown, calls reject immediately.
    with pytest.raises(LLMCircuitOpenError):
        wrapper.diagnose(_evidence())


def test_open_circuit_admits_no_calls_before_cooldown() -> None:
    """Within the cooldown, every call short-circuits."""
    clock = _Clock()
    wrapper = CircuitBreakerLLMProvider(
        _make_failing_fake(),
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            wrapper.diagnose(_evidence())

    # Several calls before the cooldown elapses — all short-circuit.
    for _ in range(3):
        clock.advance(5.0)
        with pytest.raises(LLMCircuitOpenError):
            wrapper.diagnose(_evidence())


# ---------------------------------------------------------------------------
# Shared circuit across methods
# ---------------------------------------------------------------------------


def test_diagnose_and_candidate_share_circuit() -> None:
    """A failure in ``diagnose`` counts toward the threshold for ``generate_candidate``."""
    clock = _Clock()
    inner = FakeLLMProvider()
    inner.diagnose_callable = _always_raise_diagnose  # type: ignore[assignment]

    def _failing_candidate(_: CandidateContext) -> tuple[object, object]:
        raise LLMProviderError("candidate failure")

    inner.candidate_callable = _failing_candidate  # type: ignore[assignment]
    wrapper = CircuitBreakerLLMProvider(
        inner,
        failure_threshold=2,
        window_seconds=60.0,
        open_duration_seconds=30.0,
        time_source=clock,
    )
    with pytest.raises(LLMProviderError):
        wrapper.diagnose(_evidence())
    with pytest.raises(LLMProviderError):
        wrapper.generate_candidate(_candidate_ctx())
    assert wrapper.state is CircuitState.OPEN
    # Either method short-circuits.
    with pytest.raises(LLMCircuitOpenError):
        wrapper.generate_candidate(_candidate_ctx())


def test_generate_candidate_delegates_when_closed() -> None:
    """Sanity: the candidate method works through the wrapper when CLOSED."""
    inner = FakeLLMProvider()
    wrapper = CircuitBreakerLLMProvider(inner)
    candidate, _usage = wrapper.generate_candidate(_candidate_ctx())
    assert candidate.artifact_type == "prompt"
    assert len(inner.candidate_calls) == 1


# ---------------------------------------------------------------------------
# Construction validation + ``maybe_wrap``
# ---------------------------------------------------------------------------


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreakerLLMProvider(FakeLLMProvider(), failure_threshold=0)


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        CircuitBreakerLLMProvider(FakeLLMProvider(), window_seconds=0)


def test_invalid_open_duration_rejected() -> None:
    with pytest.raises(ValueError, match="open_duration_seconds"):
        CircuitBreakerLLMProvider(FakeLLMProvider(), open_duration_seconds=-1)


def test_maybe_wrap_disabled_returns_inner_unchanged() -> None:
    inner = FakeLLMProvider()
    assert (
        maybe_wrap(
            inner,
            enabled=False,
            failure_threshold=3,
            window_seconds=60.0,
            open_duration_seconds=30.0,
        )
        is inner
    )


def test_maybe_wrap_enabled_returns_wrapper() -> None:
    inner = FakeLLMProvider()
    wrapped = maybe_wrap(
        inner,
        enabled=True,
        failure_threshold=3,
        window_seconds=60.0,
        open_duration_seconds=30.0,
    )
    assert isinstance(wrapped, CircuitBreakerLLMProvider)
