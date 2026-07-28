"""Tests for the outbound webhook dispatcher.

Four layers of contract to pin:

1. The signing function emits the exact Stripe-style format receivers
   know how to verify (``t=<ts>,v1=<hex>``).
2. The dispatcher subscribes to the event bus and POSTs each event
   that matches the filter — and *only* those events.
3. A request actually carries the timestamp + signature headers and a
   recomputable HMAC.
4. The dispatcher degrades gracefully: no URLs → no-op; URLs but no
   secret → operator warning, no POSTs; receiver returns 500 → log it,
   keep going.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from caliber.events.bus import EventBus
from caliber.events.webhooks import (
    WebhookDispatcher,
    build_dispatcher,
    compute_signature,
)

# ---------------------------------------------------------------------------
# Signature format
# ---------------------------------------------------------------------------


def test_compute_signature_matches_stripe_style_hmac_sha256() -> None:
    """Receivers reproduce the calculation; this pins the exact recipe.

    Format: hex-encoded HMAC-SHA256 over ``f"{timestamp}.{payload}"``.
    """
    secret = b"shhh"
    timestamp = "1747365600"
    payload = '{"type":"approval.promoted"}'
    expected = hmac.new(secret, f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    assert compute_signature(secret, timestamp, payload) == expected


def test_compute_signature_is_deterministic_for_same_input() -> None:
    secret = b"k"
    sig_a = compute_signature(secret, "1", "x")
    sig_b = compute_signature(secret, "1", "x")
    assert sig_a == sig_b


def test_compute_signature_differs_when_payload_changes() -> None:
    """A receiver that recomputes against a tampered payload must fail
    the comparison — i.e. the signature MUST be payload-bound."""
    secret = b"k"
    sig_a = compute_signature(secret, "1", "x")
    sig_b = compute_signature(secret, "1", "x-tampered")
    assert sig_a != sig_b


def test_compute_signature_differs_when_timestamp_changes() -> None:
    """The timestamp is part of the signed string so replay attempts at
    a different time fail the comparison."""
    secret = b"k"
    sig_a = compute_signature(secret, "1", "x")
    sig_b = compute_signature(secret, "2", "x")
    assert sig_a != sig_b


# ---------------------------------------------------------------------------
# Dispatcher construction
# ---------------------------------------------------------------------------


def test_build_dispatcher_disabled_without_urls() -> None:
    bus = EventBus()
    dispatcher = build_dispatcher(
        bus=bus,
        urls_csv="",
        secret_env_var="UNUSED",
        event_filter_csv="*",
        environ={},
    )
    assert not dispatcher.is_enabled


def test_build_dispatcher_disabled_without_secret() -> None:
    bus = EventBus()
    dispatcher = build_dispatcher(
        bus=bus,
        urls_csv="https://hooks.example/inbound",
        secret_env_var="MISSING_SECRET",
        event_filter_csv="*",
        environ={},  # secret env var absent
    )
    assert not dispatcher.is_enabled


def test_build_dispatcher_enabled_when_both_present() -> None:
    bus = EventBus()
    dispatcher = build_dispatcher(
        bus=bus,
        urls_csv="https://hooks.example/inbound",
        secret_env_var="SECRET_VAR",
        event_filter_csv="approval.promoted",
        environ={"SECRET_VAR": "topsecret"},
    )
    assert dispatcher.is_enabled


def test_build_dispatcher_star_filter_subscribes_to_all() -> None:
    """``event_filter_csv="*"`` should pass every event through."""
    bus = EventBus()
    dispatcher = build_dispatcher(
        bus=bus,
        urls_csv="https://hooks.example/inbound",
        secret_env_var="SECRET_VAR",
        event_filter_csv="*",
        environ={"SECRET_VAR": "k"},
    )
    # Internal access — we want to make sure the filter resolved to
    # "all events", not just a literal set containing "*".
    assert dispatcher._event_filter is None


def test_dispatcher_skips_remote_nats_fanout_events() -> None:
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://hooks.example/inbound"],
        secret="topsecret",
    )
    assert dispatcher._should_dispatch({"type": "workflow.run.queued"}) is True
    assert (
        dispatcher._should_dispatch({"type": "workflow.run.queued", "_caliber_remote": True})
        is False
    )


# ---------------------------------------------------------------------------
# Dispatch behavior (with urllib mocked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal duck-type matching ``urllib.request.urlopen``'s context manager."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def captured_requests() -> Iterator[list[Any]]:
    """Intercept every ``urllib.request.urlopen`` call so tests can
    inspect the request object the dispatcher built."""
    captured: list[Any] = []

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured.append(req)
        return _FakeResponse(status=200)

    with patch("caliber.events.webhooks.urllib.request.urlopen", _fake_urlopen):
        yield captured


def _build_dispatcher_with(
    urls: list[str],
    secret: str = "topsecret",
    event_filter: frozenset[str] | None = None,
    max_attempts: int = 3,
) -> tuple[EventBus, WebhookDispatcher]:
    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=urls,
        secret=secret,
        event_filter=event_filter,
        max_attempts=max_attempts,
        # Retry timing is asserted separately; keep these tests instant.
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    return bus, dispatcher


async def _await_subscription(bus: EventBus, timeout: float = 1.0) -> None:
    """Spin until the dispatcher task has actually registered against the bus.

    ``EventBus.subscribe`` registers the queue inside the generator
    body, which runs on the first ``__anext__``. ``dispatcher.start()``
    only schedules the task; the registration happens on the first
    event-loop tick the task is allowed to run. Publishing before that
    point drops the event silently.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while bus.subscriber_count() == 0:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("dispatcher never subscribed to the bus")
        await asyncio.sleep(0.01)


