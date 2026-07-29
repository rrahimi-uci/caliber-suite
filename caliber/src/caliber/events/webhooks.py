"""HMAC-signed outbound webhook dispatcher.

Subscribes to the in-process :class:`caliber.events.bus.EventBus` and
POSTs each event matching the configured filter to every registered
URL. Every request is signed with HMAC-SHA256 so receivers can verify
authenticity and reject replays older than the configured window.

Signature format follows the Stripe convention — the de-facto standard
for webhook signing and the format the OSS receiver libraries already
know how to verify::

    X-Caliber-Timestamp: 1747365600
    X-Caliber-Signature: t=1747365600,v1=<hex of HMAC-SHA256(secret, "{ts}.{payload}")>

Receivers should:

1. Verify the timestamp is within ``±5 minutes`` of their own clock.
2. Recompute ``HMAC-SHA256(secret, "{ts}.{payload}")`` and compare with
   ``hmac.compare_digest`` (constant-time) against the ``v1=`` value.
3. Reject anything that fails either check.

Delivery is **bounded retry with a dead-letter**, not fire-and-forget. A
receiver restart or a brief network blip used to drop the event silently, which
made the webhook stream unusable as an operational signal: an operator could not
tell "nothing happened" from "we failed to tell you". Each POST is retried
with exponential backoff up to a configured attempt limit; a 4xx is treated as
permanent (retrying a rejected payload cannot help) while a 5xx, a timeout, or a
connection error is retried.

## Reception is decoupled from delivery (closes L6)

The dispatcher used to retry **inside its bus subscriber**, and the bus gives each
subscriber a bounded 128-entry queue whose overflow behaviour is to drop. A slow or
unavailable receiver therefore occupied the subscriber long enough for later events to
be discarded by the bus before any delivery logic saw them — events lost *before* the
retry/dead-letter machinery could record them, which is the one failure the machinery
existed to prevent.

Now the subscriber does nothing but move events onto the dispatcher's **own** bounded
queue, which it drains in microseconds, so the bus queue does not back up behind a
receiver. A separate delivery task owns the retry loop and the blocking sleeps.

The dispatcher's queue is bounded too — an unbounded one merely converts event loss
into memory exhaustion — but its overflow is **recorded as a dead letter** rather than
logged and forgotten. Shedding load is sometimes unavoidable; doing it silently is
not, and that distinction is the actual closure.

## The dead-letter record is durable

Failures are written to ``caliber_webhook_dead_letters`` when a session factory is
supplied, with the in-memory ring retained as a cache for the operations endpoint and
as the only store when no database is bound (unit tests, a bus with no app). A restart
used to erase the failure record exactly when an operator rebooted to fix the
receiver.

We use ``urllib.request`` (stdlib) rather than ``httpx`` to keep the
runtime dependency set tight. The synchronous POST is run on a thread
via ``asyncio.to_thread`` so a slow receiver doesn't block the event
loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import Any, Final

from caliber.events.bus import EventBus
from caliber.observability.trace import bind_trace_id

logger = logging.getLogger("caliber.events.webhooks")

_TIMESTAMP_HEADER: Final[str] = "X-Caliber-Timestamp"
_SIGNATURE_HEADER: Final[str] = "X-Caliber-Signature"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
# 2xx is success; anything >= this is treated as receiver failure and
# logged. Named so the comparison in :meth:`_post` reads as the spec.
_HTTP_2XX_CEILING: Final[int] = 300
# 4xx means the receiver understood and rejected the request; replaying an
# identical payload cannot change that, so it is permanent. 5xx and transport
# errors are transient and worth retrying.
_HTTP_CLIENT_ERROR_FLOOR: Final[int] = 400
_HTTP_SERVER_ERROR_FLOOR: Final[int] = 500
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_BACKOFF_SECONDS: Final[float] = 0.5
#: Bounded so a long outage cannot grow memory without limit. Oldest entries are
#: evicted, and the eviction count is reported so the surface never implies it is
#: complete when it is not. With a session factory bound, the durable table is the
#: real record and this is a cache of the newest entries.
_DEAD_LETTER_CAPACITY: Final[int] = 200
#: Pending events held between the bus subscriber and the delivery loop. Deep enough
#: to ride out a receiver outage of a few minutes at normal event rates, bounded so a
#: permanent outage cannot exhaust memory. Overflow is dead-lettered, not dropped.
_DEFAULT_PENDING_CAPACITY: Final[int] = 2048
#: Kinds of loss, kept distinct because they have different causes and fixes.
KIND_EXHAUSTED: Final[str] = "exhausted"
KIND_OVERFLOW: Final[str] = "overflow"
#: Accepted but still queued when the process stopped. A third kind because the
#: operator action differs again: the receiver was fine and CALIBER was not
#: overloaded — the event simply had not been sent yet, so it is a replay candidate
#: rather than a capacity or receiver problem.
KIND_SHUTDOWN: Final[str] = "shutdown"


class WebhookDeliveryError(Exception):
    """A delivery attempt failed. ``permanent`` suppresses further retries."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class WebhookDispatcher:
    """Subscribe-to-bus / sign / POST loop.

    Constructed once per app in :func:`caliber.server.create_app`.
    Started + stopped by the Starlette lifespan alongside the worker,
    poller, and janitor.

    Parameters
    ----------
    bus:
        The in-process event bus to subscribe to.
    urls:
        Iterable of HTTPS URLs that receive the POSTs.
    secret:
        The signing key. Empty string disables dispatch (the receiver
        couldn't verify anyway, so we degrade silently rather than
        sending plaintext events to the wire). Operators get a logged
        warning at startup if the secret is empty but URLs are set.
    event_filter:
        Set of event types this dispatcher cares about. Pass ``None``
        (or ``{"*"}``) to subscribe to everything.
    timeout_seconds:
        Per-request HTTP timeout. Defaults to 5s — receivers that need
        longer should respond fast and process async on their end.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        urls: list[str],
        secret: str,
        event_filter: frozenset[str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] | None = None,
        session_factory: Any | None = None,
        pending_capacity: int = _DEFAULT_PENDING_CAPACITY,
    ) -> None:
        self._bus = bus
        self._urls = list(urls)
        self._secret = secret.encode("utf-8") if secret else b""
        self._event_filter = event_filter
        self._timeout = timeout_seconds
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = max(0.0, float(backoff_seconds))
        # Injectable so retry timing is testable without real waiting. The
        # dispatch path already runs on a worker thread, so a blocking sleep here
        # does not stall the event loop.
        self._sleep = sleep if sleep is not None else time.sleep
        # ``None`` keeps the ring as the only store, which is correct for unit tests
        # and any bus with no app behind it. When present, the table is the record.
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # Between the bus subscriber and the delivery loop. The subscriber's only job
        # is to move events here, so the bus's own bounded queue never backs up behind
        # a slow receiver — that backpressure was L6's event loss.
        self._pending_capacity = max(1, int(pending_capacity))
        self._pending: asyncio.Queue[dict[str, Any]] | None = None
        self._dead_letters: deque[dict[str, Any]] = deque(maxlen=_DEAD_LETTER_CAPACITY)
        self._dead_letter_lock = threading.Lock()
        self._dropped_dead_letters = 0
        self._delivered = 0
        self._retried = 0
        self._overflowed = 0

    def dead_letters(self) -> dict[str, Any]:
        """Events that were never delivered, by either loss mode.

        ``dropped`` counts entries evicted by the in-memory ring's capacity, so a
        caller can never mistake a truncated list for the whole failure history. With
        a session factory bound the durable table holds the complete record; this
        surface stays a fast, allocation-free read for the operations endpoint.
        """
        with self._dead_letter_lock:
            return {
                "entries": list(self._dead_letters),
                "count": len(self._dead_letters),
                "dropped": self._dropped_dead_letters,
                "capacity": _DEAD_LETTER_CAPACITY,
                "delivered": self._delivered,
                "retried": self._retried,
                "overflowed": self._overflowed,
                "pending": self._pending.qsize() if self._pending is not None else 0,
                "pending_capacity": self._pending_capacity,
                "durable": self._session_factory is not None,
            }

    def _record_dead_letter(
        self,
        url: str,
        event: dict[str, object],
        reason: str,
        *,
        kind: str = KIND_EXHAUSTED,
        attempts: int | None = None,
    ) -> None:
        entry = {
            "url": url,
            "event_type": event.get("type"),
            "reason": reason,
            "kind": kind,
            "attempts": self._max_attempts if attempts is None else attempts,
            "failed_at_ms": int(time.time() * 1000),
        }
        with self._dead_letter_lock:
            if len(self._dead_letters) == _DEAD_LETTER_CAPACITY:
                self._dropped_dead_letters += 1
            self._dead_letters.append(entry)
            if kind == KIND_OVERFLOW:
                self._overflowed += 1
        self._persist_dead_letter(entry, event)

    def _persist_dead_letter(self, entry: dict[str, Any], event: dict[str, object]) -> None:
        """Write the failure to ``caliber_webhook_dead_letters``.

        Swallows its own errors: the caller has already failed to deliver an event,
        and raising here would replace a recorded delivery failure with an unhandled
        exception in the delivery loop — losing both the event and the record of it.
        """
        if self._session_factory is None:
            return
        try:
            from caliber.db.models import CaliberWebhookDeadLetter  # noqa: PLC0415
            from caliber.ids import new_webhook_dead_letter_id  # noqa: PLC0415

            with self._session_factory() as session:
                session.add(
                    CaliberWebhookDeadLetter(
                        dead_letter_id=new_webhook_dead_letter_id(),
                        url=entry["url"][:1024],
                        event_type=(
                            str(entry["event_type"])[:128] if entry["event_type"] else None
                        ),
                        event=event if isinstance(event, dict) else None,
                        reason=str(entry["reason"])[:2000],
                        attempts=int(entry["attempts"]),
                        kind=str(entry["kind"]),
                        status="open",
                    )
                )
                session.commit()
        except Exception:
            logger.warning("could not persist webhook dead letter", exc_info=True)

    @property
    def is_enabled(self) -> bool:
        """Dispatch fires only when URLs *and* a secret are configured."""
        return bool(self._urls) and bool(self._secret)

    async def start(self) -> None:
        """Start subscribing — no-op when no URLs are configured."""
        if self._task is not None:
            raise RuntimeError("WebhookDispatcher.start() called while already running")
        if not self._urls:
            logger.info("webhook dispatcher disabled (no URLs configured)")
            return
        if not self._secret:
            logger.warning(
                "webhook dispatcher has URLs but no signing secret; "
                "events will *not* be sent. Set the secret-env-var named by "
                "config.webhook_signing_secret_env."
            )
            return
        self._stopped.clear()
        self._pending = asyncio.Queue(maxsize=self._pending_capacity)
        self._task = asyncio.create_task(self._run(), name="caliber.webhook_dispatcher")
        # A second task so the retry loop's blocking waits never hold up bus
        # consumption. One task doing both was L6.
        self._delivery_task = asyncio.create_task(
            self._deliver_forever(), name="caliber.webhook_delivery"
        )
        logger.info(
            "webhook dispatcher started (urls=%d, events=%s, pending_capacity=%d)",
            len(self._urls),
            sorted(self._event_filter) if self._event_filter else "*",
            self._pending_capacity,
        )

    async def stop(self) -> None:
        """Stop the subscription and the delivery loop, **recording what was queued**.

        Pending events are deliberately not *delivered* on the way out: a stop is a
        shutdown or a restart, and blocking either on a receiver that is already failing
        is how a deploy hangs.

        They are, however, **dead-lettered**. An earlier version of this method claimed
        undelivered events "stay in the durable dead-letter record via the
        overflow/exhaustion paths", and that was wrong — an independent review caught it.
        Those paths only cover events that overflowed the queue or exhausted their
        retries; anything merely *sitting* in the queue was discarded on cancellation
        with no record at all, so a graceful restart silently lost accepted events.

        Draining to the durable record costs one database write per queued event and no
        network calls, so it keeps shutdown fast while making the loss observable and
        replayable. That is the whole point of the dead-letter table: an event a
        downstream system was never told about must not vanish quietly.
        """
        if self._task is None and self._delivery_task is None:
            return
        self._stopped.set()
        for task in (self._task, self._delivery_task):
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._delivery_task = None
        abandoned = self._drain_pending_to_dead_letters()
        self._pending = None
        if abandoned:
            logger.warning(
                "webhook dispatcher stopped with %d undelivered event(s); "
                "recorded as %r dead letters for replay",
                abandoned,
                KIND_SHUTDOWN,
            )
        else:
            logger.info("webhook dispatcher stopped")

    def _drain_pending_to_dead_letters(self) -> int:
        """Record every still-queued event as a ``shutdown`` dead letter. Returns the count.

        ``get_nowait`` in a loop rather than ``await get()``: the delivery task is
        already cancelled, nothing will refill the queue, and stopping must not block.
        """
        if self._pending is None:
            return 0
        drained = 0
        while True:
            try:
                event = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            drained += 1
            for url in self._urls:
                self._record_dead_letter(
                    url,
                    event,
                    "dispatcher stopped before this event was delivered",
                    kind=KIND_SHUTDOWN,
                    attempts=0,
                )

    async def _run(self) -> None:
        """Subscribe to the bus and hand matching events to the delivery loop.

        This coroutine must stay fast. The bus gives each subscriber a bounded queue
        that *drops* on overflow, so any time spent here — a retry backoff, a slow
        POST — is time during which the bus discards later events with no record.
        Enqueueing is the only work done per event.

        The subscription is typed as :class:`AsyncGenerator` so we can
        call ``aclose()`` in the ``finally`` block — :class:`AsyncIterator`
        doesn't expose that method, even though the actual return type
        of ``bus.subscribe`` is the wider generator.
        """
        subscription: AsyncGenerator[dict[str, Any], None] = self._bus.subscribe()  # type: ignore[assignment]
        try:
            async for event in subscription:
                if self._stopped.is_set():
                    return
                if not self._should_dispatch(event):
                    continue
                self._enqueue(event)
        except asyncio.CancelledError:
            raise
        finally:
            await subscription.aclose()

    def _enqueue(self, event: dict[str, Any]) -> None:
        """Queue an event for delivery, dead-lettering it if there is no room.

        ``put_nowait`` rather than ``await put``: awaiting would apply backpressure to
        the bus subscriber, which is exactly the coupling being removed — the bus
        would then drop events with only a log line to show for it. Overflow here is
        recorded instead, so shedding load is visible.
        """
        if self._pending is None:  # pragma: no cover - start() always creates it
            return
        try:
            self._pending.put_nowait(event)
        except asyncio.QueueFull:
            logger.error(
                "webhook pending queue full (capacity=%d); dead-lettering event=%r",
                self._pending_capacity,
                event.get("type"),
            )
            for url in self._urls:
                self._record_dead_letter(
                    url,
                    event,
                    f"pending delivery queue full at {self._pending_capacity} events",
                    kind=KIND_OVERFLOW,
                    attempts=0,
                )

    async def _deliver_forever(self) -> None:
        """Own the retry loop, off the bus subscriber's critical path."""
        assert self._pending is not None
        try:
            while True:
                event = await self._pending.get()
                try:
                    # Each dispatch runs under its own trace ID so a webhook
                    # POST and the originating state-change can be
                    # correlated in the JSON-log stream.
                    with bind_trace_id():
                        await asyncio.to_thread(self._post_to_all, event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A bug in delivery must not kill the loop and silently stop every
                    # future webhook; the event's own failure is already recorded.
                    logger.exception("webhook delivery raised; continuing")
                finally:
                    self._pending.task_done()
        except asyncio.CancelledError:
            raise

    def _should_dispatch(self, event: dict[str, object]) -> bool:
        if event.get("_caliber_remote") is True:
            return False
        if self._event_filter is None or "*" in self._event_filter:
            return True
        event_type = event.get("type")
        return isinstance(event_type, str) and event_type in self._event_filter

    def _post_to_all(self, event: dict[str, object]) -> None:
        """POST a single event to every configured URL.

        Synchronous: this method runs on a thread via
        :func:`asyncio.to_thread` so a slow receiver doesn't block the
        event loop. Failures are logged per-URL; one bad receiver
        doesn't taint the others.
        """
        payload = json.dumps(event, separators=(",", ":"), default=str)
        timestamp = str(int(time.time()))
        signature = compute_signature(self._secret, timestamp, payload)
        body = payload.encode("utf-8")
        for url in self._urls:
            self._deliver_with_retry(url, body, timestamp, signature, event)

    def _deliver_with_retry(
        self,
        url: str,
        body: bytes,
        timestamp: str,
        signature: str,
        event: dict[str, object],
    ) -> None:
        """POST with exponential backoff; dead-letter after the last attempt.

        The signature is computed once and reused across attempts: re-signing with
        a fresh timestamp on each retry would look like a distinct event to a
        receiver deduplicating on it.
        """
        last_reason = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._post(url, body, timestamp, signature)
                self._delivered += 1
                return
            except WebhookDeliveryError as exc:
                last_reason = str(exc)
                if exc.permanent:
                    break
            except Exception as exc:
                last_reason = str(exc) or exc.__class__.__name__
            if attempt < self._max_attempts:
                self._retried += 1
                logger.warning(
                    "webhook POST failed, retrying: url=%s event=%r attempt=%d/%d err=%s",
                    url,
                    event.get("type"),
                    attempt,
                    self._max_attempts,
                    last_reason,
                )
                self._sleep(self._backoff * (2 ** (attempt - 1)))
        logger.error(
            "webhook delivery exhausted, dead-lettering: url=%s event=%r err=%s",
            url,
            event.get("type"),
            last_reason,
        )
        self._record_dead_letter(url, event, last_reason)

    def _post(self, url: str, body: bytes, timestamp: str, signature: str) -> None:
        req = urllib.request.Request(  # noqa: S310 — URL comes from operator config
            url=url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                _TIMESTAMP_HEADER: timestamp,
                _SIGNATURE_HEADER: f"t={timestamp},v1={signature}",
                "User-Agent": "caliber",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:  # noqa: S310
                status = response.status
        except urllib.error.HTTPError as exc:
            # urllib raises for 4xx/5xx rather than returning them, so the
            # permanent/transient split has to be made here too.
            permanent = _HTTP_CLIENT_ERROR_FLOOR <= exc.code < _HTTP_SERVER_ERROR_FLOOR
            raise WebhookDeliveryError(
                f"receiver returned HTTP {exc.code}", permanent=permanent
            ) from exc
        if status >= _HTTP_2XX_CEILING:
            permanent = _HTTP_CLIENT_ERROR_FLOOR <= status < _HTTP_SERVER_ERROR_FLOOR
            raise WebhookDeliveryError(f"receiver returned HTTP {status}", permanent=permanent)


def compute_signature(secret: bytes, timestamp: str, payload: str) -> str:
    """Compute the Stripe-style HMAC-SHA256 signature hex digest.

    Receivers reproduce this calculation and compare with
    :func:`hmac.compare_digest` (constant-time) to verify authenticity.

    Exposed at module level so tests can pin the format without going
    through the dispatcher class.
    """
    signed_payload = f"{timestamp}.{payload}".encode()
    return hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()


def build_dispatcher(
    *,
    bus: EventBus,
    urls_csv: str,
    secret_env_var: str,
    event_filter_csv: str,
    environ: dict[str, str] | None = None,
    session_factory: Any | None = None,
) -> WebhookDispatcher:
    """Construct a dispatcher from CALIBER config + the live environment.

    The ``secret_env_var`` config field accepts either a bare env-var
    name (backwards compatible) or a :mod:`caliber.secrets` URI
    (``env://X``, ``file:///abs/path``) — both flow through
    :func:`resolve_secret`. The secret value never lands on the
    config object so it can't leak into a serialized config dump or
    an audit log.

    ``session_factory`` makes the dead-letter record durable. Optional so a caller
    with no database (tests, an embedded bus) still gets a working dispatcher with the
    in-memory ring rather than an error.
    """
    # Lazy import — same reason as in caliber.csrf.build_token_manager.
    from caliber.secrets import resolve_secret  # noqa: PLC0415

    urls = [u.strip() for u in urls_csv.split(",") if u.strip()]
    secret = resolve_secret(secret_env_var, environ=environ) or ""
    event_filter = _parse_filter(event_filter_csv)
    return WebhookDispatcher(
        bus=bus,
        urls=urls,
        secret=secret,
        event_filter=event_filter,
        session_factory=session_factory,
    )


def _parse_filter(csv: str) -> frozenset[str] | None:
    parts = {entry.strip() for entry in csv.split(",") if entry.strip()}
    if not parts or "*" in parts:
        return None
    return frozenset(parts)
