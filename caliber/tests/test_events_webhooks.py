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
from datetime import datetime, timedelta, timezone
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
    """Minimal duck-type matching the opener's response context manager."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def captured_requests() -> Iterator[list[Any]]:
    """Intercept every delivery on the dispatcher's opener so tests can
    inspect the request object the dispatcher built."""
    captured: list[Any] = []

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured.append(req)
        return _FakeResponse(status=200)

    with patch("caliber.events.webhooks._OPENER.open", _fake_urlopen):
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


async def _await_condition(predicate: Any, timeout: float = 30.0) -> None:
    """Poll ``predicate`` until it is true or the deadline passes.

    The deadline is deliberately large, and the reason is worth recording because two
    smaller values were tried first and both flaked.

    Retry backoff is already stubbed to zero in ``_build_dispatcher_with``, so this is not
    waiting on backoff. Delivery runs via ``asyncio.to_thread``, i.e. on the event loop's
    **default ThreadPoolExecutor** — and under the full suite other async tests sharing the
    same xdist worker can occupy those threads, so the delivery thread queues. Pure CPU
    saturation does *not* reproduce it (checked with 12 spinners; the test still passed in
    0.08s), which is what points at executor contention rather than raw load.

    A large ceiling is free when the condition holds, because this returns as soon as it
    does. It is only paid on a genuine regression, where waiting 30s before failing is a
    fine trade for not failing spuriously.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return  # let the caller's assertion report the actual state
        await asyncio.sleep(0.01)


