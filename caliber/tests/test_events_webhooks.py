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
import threading
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
    pending_capacity: int = 4096,
) -> tuple[EventBus, WebhookDispatcher]:
    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=urls,
        secret=secret,
        event_filter=event_filter,
        max_attempts=max_attempts,
        pending_capacity=pending_capacity,
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


async def _await_captures(captured: list[Any], expected: int, timeout: float = 5.0) -> None:
    """Wait until ``captured`` has at least ``expected`` entries.

    Used after a publish so the test asserts on the dispatch *result*
    rather than racing the event loop with a hard-coded sleep.

    The deadline is generous on purpose. Retry backoff is already stubbed to zero in
    ``_build_dispatcher_with``, so what this actually waits on is the dispatcher task being
    *scheduled* — and under the full suite with ``-n auto`` every core is saturated, so a
    multi-attempt test could exceed a 1s budget and fail as if delivery had broken. It was
    observed exactly once in a 5634-test run and never in isolation, which is the signature
    of a scheduling budget rather than a defect. Raising the ceiling costs nothing when
    things pass, because the loop returns as soon as the condition holds.
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


# ---------------------------------------------------------------------------
# L6 — reception is decoupled from delivery, and the record is durable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_receiver_does_not_make_the_bus_drop_later_events() -> None:
    """The L6 regression, stated precisely.

    The dispatcher used to run its retry loop *inside* the bus subscriber, and the bus
    gives each subscriber a bounded queue that drops on overflow. A receiver slow
    enough to hold the subscriber therefore caused later events to be discarded by the
    bus before any delivery or dead-letter logic saw them.

    Here the receiver blocks until released, and more events are published than the
    bus's per-subscriber queue can hold. Every one must still reach delivery, because
    the subscriber's only job is now a ``put_nowait``.
    """
    from caliber.events.bus import _SUBSCRIBER_QUEUE_MAX

    total = _SUBSCRIBER_QUEUE_MAX + 40
    release = threading.Event()
    seen: list[str] = []
    seen_lock = threading.Lock()

    def _blocking_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        # The first delivery blocks, modelling a hung receiver, while the publisher
        # keeps producing. Later deliveries pass straight through.
        payload = json.loads(req.data.decode("utf-8"))
        with seen_lock:
            first = not seen
            seen.append(payload["id"])
        if first:
            release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus, dispatcher = _build_dispatcher_with(urls=["https://slow.example/hook"])
    with patch("caliber.events.webhooks.urllib.request.urlopen", _blocking_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            for index in range(total):
                bus.publish({"type": "run.completed", "id": str(index)})
                # Yield so the subscriber drains into the dispatcher's own queue,
                # which is the behaviour under test — the bus queue must not fill.
                await asyncio.sleep(0)
            release.set()
            deadline = asyncio.get_running_loop().time() + 10.0
            while len(seen) < total and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            release.set()
            await dispatcher.stop()

    assert len(seen) == total, f"delivered {len(seen)} of {total}; events were dropped by the bus"
    assert sorted(seen, key=int) == [str(index) for index in range(total)]


@pytest.mark.asyncio
async def test_overflowing_the_pending_queue_dead_letters_instead_of_dropping() -> None:
    """A bound is necessary — an unbounded queue turns event loss into memory
    exhaustion — but exhausting it must be *recorded*. Shedding load silently is the
    failure; shedding it visibly is a capacity decision an operator can act on."""
    release = threading.Event()

    def _blocking_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://slow.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        pending_capacity=2,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _blocking_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            for index in range(12):
                bus.publish({"type": "run.completed", "id": str(index)})
                await asyncio.sleep(0)
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                dispatcher.dead_letters()["overflowed"] == 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
        finally:
            release.set()
            await dispatcher.stop()

    report = dispatcher.dead_letters()
    assert report["overflowed"] > 0, "load shedding must be recorded, not silent"
    overflow_entries = [e for e in report["entries"] if e["kind"] == "overflow"]
    assert overflow_entries
    assert "pending delivery queue full" in overflow_entries[0]["reason"]
    # And the two loss modes stay distinguishable: they have different causes.
    assert overflow_entries[0]["attempts"] == 0


@pytest.mark.asyncio
async def test_a_dead_letter_is_persisted_when_a_session_factory_is_bound(
    session_factory,
) -> None:
    """Durability is the other half of L6: the in-memory ring lost the record of an
    undelivered event exactly when an operator restarted to fix the receiver."""
    from caliber.db.models import CaliberWebhookDeadLetter

    def _always_500(req: Any, timeout: float = 0) -> _FakeResponse:
        del req, timeout
        return _FakeResponse(status=500)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://broken.example/hook"],
        secret="topsecret",
        max_attempts=2,
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _always_500):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "R-1"})
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                dispatcher.dead_letters()["count"] == 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()

    assert dispatcher.dead_letters()["durable"] is True
    with session_factory() as session:
        rows = session.query(CaliberWebhookDeadLetter).all()
    assert len(rows) == 1
    assert rows[0].url == "https://broken.example/hook"
    assert rows[0].event_type == "run.completed"
    assert rows[0].kind == "exhausted"
    assert rows[0].status == "open"
    # The whole event is retained so a failure can be replayed by hand rather than
    # only acknowledged.
    assert rows[0].event == {"type": "run.completed", "id": "R-1"}


@pytest.mark.asyncio
async def test_a_failing_persist_does_not_break_delivery(session_factory) -> None:
    """The dispatcher has already failed to deliver an event; raising while recording
    that would lose both the event and the record of it."""

    def _always_500(req: Any, timeout: float = 0) -> _FakeResponse:
        del req, timeout
        return _FakeResponse(status=500)

    def _broken_factory():
        raise RuntimeError("database is gone")

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://broken.example/hook"],
        secret="topsecret",
        max_attempts=1,
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=_broken_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _always_500):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "R-1"})
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                dispatcher.dead_letters()["count"] == 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()

    # The in-memory record still holds it, so the failure is observable even with the
    # durable store unavailable.
    assert dispatcher.dead_letters()["count"] == 1


def test_the_report_exposes_pending_depth_and_durability() -> None:
    """``/system/queue`` renders this, and an operator needs to distinguish "queued
    behind a slow receiver" from "already given up on"."""
    _bus, dispatcher = _build_dispatcher_with(urls=["https://a.example/hook"])
    report = dispatcher.dead_letters()
    assert report["pending"] == 0
    assert report["pending_capacity"] > 0
    assert report["overflowed"] == 0
    assert report["durable"] is False  # no session factory in this construction