async def _await_captures(captured: list[Any], expected: int, timeout: float = 1.0) -> None:
    """Wait until ``captured`` has at least ``expected`` entries.

    Used after a publish so the test asserts on the dispatch *result*
    rather than racing the event loop with a hard-coded sleep.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while len(captured) < expected:
        if asyncio.get_running_loop().time() > deadline:
            return  # let the assertion fail with a useful len
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatch_signs_and_posts_to_every_url(
    captured_requests: list[Any],
) -> None:
    bus, dispatcher = _build_dispatcher_with(
        urls=["https://a.example/hook", "https://b.example/hook"],
        secret="topsecret",
    )
    await dispatcher.start()
    try:
        await _await_subscription(bus)
        bus.publish({"type": "approval.promoted", "approval_id": "AP-1"})
        await _await_captures(captured_requests, expected=2)
    finally:
        await dispatcher.stop()

    assert len(captured_requests) == 2
    a_req, b_req = captured_requests
    assert a_req.full_url == "https://a.example/hook"
    assert b_req.full_url == "https://b.example/hook"

    # Headers — case-insensitive lookup since urllib normalizes.
    headers = {k.lower(): v for k, v in a_req.headers.items()}
    assert headers["content-type"] == "application/json"
    assert "x-caliber-timestamp" in headers
    assert headers["x-caliber-signature"].startswith("t=")
    assert ",v1=" in headers["x-caliber-signature"]

    # Body: parse it back, confirm the event payload arrived intact.
    payload = json.loads(a_req.data.decode("utf-8"))
    assert payload == {"type": "approval.promoted", "approval_id": "AP-1"}


@pytest.mark.asyncio
async def test_dispatch_signature_recomputes_against_secret(
    captured_requests: list[Any],
) -> None:
    """End-to-end signing test: the receiver-side verification recipe
    must succeed on the wire bytes the dispatcher actually sent."""
    secret = "shhh-it-is-secret"
    bus, dispatcher = _build_dispatcher_with(
        urls=["https://hooks.example/inbound"],
        secret=secret,
    )
    await dispatcher.start()
    try:
        await _await_subscription(bus)
        bus.publish({"type": "approval.promoted"})
        await _await_captures(captured_requests, expected=1)
    finally:
        await dispatcher.stop()

    assert len(captured_requests) == 1
    req = captured_requests[0]
    headers = {k.lower(): v for k, v in req.headers.items()}
    timestamp = headers["x-caliber-timestamp"]
    signature_value = headers["x-caliber-signature"]
    # Stripe-style: t=<ts>,v1=<hex>
    parts = dict(p.split("=", 1) for p in signature_value.split(","))
    assert parts["t"] == timestamp

    # Receiver-side recomputation against the *wire bytes* (not a
    # re-serialized dict — that would re-test our serializer).
    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.{req.data.decode('utf-8')}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(parts["v1"], expected)


@pytest.mark.asyncio
async def test_dispatch_respects_event_filter(
    captured_requests: list[Any],
) -> None:
    bus, dispatcher = _build_dispatcher_with(
        urls=["https://hooks.example/inbound"],
        event_filter=frozenset({"approval.promoted"}),
    )
    await dispatcher.start()
    try:
        await _await_subscription(bus)
        bus.publish({"type": "approval.promoted"})
        bus.publish({"type": "verification.verified"})  # filtered out
        bus.publish({"type": "approval.promoted"})
        await _await_captures(captured_requests, expected=2)
    finally:
        await dispatcher.stop()

    assert len(captured_requests) == 2  # not 3


@pytest.mark.asyncio
async def test_dispatch_swallows_per_url_failure() -> None:
    """One bad receiver shouldn't taint the others.

    ``max_attempts=1`` isolates the fan-out property from the retry policy: with
    retries on, the broken URL would legitimately appear more than once.
    """
    bus, dispatcher = _build_dispatcher_with(
        urls=["https://broken.example/hook", "https://ok.example/hook"],
        max_attempts=1,
    )

    captured: list[Any] = []

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured.append(req)
        if req.full_url.startswith("https://broken"):
            raise ConnectionError("simulated failure")
        return _FakeResponse(status=200)

    with patch("caliber.events.webhooks.urllib.request.urlopen", _fake_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "approval.promoted"})
            await _await_captures(captured, expected=2)
        finally:
            await dispatcher.stop()

    # Both URLs are attempted; only one succeeds at the receiver layer.
    assert len(captured) == 2
    # The broken one is dead-lettered rather than silently lost.
    letters = dispatcher.dead_letters()
    assert letters["count"] == 1
    assert letters["entries"][0]["url"] == "https://broken.example/hook"
    assert letters["delivered"] == 1


@pytest.mark.asyncio
async def test_dispatch_retries_a_5xx_then_dead_letters(
    captured_requests: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 500 is transient: retry to the attempt limit, then dead-letter.

    Previously a 500 was logged once and the event was dropped silently, so an
    operator could not distinguish "nothing happened" from "we failed to tell
    you". The dead-letter record is what makes the loss observable.
    """

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured_requests.append(req)
        return _FakeResponse(status=500)

    bus, dispatcher = _build_dispatcher_with(
        urls=["https://broken.example/hook"],
        max_attempts=3,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _fake_urlopen):
        await dispatcher.start()
        try:
            with caplog.at_level("WARNING", logger="caliber.events.webhooks"):
                await _await_subscription(bus)
                bus.publish({"type": "approval.promoted"})
                await _await_captures(captured_requests, expected=3)
        finally:
            await dispatcher.stop()

    assert len(captured_requests) == 3  # attempted, not dropped after one try
    letters = dispatcher.dead_letters()
    assert letters["count"] == 1
    assert letters["entries"][0]["event_type"] == "approval.promoted"
    assert "HTTP 500" in letters["entries"][0]["reason"]
    assert letters["entries"][0]["attempts"] == 3
    assert letters["delivered"] == 0
    assert letters["retried"] == 2
    assert any("dead-lettering" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_dispatch_does_not_retry_a_4xx(
    captured_requests: list[Any],
) -> None:
    """A rejected payload is permanent: replaying it cannot change the verdict, so
    retrying only wastes attempts and delays the dead-letter."""

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured_requests.append(req)
        return _FakeResponse(status=422)

    bus, dispatcher = _build_dispatcher_with(
        urls=["https://picky.example/hook"],
        max_attempts=5,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _fake_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "approval.promoted"})
            await _await_captures(captured_requests, expected=1)
        finally:
            await dispatcher.stop()

    assert len(captured_requests) == 1
    letters = dispatcher.dead_letters()
    assert letters["count"] == 1
    assert "HTTP 422" in letters["entries"][0]["reason"]
    assert letters["retried"] == 0