async def _await_captures(captured: list[Any], expected: int, timeout: float = 30.0) -> None:
    """Wait until ``captured`` has at least ``expected`` entries.

    Used after a publish so the test asserts on the dispatch *result*
    rather than racing the event loop with a hard-coded sleep.
    See :func:`_await_condition` for why the deadline is what it is.
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

    with patch("caliber.events.webhooks._OPENER.open", _fake_urlopen):
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
    with patch("caliber.events.webhooks._OPENER.open", _fake_urlopen):
        await dispatcher.start()
        try:
            with caplog.at_level("WARNING", logger="caliber.events.webhooks"):
                await _await_subscription(bus)
                bus.publish({"type": "approval.promoted"})
                # Wait for the *terminal* state, not the attempt count. Polling for
                # "3 captures" cannot tell "the third attempt has not happened yet" from
                # "delivery stalled", so it can time out mid-retry and report a count that
                # was always going to arrive. The dead letter is written only after the
                # attempts are exhausted, so it is the condition this test actually means.
                await _await_condition(lambda: dispatcher.dead_letters()["count"] >= 1)
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
    with patch("caliber.events.webhooks._OPENER.open", _fake_urlopen):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "approval.promoted"})
            # Wait for the *settled* outcome, not the captured request. The POST is
            # recorded before the delivery thread writes its dead letter, so stopping
            # at the capture let ``stop()``'s drain win the atomic claim and record
            # ``shutdown`` where this test asserts ``exhausted``. Waiting on the kind
            # under test removes the race rather than widening a timeout.
            await _await_condition(
                lambda: any(e["kind"] == "exhausted" for e in dispatcher.dead_letters()["entries"])
            )
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
    with patch("caliber.events.webhooks._OPENER.open", _fake_urlopen):
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
        "caliber.events.webhooks._OPENER.open",
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
        "caliber.events.webhooks._OPENER.open",
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
    with patch("caliber.events.webhooks._OPENER.open", captured):
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
    with patch("caliber.events.webhooks._OPENER.open", _blocking_urlopen):
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
    with patch("caliber.events.webhooks._OPENER.open", _blocking_urlopen):
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
    with patch("caliber.events.webhooks._OPENER.open", _always_500):
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
async def test_a_failing_accept_persist_refuses_delivery(session_factory) -> None:
    """Sending without a durable obligation can turn a later crash into silent loss."""

    requests: list[Any] = []

    def _capture(req: Any, timeout: float = 0) -> _FakeResponse:
        del timeout
        requests.append(req)
        return _FakeResponse(status=200)

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
    with patch("caliber.events.webhooks._OPENER.open", _capture):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "R-1"})
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                dispatcher.dead_letters()["accept_rejected"] == 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()

    report = dispatcher.dead_letters()
    assert requests == [], "an event with no durable accept record must not be sent"
    assert report["accept_rejected"] == 1
    assert report["count"] == 1
    assert report["entries"][0]["kind"] == "accept_failed"


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
    with patch("caliber.events.webhooks._OPENER.open", _blocking_urlopen):
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
    with patch("caliber.events.webhooks._OPENER.open", _blocking_urlopen):
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
async def test_stop_prevents_detached_sender_from_posting_later_targets_after_restart() -> None:
    """Cancelling ``to_thread`` must not let the old generation keep sending.

    The first target is held inside ``urlopen`` while ``stop()`` records both targets as
    abandoned. The same dispatcher is then restarted and delivers a new event before the
    old POST returns. Releasing the old worker must neither start its second target nor
    settle the new generation's in-flight map.
    """
    old_entered = threading.Event()
    old_release = threading.Event()
    old_worker_done = threading.Event()
    calls: list[tuple[str, str]] = []
    calls_lock = threading.Lock()

    def _generation_aware_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        del timeout
        payload = json.loads(req.data.decode("utf-8"))
        call = (str(payload["id"]), req.full_url)
        with calls_lock:
            calls.append(call)
        if call == ("old", "https://a.example/hook"):
            old_entered.set()
            old_release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://a.example/hook", "https://b.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    real_post_to_all = dispatcher._post_to_all

    def _observe_worker_completion(event: dict[str, object], *args: Any) -> None:
        try:
            real_post_to_all(event, *args)
        finally:
            if event.get("id") == "old":
                old_worker_done.set()

    dispatcher._post_to_all = _observe_worker_completion  # type: ignore[method-assign]
    with patch("caliber.events.webhooks._OPENER.open", _generation_aware_urlopen):
        await dispatcher.start()
        await _await_subscription(bus)
        bus.publish({"type": "run.completed", "id": "old"})
        await _await_condition(old_entered.is_set, timeout=5.0)
        assert old_entered.is_set(), "the old generation never entered its first POST"

        await dispatcher.stop()
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "new"})
            await _await_condition(
                lambda: (
                    {url for event_id, url in calls if event_id == "new"}
                    == {"https://a.example/hook", "https://b.example/hook"}
                ),
                timeout=5.0,
            )
            old_release.set()
            await _await_condition(old_worker_done.is_set, timeout=5.0)
            assert old_worker_done.is_set(), "detached old sender did not finish"
        finally:
            old_release.set()
            await dispatcher.stop()

    old_calls = [url for event_id, url in calls if event_id == "old"]
    new_calls = [url for event_id, url in calls if event_id == "new"]
    assert old_calls == ["https://a.example/hook"]
    assert new_calls == ["https://a.example/hook", "https://b.example/hook"]


@pytest.mark.asyncio
async def test_a_target_is_recorded_once_when_delivery_and_shutdown_race(
    session_factory,
) -> None:
    """One event, one target, one row — even when both writers reach it.

    Cancelling ``asyncio.to_thread`` does **not** stop the worker thread: the ``await``
    raises immediately while the POST keeps running to completion. So at shutdown two
    writers can settle the same target — the delivery thread finishing
    ``_deliver_with_retry``, and ``stop()``'s drain walking ``_in_flight`` — and the event
    was dead-lettered twice, once ``exhausted`` and once ``shutdown``. An operator saw two
    rows for one delivery and would double-count it on replay.

    This surfaced as an intermittent failure in an unrelated assertion
    (``letters["count"] == 1`` seeing ``2``) and moved between tests run to run, which is
    what a race looks like from the outside. Reproduced deterministically here by holding
    the sender inside the POST, stopping while it is held, and only then releasing it — so
    the drain runs first and the thread finishes second, which is the losing order.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    entered = threading.Event()
    release = threading.Event()

    def _blocking_then_failing(req: Any, timeout: float = 0) -> _FakeResponse:
        entered.set()
        release.wait(timeout=5.0)
        # 422 is permanent, so the thread goes straight to its own dead-letter record.
        return _FakeResponse(status=422)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://picky.example/hook"],
        secret="topsecret",
        max_attempts=1,
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks._OPENER.open", _blocking_then_failing):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "raced"})
            await _await_condition(entered.is_set, timeout=5.0)
            assert entered.is_set(), "sender never started; the test would prove nothing"
        finally:
            await dispatcher.stop()
            # Released only now, so the thread records *after* the drain already has.
            release.set()
            # Give the detached thread time to finish and attempt its own record.
            await _await_condition(lambda: False, timeout=0.5)

    with session_factory() as session:
        rows = [
            r
            for r in session.query(CaliberWebhookDeadLetter).all()
            if r.event and r.event.get("id") == "raced"
        ]
    assert len(rows) == 1, f"one target must yield one row, got {[r.kind for r in rows]}"
    # Either writer winning is acceptable; recording twice is not.
    assert rows[0].kind in {"shutdown", "exhausted"}