@pytest.mark.asyncio
async def test_stopping_dead_letters_events_that_were_never_sent(session_factory) -> None:
    """N5: a graceful stop must not silently discard accepted events.

    ``stop()`` cancelled the delivery task and dropped whatever was queued, while its
    docstring claimed the overflow/exhaustion paths covered it. They do not — those
    cover events that overflowed the queue or exhausted their retries, not ones merely
    waiting. So a routine restart lost accepted events with no record anywhere.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    release = threading.Event()

    def _blocking_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://slow.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _blocking_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            # One event occupies the (blocked) delivery task; the rest sit in the queue.
            for index in range(4):
                bus.publish({"type": "run.completed", "id": str(index)})
                await asyncio.sleep(0)
        finally:
            release.set()
            await dispatcher.stop()

    with session_factory() as session:
        rows = session.query(CaliberWebhookDeadLetter).all()
    shutdown_rows = [r for r in rows if r.kind == "shutdown"]
    assert shutdown_rows, "queued-but-unsent events must be recorded, not discarded"
    # The full event is retained, so an operator can replay it by hand.
    assert shutdown_rows[0].event["type"] == "run.completed"
    assert shutdown_rows[0].attempts == 0
    # Two shutdown reasons are possible and both are correct: the event was still
    # queued, or it was in flight when the stop arrived. The in-flight one is recorded
    # first because it is the one closest to being lost.
    assert any(
        phrase in row.reason
        for row in shutdown_rows
        for phrase in ("stopped before this event was delivered", "in flight")
    )


@pytest.mark.asyncio
async def test_a_clean_stop_with_nothing_queued_records_nothing(session_factory) -> None:
    """The drain must stay narrow: a quiet shutdown is not a delivery failure, and
    inventing dead letters for it would make the queue useless as a work list."""
    from caliber.db.models import CaliberWebhookDeadLetter

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://a.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
    )
    await dispatcher.start()
    await _await_subscription(bus)
    await dispatcher.stop()

    with session_factory() as session:
        assert session.query(CaliberWebhookDeadLetter).count() == 0


@pytest.mark.asyncio
async def test_stopping_records_the_event_that_was_in_flight(session_factory) -> None:
    """The in-flight half of N5, which the first fix missed.

    ``_deliver_forever`` calls ``get()`` — removing the event from the queue — and then
    awaits the POST. Between those two points the event exists only in a local
    variable, so a drain that walked the queue alone missed exactly the event most
    likely to be lost: the one being delivered when the stop arrived. An independent
    probe reproduced it as ``pending_before_stop=0`` with zero dead-letter rows.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    entered = threading.Event()
    release = threading.Event()

    def _blocking_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        entered.set()
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://slow.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _blocking_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "in-flight"})
            deadline = asyncio.get_running_loop().time() + 5.0
            while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert entered.is_set(), "sender never started; the test would prove nothing"
            assert dispatcher.dead_letters()["pending"] == 0
        finally:
            # stop() *while the sender is still blocked* — that is the whole scenario.
            # Releasing first would let delivery finish and prove nothing.
            await dispatcher.stop()
            release.set()

    with session_factory() as session:
        rows = session.query(CaliberWebhookDeadLetter).all()
    in_flight = [r for r in rows if r.event and r.event.get("id") == "in-flight"]
    assert in_flight, "an event in flight at shutdown must still be recorded"
    assert "in flight" in in_flight[0].reason
    assert in_flight[0].kind == "shutdown"