@pytest.mark.asyncio
async def test_a_transient_failure_that_recovers_is_delivered_not_dead_lettered(
    captured_requests: list[Any],
) -> None:
    """The point of the retry: a receiver restart must not lose the event."""
    attempts: list[int] = []

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured_requests.append(req)
        attempts.append(1)
        return _FakeResponse(status=200 if len(attempts) > 1 else 503)

    bus, dispatcher = _build_dispatcher_with(
        urls=["https://flaky.example/hook"],
        max_attempts=3,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _fake_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "approval.promoted"})
            await _await_captures(captured_requests, expected=2)
        finally:
            await dispatcher.stop()

    letters = dispatcher.dead_letters()
    assert letters["count"] == 0
    assert letters["delivered"] == 1
    assert letters["retried"] == 1


def test_retry_backoff_is_exponential() -> None:
    """Asserted directly so the delivery tests can run without real waiting."""
    slept: list[float] = []
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://x.example/hook"],
        secret="s",
        max_attempts=4,
        backoff_seconds=0.5,
        sleep=slept.append,
    )
    with patch(
        "caliber.events.webhooks.urllib.request.urlopen",
        side_effect=OSError("connection refused"),
    ):
        dispatcher._post_to_all({"type": "approval.promoted"})
    assert slept == [0.5, 1.0, 2.0]  # no sleep after the final attempt
    assert dispatcher.dead_letters()["count"] == 1