@pytest.mark.asyncio
async def test_an_abruptly_killed_process_leaves_a_recoverable_record(
    session_factory,
) -> None:
    """The half of durability the graceful drain cannot cover.

    ``stop()`` drains queued and in-flight events, but ``SIGKILL``, an OOM kill, and a
    container eviction never run it — and both the queue and the in-flight map are memory
    only. An operator could still not tell "nothing happened" from "we failed to tell you"
    for precisely the failure where it matters most.

    A killed process cannot be simulated by calling ``stop()``, because that is the path
    under test being absent. So the crash is modelled the only faithful way: hold the
    sender inside the POST so the event is genuinely accepted-but-unsettled, then abandon
    the dispatcher without stopping it — leaving exactly the durable state a killed process
    would leave — and start a fresh dispatcher against the same database.
    """
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    entered = threading.Event()
    release = threading.Event()

    def _blocking(req: Any, timeout: float = 0) -> _FakeResponse:
        entered.set()
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    doomed = WebhookDispatcher(
        bus=bus,
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
    )
    with patch("caliber.events.webhooks._OPENER.open", _blocking):
        await doomed.start()
        await _await_subscription(bus)
        bus.publish({"type": "run.completed", "id": "killed-mid-flight"})
        await _await_condition(entered.is_set, timeout=5.0)
        assert entered.is_set(), "the event never reached the sender"

        # The accept must already be durable at this point — that is what makes the crash
        # recoverable rather than silent.
        with session_factory() as session:
            assert session.query(CaliberWebhookAcceptedEvent).count() == 1

        # Abandon without stop(): cancel the process-owned tasks but keep the sender thread
        # blocked. A real crashed process cannot renew its lease, so expire it explicitly.
        tasks = [task for task in (doomed._task, doomed._delivery_task, doomed._lease_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with session_factory() as session:
            row = session.query(CaliberWebhookAcceptedEvent).one()
            row.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=1
            )
            session.commit()

        # A fresh process starts against the same database and claims only the expired row.
        survivor = WebhookDispatcher(
            bus=EventBus(),
            urls=["https://receiver.example/hook"],
            secret="topsecret",
            session_factory=session_factory,
        )
        recovered = survivor.recover_accepted_events()
        release.set()

    assert recovered == 1, "the accepted event was not recovered after an abrupt loss"
    with session_factory() as session:
        rows = session.query(CaliberWebhookDeadLetter).all()
        # Recorded as replayable, and the accept row is consumed so a second boot does not
        # report the same loss again.
        assert [r.kind for r in rows] == ["shutdown"]
        assert rows[0].event and rows[0].event.get("id") == "killed-mid-flight"
        assert session.query(CaliberWebhookAcceptedEvent).count() == 0


@pytest.mark.asyncio
async def test_start_recovers_expired_accepts_after_all_urls_are_removed(
    session_factory,
) -> None:
    """Current dispatch configuration cannot erase obligations accepted previously."""
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberWebhookAcceptedEvent(
                accepted_id="accepted-before-url-removal",
                url="https://retired.example/hook",
                event_type="run.completed",
                event={"type": "run.completed", "id": "orphaned-config"},
                accepted_at=now - timedelta(minutes=1),
                owner_id="dead-replica",
                lease_expires_at=now - timedelta(seconds=1),
            )
        )
        session.commit()

    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=[],
        secret="",
        session_factory=session_factory,
        owner_id="replacement-replica",
    )
    await dispatcher.start()
    try:
        assert dispatcher._task is None, "no URLs should still leave live dispatch disabled"
        assert dispatcher._lease_task is not None, "durable recovery is configuration-independent"
        with session_factory() as session:
            assert session.query(CaliberWebhookAcceptedEvent).count() == 0
            recovered = session.query(CaliberWebhookDeadLetter).one()
            assert recovered.kind == "shutdown"
            assert recovered.url == "https://retired.example/hook"
            assert recovered.event and recovered.event.get("id") == "orphaned-config"
    finally:
        await dispatcher.stop()
    assert dispatcher._lease_task is None