@pytest.mark.asyncio
async def test_shutdown_records_every_target_still_owed_an_outcome(session_factory) -> None:
    """Per-target tracking, which an independent probe showed the first fix lacked.

    One event fans out to every configured URL and each target has its own outcome, but
    the marker was event-level. So the *first* target to be dead-lettered discharged
    every other target's obligation: with target one failing permanently and target two
    still blocked, `stop()` logged a clean stop and wrote no row for target two.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    release = threading.Event()

    def _urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        if "doomed" in req.full_url:
            # A 4xx status rather than a hand-built HTTPError: it travels the real
            # permanent-failure path in ``_post``, and constructing HTTPError with a
            # None file object trips a ResourceWarning that pytest promotes to an error.
            return _FakeResponse(status=400)
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://doomed.example/hook", "https://blocked.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "fan-out"})
            await asyncio.sleep(0.3)
        finally:
            await dispatcher.stop()
            release.set()

    with session_factory() as session:
        rows = session.query(CaliberWebhookDeadLetter).all()
    by_url = {row.url: row.kind for row in rows}
    assert "https://doomed.example/hook" in by_url, "the permanently failing target"
    assert by_url["https://blocked.example/hook"] == "shutdown", (
        "a target still blocked at shutdown must get its own row, not be discharged "
        "by a sibling target's dead letter"
    )


@pytest.mark.asyncio
async def test_shutdown_does_not_dead_letter_an_already_delivered_target(
    session_factory,
) -> None:
    """The converse, and the other half of the same defect.

    Draining the event against *every* configured URL would invent a shutdown row for a
    target that had already succeeded — reporting a delivered event as lost, which sends
    an operator chasing a replay that must not happen.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    release = threading.Event()

    def _urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        if "fast" not in req.full_url:
            release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://fast.example/hook", "https://slow.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks.urllib.request.urlopen", _urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "partial"})
            await asyncio.sleep(0.3)
        finally:
            await dispatcher.stop()
            release.set()

    with session_factory() as session:
        urls = {row.url for row in session.query(CaliberWebhookDeadLetter).all()}
    assert "https://fast.example/hook" not in urls, "a delivered target must not be dead-lettered"
    assert "https://slow.example/hook" in urls