def test_the_dead_letter_ring_is_bounded_and_reports_what_it_dropped() -> None:
    """An unbounded ring would turn a long outage into a memory leak; a silently
    truncated one would misreport the failure history as complete."""
    from caliber.events.webhooks import _DEAD_LETTER_CAPACITY

    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://x.example/hook"],
        secret="s",
        max_attempts=1,
        backoff_seconds=0.0,
        sleep=lambda _s: None,
    )
    overflow = 5
    with patch(
        "caliber.events.webhooks.urllib.request.urlopen",
        side_effect=OSError("connection refused"),
    ):
        for index in range(_DEAD_LETTER_CAPACITY + overflow):
            dispatcher._post_to_all({"type": f"event.{index}"})

    letters = dispatcher.dead_letters()
    assert letters["count"] == _DEAD_LETTER_CAPACITY
    assert letters["dropped"] == overflow
    assert letters["capacity"] == _DEAD_LETTER_CAPACITY


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_noop_when_no_urls() -> None:
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=[],
        secret="any",
    )
    await dispatcher.start()
    # No background task → stop is also a no-op.
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_start_is_noop_when_no_secret() -> None:
    """Operators get a warning at startup; the task itself doesn't run."""
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://hook.example"],
        secret="",
    )
    await dispatcher.start()
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_double_start_raises() -> None:
    bus = EventBus()
    dispatcher = WebhookDispatcher(bus=bus, urls=["https://hook.example"], secret="k")
    captured = MagicMock()
    with patch("caliber.events.webhooks.urllib.request.urlopen", captured):
        await dispatcher.start()
        try:
            with pytest.raises(RuntimeError, match=r"already running"):
                await dispatcher.start()
        finally:
            await dispatcher.stop()