@pytest.mark.asyncio
async def test_no_url_recovery_continues_until_a_foreign_live_lease_expires(
    session_factory,
) -> None:
    """Disabling all current targets must not require another restart for recovery."""
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberWebhookAcceptedEvent(
                accepted_id="live-when-zero-url-replica-started",
                url="https://retired.example/hook",
                event_type="run.completed",
                event={"type": "run.completed", "id": "expires-after-start"},
                accepted_at=now,
                owner_id="old-still-live-replica",
                lease_expires_at=now + timedelta(seconds=0.2),
            )
        )
        session.commit()

    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=[],
        secret="",
        session_factory=session_factory,
        owner_id="recovery-only-replica",
        accept_lease_seconds=0.12,
    )
    await dispatcher.start()
    try:
        assert dispatcher._task is None
        assert dispatcher._delivery_task is None
        assert dispatcher._lease_task is not None
        with session_factory() as session:
            assert session.query(CaliberWebhookAcceptedEvent).count() == 1
            assert session.query(CaliberWebhookDeadLetter).count() == 0
        await _await_condition(
            lambda: dispatcher.dead_letters()["count"] == 1,
            timeout=3.0,
        )
    finally:
        await dispatcher.stop()

    assert dispatcher._lease_task is None
    with session_factory() as session:
        assert session.query(CaliberWebhookAcceptedEvent).count() == 0
        recovered = session.query(CaliberWebhookDeadLetter).one()
        assert recovered.kind == "shutdown"
        assert recovered.event and recovered.event.get("id") == "expires-after-start"


def test_a_live_replicas_accept_row_is_not_swept(session_factory) -> None:
    """Starting replica B must not dead-letter work replica A is still delivering."""
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    owner = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
        owner_id="replica-a",
        accept_lease_seconds=60.0,
    )
    assert owner._persist_accepted({"type": "run.completed", "id": "live"}) is True

    contender = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
        owner_id="replica-b",
    )
    assert contender.recover_accepted_events() == 0

    with session_factory() as session:
        row = session.query(CaliberWebhookAcceptedEvent).one()
        assert row.owner_id == "replica-a"
        assert row.lease_expires_at > datetime.now(timezone.utc).replace(tzinfo=None)
        assert session.query(CaliberWebhookDeadLetter).count() == 0


@pytest.mark.asyncio
async def test_a_running_replica_recovers_a_lease_that_expires_after_start(
    session_factory,
) -> None:
    """Failover must not require a third replica restart after the owner crashes."""
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    owner = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
        owner_id="replica-a",
        accept_lease_seconds=0.3,
    )
    assert owner._persist_accepted({"type": "run.completed", "id": "later-expiry"})

    survivor = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
        owner_id="replica-b",
        accept_lease_seconds=0.15,
    )
    await survivor.start()
    try:
        with session_factory() as session:
            assert session.query(CaliberWebhookAcceptedEvent).count() == 1
            assert session.query(CaliberWebhookDeadLetter).count() == 0
        await _await_condition(
            lambda: survivor.dead_letters()["count"] == 1,
            timeout=3.0,
        )
    finally:
        await survivor.stop()

    with session_factory() as session:
        assert session.query(CaliberWebhookAcceptedEvent).count() == 0
        assert session.query(CaliberWebhookDeadLetter).count() == 1


def test_failed_recovery_persistence_keeps_the_accept_row(session_factory) -> None:
    """The dead letter must commit before recovery consumes its only durable evidence."""
    from caliber.db.models import CaliberWebhookAcceptedEvent, CaliberWebhookDeadLetter

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberWebhookAcceptedEvent(
                accepted_id="expired-accept",
                url="https://receiver.example/hook",
                event_type="run.completed",
                event={"type": "run.completed", "id": "expired"},
                accepted_at=now - timedelta(minutes=1),
                owner_id="dead-replica",
                lease_expires_at=now - timedelta(seconds=1),
            )
        )
        session.commit()

    class _FailingDeadLetterSession:
        def __init__(self) -> None:
            self._inner = session_factory()

        def __enter__(self) -> Any:
            self._inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._inner.__exit__(*args)

        def add(self, instance: Any) -> None:
            if isinstance(instance, CaliberWebhookDeadLetter):
                raise RuntimeError("dead-letter storage failed")
            self._inner.add(instance)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=_FailingDeadLetterSession,
        owner_id="recovery-replica",
        accept_lease_seconds=1.0,
    )
    assert dispatcher.recover_accepted_events() == 0

    with session_factory() as session:
        row = session.get(CaliberWebhookAcceptedEvent, "expired-accept")
        assert row is not None, "a failed dead-letter insert must not consume the accept row"
        assert row.owner_id == "recovery-replica"
        assert session.query(CaliberWebhookDeadLetter).count() == 0


@pytest.mark.asyncio
async def test_successful_delivery_prunes_accept_markers(session_factory) -> None:
    """Settled events cannot leave one marker and lease id per delivery forever."""
    from caliber.db.models import CaliberWebhookAcceptedEvent

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
    )
    with patch(
        "caliber.events.webhooks._OPENER.open",
        lambda req, timeout=0: _FakeResponse(status=200),
    ):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "settled"})
            await _await_condition(
                lambda: (
                    dispatcher.dead_letters()["delivered"] == 1 and not dispatcher._event_markers
                )
            )
        finally:
            await dispatcher.stop()

    assert dispatcher._event_markers == {}
    assert dispatcher._marker_targets == {}
    assert dispatcher._owned_accept_ids == set()
    with session_factory() as session:
        assert session.query(CaliberWebhookAcceptedEvent).count() == 0


@pytest.mark.asyncio
async def test_republishing_the_same_event_object_tracks_each_occurrence(session_factory) -> None:
    """Object identity reuse cannot overwrite one occurrence's durable marker."""
    from caliber.db.models import CaliberWebhookAcceptedEvent

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://receiver.example/hook"],
        secret="topsecret",
        session_factory=session_factory,
    )
    event = {"type": "run.completed", "id": "same-object"}
    with patch(
        "caliber.events.webhooks._OPENER.open",
        lambda req, timeout=0: _FakeResponse(status=200),
    ):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish(event)
            bus.publish(event)
            await _await_condition(lambda: dispatcher.dead_letters()["delivered"] == 2)
        finally:
            await dispatcher.stop()

    assert dispatcher._event_markers == {}
    assert dispatcher._marker_targets == {}
    assert dispatcher._owned_accept_ids == set()
    with session_factory() as session:
        assert session.query(CaliberWebhookAcceptedEvent).count() == 0


@pytest.mark.asyncio
async def test_overflow_does_not_erase_a_different_events_in_flight_marker(
    session_factory,
) -> None:
    """A dead letter for one event must not discharge another event's obligation.

    Found by an independent probe, and distinct from the same-event double-writer race in
    §0.19. `_in_flight` is keyed by URL, and the non-settling record path used to
    `pop(url)` — but overflow concerns an event that never left the queue, so the marker it
    removed belonged to whichever event was *actually* in flight at that URL. With one
    event blocked in delivery and another overflowing at the same URL, the blocked event
    lost its record entirely and shutdown persisted no row for it.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    entered = threading.Event()
    release = threading.Event()

    def _blocking(req: Any, timeout: float = 0) -> _FakeResponse:
        entered.set()
        release.wait(timeout=5.0)
        return _FakeResponse(status=200)

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        bus=bus,
        urls=["https://one.example/hook"],
        secret="topsecret",
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
        session_factory=session_factory,
        # Capacity 1 so the third event overflows while the first is still blocked.
        pending_capacity=1,
    )
    with patch("caliber.events.webhooks._OPENER.open", _blocking):
        await dispatcher.start()
        try:
            await _await_subscription(bus)
            bus.publish({"type": "run.completed", "id": "blocked"})
            await _await_condition(entered.is_set, timeout=5.0)
            assert entered.is_set(), "first event never reached the sender"
            # Queued, then overflowing — both at the same URL as the blocked delivery.
            bus.publish({"type": "run.completed", "id": "queued"})
            bus.publish({"type": "run.completed", "id": "overflowed"})
            await _await_condition(
                lambda: any(
                    e.get("kind") == "overflow" for e in dispatcher.dead_letters()["entries"]
                ),
                timeout=5.0,
            )
        finally:
            await dispatcher.stop()
            release.set()
            await _await_condition(lambda: False, timeout=0.5)

    with session_factory() as session:
        ids = {r.event.get("id") for r in session.query(CaliberWebhookDeadLetter).all() if r.event}
    # The blocked event is the one whose marker was being erased. It must still have a row.
    assert "blocked" in ids, f"the in-flight event lost its durable outcome; got {sorted(ids)}"


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
    with patch("caliber.events.webhooks._OPENER.open", _urlopen):
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
    with patch("caliber.events.webhooks._OPENER.open", _urlopen):
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


# ---------------------------------------------------------------------------
# Redirects away from the configured destination
# ---------------------------------------------------------------------------


def test_delivery_refuses_to_follow_a_redirect_to_another_host() -> None:
    """The receiver must not get to choose where CALIBER sends the signature.

    ``urlopen`` installs the default opener, which follows redirects, so an
    operator-configured URL became a URL the *receiver* selects. Measured
    against this exact pair of servers before the fix: the 302 was followed and
    the second host received ``X-Caliber-Signature`` and ``X-Caliber-Timestamp``
    intact. urllib rewrites POST to GET on a 302 so the body was dropped — but a
    valid HMAC for that payload had already reached a host the operator never
    configured, which is the disclosure that matters.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from caliber.events.webhooks import WebhookDeliveryError

    unauthorized: list[dict[str, str]] = []

    class Unauthorized(BaseHTTPRequestHandler):
        def _record(self, method: str) -> None:
            unauthorized.append(
                {"method": method, "signature": self.headers.get("X-Caliber-Signature", "")}
            )
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:
            self._record("GET")

        def do_POST(self) -> None:
            self._record("POST")

        def log_message(self, *args: object) -> None:
            return

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/internal")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    sink = HTTPServer(("127.0.0.1", 0), Unauthorized)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    hop = HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=hop.serve_forever, daemon=True).start()
    try:
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            urls=[f"http://127.0.0.1:{hop.server_port}/hook"],
            secret="topsecret",
        )
        with pytest.raises(WebhookDeliveryError):
            dispatcher._post(
                f"http://127.0.0.1:{hop.server_port}/hook",
                b'{"type":"approval.promoted"}',
                "1700000000",
                "deadbeef",
            )
    finally:
        # ``shutdown`` stops serve_forever; ``server_close`` releases the
        # listening socket. Without the second the suite's ResourceWarning
        # policy fails whichever test the GC happens to be running in.
        for server in (sink, hop):
            server.shutdown()
            server.server_close()

    assert unauthorized == [], f"the redirect was followed: {unauthorized}"


# ---------------------------------------------------------------------------
# Loss upstream of the dispatcher's own durability
# ---------------------------------------------------------------------------


def test_a_bus_queue_overflow_is_dead_lettered_not_just_logged() -> None:
    """The one loss this component exists to prevent had no record.

    The bus gives each subscriber a bounded queue and drops on overflow with a
    log line. That happens *upstream* of every mechanism here — before
    acceptance, before retry, before the dead-letter table — so a burst could
    lose a notification with nothing but a warning to show for it, which reads
    to an operator as "nothing happened".
    """
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://hooks.example/inbound"],
        secret="topsecret",
    )

    dispatcher._record_bus_overflow({"type": "approval.promoted", "id": "EV-1"})

    letters = dispatcher.dead_letters()
    entries = letters["entries"] if isinstance(letters, dict) else letters
    assert entries, "a bus-level drop must leave a durable record"
    assert any("bus subscriber queue overflowed" in str(entry) for entry in entries)


def test_a_filtered_out_event_dropped_by_the_bus_is_not_dead_lettered() -> None:
    """Recording drops this dispatcher would never have delivered buries the real ones."""
    dispatcher = WebhookDispatcher(
        bus=EventBus(),
        urls=["https://hooks.example/inbound"],
        secret="topsecret",
        event_filter="approval.promoted",
    )

    dispatcher._record_bus_overflow({"type": "something.unrelated", "id": "EV-2"})

    letters = dispatcher.dead_letters()
    entries = letters["entries"] if isinstance(letters, dict) else letters
    assert not entries
