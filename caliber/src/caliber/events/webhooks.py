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
import contextlib
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from caliber.events.bus import EventBus
from caliber.observability.trace import bind_trace_id

logger = logging.getLogger("caliber.events.webhooks")

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect away from the configured destination.

    ``urllib.request.urlopen`` installs the default opener, which follows
    redirects. That turns an operator-configured URL into a URL the *receiver*
    chooses: a compromised or merely misconfigured endpoint answering 302 sends
    CALIBER wherever it likes, including the cloud metadata endpoint and
    CALIBER's own loopback API.

    Measured before this handler existed, against a local pair of servers: a 302
    was followed to the second host and arrived carrying ``X-Caliber-Signature``
    and ``X-Caliber-Timestamp`` intact. urllib rewrites POST to GET on a 302, so
    the body was dropped — but a valid HMAC signature for that payload had
    already been handed to an unauthorized host, which is the disclosure that
    matters. (307 raised rather than following, so only the 301/302/303 path was
    exposed; refusing all of them is the only durable answer.)

    Returning ``None`` makes urllib stop and surface the redirect as an
    ``HTTPError``, which ``_post`` already classifies: 3xx is neither 2xx nor a
    4xx client error, so it becomes a transient failure, retries, and finally
    dead-letters with the status visible to an operator. A redirect is a
    misconfigured destination, and that is exactly how it now reads.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        return None


#: Shared opener with redirects disabled. Module-level so every delivery uses
#: the same policy and no call site can forget it.
_OPENER: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(_NoRedirect)


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
#: Distinguishes "was not in the map" from "mapped to None" when atomically claiming an
#: in-flight target, so the claim cannot be won twice.
_UNSET: Any = object()
#: Pending events held between the bus subscriber and the delivery loop. Deep enough
#: to ride out a receiver outage of a few minutes at normal event rates, bounded so a
#: permanent outage cannot exhaust memory. Overflow is dead-lettered, not dropped.
_DEFAULT_PENDING_CAPACITY: Final[int] = 2048
#: A live dispatcher renews its accepted rows at one third of this interval. The default is
#: also sized from request/retry timing in ``__init__`` so ordinary delivery cannot outlive
#: its owner merely because an operator raised the HTTP timeout.
_DEFAULT_ACCEPT_LEASE_SECONDS: Final[float] = 60.0
#: Kinds of loss, kept distinct because they have different causes and fixes.
KIND_EXHAUSTED: Final[str] = "exhausted"
KIND_OVERFLOW: Final[str] = "overflow"
#: Accepted but still queued when the process stopped. A third kind because the
#: operator action differs again: the receiver was fine and CALIBER was not
#: overloaded — the event simply had not been sent yet, so it is a replay candidate
#: rather than a capacity or receiver problem.
KIND_SHUTDOWN: Final[str] = "shutdown"
#: The database could not durably accept the event, so the dispatcher deliberately did not
#: send it. Different from receiver exhaustion: retrying the receiver cannot repair this.
KIND_ACCEPT_FAILED: Final[str] = "accept_failed"


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
        owner_id: str | None = None,
        accept_lease_seconds: float | None = None,
    ) -> None:
        self._bus = bus
        # Duplicate targets are one delivery obligation, not two independent receivers. They
        # also derive the same accepted-row key and previously rolled back the whole batch.
        self._urls = list(dict.fromkeys(urls))
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
        self._lease_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # ``asyncio.to_thread`` cancellation does not stop its worker. This event is
        # generation-specific so a detached sender from a stopped generation cannot
        # resume retries or later targets after a quick restart clears asyncio state.
        self._delivery_stop = threading.Event()
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
        self._accept_rejected = 0
        self._owner_id = (
            owner_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )[:128]
        retry_window = (
            self._max_attempts * max(float(self._timeout), 0.0)
            + sum(self._backoff * (2**index) for index in range(self._max_attempts - 1))
            + 30.0
        )
        self._accept_lease_seconds = max(
            0.1,
            float(
                accept_lease_seconds
                if accept_lease_seconds is not None
                else max(_DEFAULT_ACCEPT_LEASE_SECONDS, retry_window)
            ),
        )
        self._lease_renew_interval = max(0.05, min(self._accept_lease_seconds / 3.0, 30.0))
        #: Targets the delivery loop still owes an outcome for, keyed by URL, for the
        #: event it currently holds. **Per target, not per event**: one event fans out to
        #: every configured URL and each has its own outcome, so an event-level marker
        #: was wrong in both directions — the first target to be dead-lettered cleared it
        #: and the still-blocked targets got no shutdown row, while an event whose first
        #: target had already *succeeded* could be dead-lettered against that delivered
        #: target on the way out.
        self._in_flight: dict[str, dict[str, Any]] = {}
        #: event object id -> durable accept marker, so a settle can find the row the
        #: accept wrote without threading an ID through every delivery path.
        self._event_markers: dict[int, str] = {}
        #: Targets still represented by each marker, and the exact accepted ids this replica
        #: is allowed to renew. Both are pruned as targets settle, preventing one map entry
        #: per event from accumulating for the life of the process.
        self._marker_targets: dict[int, set[str]] = {}
        self._owned_accept_ids: set[str] = set()

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
                "accept_rejected": self._accept_rejected,
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
        settles_in_flight: bool = False,
    ) -> bool:
        # ``settles_in_flight`` closes a double-record race. Two writers can be settling
        # the same (url, event) at once:
        #
        #   * the delivery thread, finishing ``_deliver_with_retry``; and
        #   * ``stop()``'s drain, walking ``_in_flight``.
        #
        # They race because cancelling ``asyncio.to_thread`` does **not** stop the worker
        # thread — the ``await`` raises immediately while the thread runs to completion.
        # So the drain could see ``_in_flight`` still populated, record ``shutdown``, and
        # the still-live thread would then record ``exhausted`` for the same event. An
        # operator saw two rows for one delivery and would double-count it on replay.
        #
        # Claiming the URL *atomically* makes the first writer the only writer. Callers
        # whose rows were never in-flight (overflow, and events still sitting in the
        # queue) pass ``False`` and are unaffected — they have no claim to win.
        if settles_in_flight:
            with self._dead_letter_lock:
                # Match the exact event object as well as its URL. A detached sender from
                # an older start/stop generation must not settle a newer generation's
                # in-flight target merely because it uses the same configured URL.
                if self._in_flight.get(url, _UNSET) is not event:
                    return False  # another writer already settled this target
                self._in_flight.pop(url, None)
        accepted_id = self._accepted_row_id(url, event)
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
        # A non-settling caller must **not** touch ``_in_flight``, and this too was a
        # defect an independent probe found. Both non-settling callers — queue overflow and
        # the stop-time queue drain — concern events that never left the queue, so they
        # never had a marker of their own. Popping by URL therefore discharged whichever
        # *other* event happened to be in flight at that URL: with event 1 blocked in
        # delivery and event 3 overflowing at the same URL, recording event 3 erased event
        # 1's marker and event 1 got no durable outcome at all.
        #
        # Only the owner settles its own marker: the delivery path (via
        # ``settles_in_flight``) or the in-flight branch of the drain.
        # One transaction writes the replay evidence and consumes the accept row. If the
        # insert fails, the delete rolls back with it and the expired accept row remains for
        # another recovery attempt. The local ownership set is still pruned so this replica
        # stops renewing work it could not durably settle.
        self._persist_dead_letter(entry, event, accepted_id=accepted_id or None)
        if accepted_id:
            self._forget_marker_target(url, event, accepted_id)
        return True

    def _persist_dead_letter(
        self,
        entry: dict[str, Any],
        event: dict[str, object],
        *,
        accepted_id: str | None = None,
    ) -> bool:
        """Write the failure to ``caliber_webhook_dead_letters``.

        When ``accepted_id`` is supplied, insertion and accept-row deletion share one
        transaction. Swallows its own errors because the in-memory record still makes the
        failure visible; importantly, an error leaves the accept row available for recovery.
        """
        if self._session_factory is None:
            return True
        try:
            from sqlalchemy import delete as _delete  # noqa: PLC0415

            from caliber.db.models import (  # noqa: PLC0415
                CaliberWebhookAcceptedEvent,
                CaliberWebhookDeadLetter,
            )
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
                # Flush first: a constraint/storage failure on the durable evidence must be
                # observed before the accept row is consumed.
                session.flush()
                if accepted_id:
                    consumed = session.execute(
                        _delete(CaliberWebhookAcceptedEvent).where(
                            CaliberWebhookAcceptedEvent.accepted_id == accepted_id,
                            CaliberWebhookAcceptedEvent.owner_id == self._owner_id,
                        )
                    )
                    if consumed.rowcount != 1:
                        raise RuntimeError(
                            "webhook accept lease was lost before settlement committed"
                        )
                session.commit()
        except Exception:
            logger.warning("could not persist webhook dead letter", exc_info=True)
            return False
        return True

    @property
    def is_enabled(self) -> bool:
        """Dispatch fires only when URLs *and* a secret are configured."""
        return bool(self._urls) and bool(self._secret)

    async def start(self) -> None:
        """Start durable recovery, plus delivery when URLs and a secret are configured."""
        if any(task is not None for task in (self._task, self._delivery_task, self._lease_task)):
            raise RuntimeError("WebhookDispatcher.start() called while already running")
        self._delivery_stop = threading.Event()
        # Before accepting anything new, account for what the previous process accepted and
        # never finished. Abrupt loss skips the graceful drain entirely, so this boot sweep
        # is the only thing that turns a SIGKILL from silent loss into a replayable record.
        # Recovery is independent of *current* URL configuration: removing the final URL
        # must not strand obligations accepted under the previous configuration forever.
        self.recover_accepted_events()
        self._stopped.clear()
        if self._session_factory is not None:
            # Keep sweeping even when current delivery configuration is disabled. A row
            # owned by another live replica during this startup sweep can expire later;
            # without this recovery-only mode it would remain stranded until another
            # process restart happened to occur.
            self._lease_task = asyncio.create_task(
                self._renew_leases_forever(), name="caliber.webhook_accept_lease"
            )
        if not self._urls:
            logger.info(
                "webhook delivery disabled (no URLs configured); durable recovery remains active"
            )
            return
        if not self._secret:
            logger.warning(
                "webhook dispatcher has URLs but no signing secret; "
                "events will *not* be sent. Set the secret-env-var named by "
                "config.webhook_signing_secret_env."
            )
            return
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
        if self._task is None and self._delivery_task is None and self._lease_task is None:
            return
        self._stopped.set()
        # Set before cancelling the asyncio task. Its ``to_thread`` worker continues
        # independently; this is the only signal that prevents it starting another POST.
        self._delivery_stop.set()
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
        if self._lease_task is not None:
            self._lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lease_task
            self._lease_task = None
        if abandoned:
            logger.warning(
                "webhook dispatcher stopped with %d undelivered event(s); "
                "recorded as %r dead letters for replay",
                abandoned,
                KIND_SHUTDOWN,
            )
        else:
            logger.info("webhook dispatcher stopped")

    def replay_event(self, url: str, event: dict[str, Any]) -> tuple[bool, str]:
        """Re-send one previously dead-lettered event. Returns ``(delivered, detail)``.

        Signed fresh rather than replayed with the original signature: the timestamp is
        part of the signed payload, and receivers reject stale ones as replay attacks.
        A redelivery is a new legitimate request, so it gets a new timestamp — a
        receiver deduplicating on event content still sees the same event.

        Synchronous and bounded by the same per-attempt retry policy as first delivery,
        so an operator gets a real answer instead of a queued promise. Never raises:
        the caller records the outcome either way, and an exception here would leave
        the dead letter in an unknown state.
        """
        payload = json.dumps(event, separators=(",", ":"), default=str)
        timestamp = str(int(time.time()))
        signature = compute_signature(self._secret, timestamp, payload)
        body = payload.encode("utf-8")
        try:
            self._post(url, body, timestamp, signature)
        except WebhookDeliveryError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        self._delivered += 1
        return True, "delivered"

    def _drain_pending_to_dead_letters(self) -> int:
        """Record every still-queued event as a ``shutdown`` dead letter. Returns the count.

        ``get_nowait`` in a loop rather than ``await get()``: the delivery task is
        already cancelled, nothing will refill the queue, and stopping must not block.
        """
        drained = 0
        # In-flight targets first: the event was already removed from the queue, so a
        # drain that only walked the queue would miss exactly the delivery most likely to
        # be lost — the one in progress when the stop arrived. One row per *target* still
        # owed an outcome; targets already delivered or already dead-lettered have been
        # removed from the map and are correctly skipped.
        # Snapshot under the lock, but do **not** clear the map: each row below claims its
        # own URL via ``settles_in_flight``, which is what makes the delivery thread and
        # this drain mutually exclusive per target. Clearing here instead would hand the
        # drain every URL unconditionally and re-open the double-record window.
        with self._dead_letter_lock:
            in_flight = dict(self._in_flight)
        for url, event in in_flight.items():
            # Counted only when the row was actually written: a target the delivery thread
            # settled first is not an abandoned event, and reporting it as one would
            # overstate shutdown loss in the operator-facing log line.
            if self._record_dead_letter(
                url,
                event,
                "dispatcher stopped while this event was in flight; delivery outcome unknown",
                kind=KIND_SHUTDOWN,
                attempts=0,
                settles_in_flight=True,
            ):
                drained += 1
        if self._pending is None:
            return drained
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
        # EventBus deliberately fans out the caller's original dict. Publishing the same
        # object twice is valid, but marker state is occurrence-specific; make a shallow
        # copy so each accepted occurrence has a distinct identity and durable row key.
        event = dict(event)
        # Durable accept record first. If the database cannot establish that obligation,
        # do not send an event whose eventual outcome could disappear without evidence.
        if not self._persist_accepted(event):
            with self._dead_letter_lock:
                self._accept_rejected += 1
            logger.error(
                "webhook accept persistence failed; refusing delivery for event=%r",
                event.get("type"),
            )
            for url in self._urls:
                self._record_dead_letter(
                    url,
                    event,
                    "database rejected the durable webhook accept record; event was not sent",
                    kind=KIND_ACCEPT_FAILED,
                    attempts=0,
                )
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

    def _accepted_row_id(
        self,
        url: str,
        event: dict[str, Any],
        *,
        marker: str | None = None,
    ) -> str:
        """Stable per (event, url) so the settle can find the row the accept wrote.

        Derived from the event's identity rather than random, because the accept and the
        settle happen in different call stacks (and, for the in-flight case, different
        threads) and passing an ID through every path would be one more thing to forget.
        """
        if marker is None:
            with self._dead_letter_lock:
                marker = self._event_markers.get(id(event))
        return f"{marker}:{hashlib.sha256(url.encode()).hexdigest()[:16]}" if marker else ""

    def _persist_accepted(self, event: dict[str, Any]) -> bool:
        """Record the accept **before** queueing, one row per target.

        Ordering is the whole point: written afterwards, a crash in between would leave
        the event in flight with no durable trace, which is the case this removes.
        """
        if self._session_factory is None:
            return True
        marker = uuid.uuid4().hex
        accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_expires_at = accepted_at + timedelta(seconds=self._accept_lease_seconds)
        accepted_ids = {url: self._accepted_row_id(url, event, marker=marker) for url in self._urls}
        try:
            from caliber.db.models import CaliberWebhookAcceptedEvent  # noqa: PLC0415

            with self._session_factory() as session:
                for url in self._urls:
                    session.add(
                        CaliberWebhookAcceptedEvent(
                            accepted_id=accepted_ids[url],
                            url=url[:1024],
                            event_type=(
                                str(event.get("type"))[:128] if event.get("type") else None
                            ),
                            event=event,
                            accepted_at=accepted_at,
                            owner_id=self._owner_id,
                            lease_expires_at=lease_expires_at,
                        )
                    )
                session.commit()
        except Exception:
            logger.warning("could not persist webhook accept record", exc_info=True)
            return False
        # Publish the marker only after the transaction commits. A failed accept must not
        # leak per-event state, and a delivery thread cannot settle a row until enqueueing.
        with self._dead_letter_lock:
            self._event_markers[id(event)] = marker
            self._marker_targets[id(event)] = set(self._urls)
            self._owned_accept_ids.update(accepted_ids.values())
        return True

    def _forget_marker_target(
        self,
        url: str,
        event: dict[str, Any],
        accepted_id: str,
    ) -> None:
        """Release local ownership for one settled target and prune empty markers."""
        event_key = id(event)
        with self._dead_letter_lock:
            self._owned_accept_ids.discard(accepted_id)
            targets = self._marker_targets.get(event_key)
            if targets is None:
                return
            targets.discard(url)
            if not targets:
                self._marker_targets.pop(event_key, None)
                self._event_markers.pop(event_key, None)

    def _clear_accepted(self, url: str, event: dict[str, Any]) -> None:
        """Drop the accept row for one settled target."""
        row_id = self._accepted_row_id(url, event)
        if not row_id:
            return
        if self._session_factory is None:
            self._forget_marker_target(url, event, row_id)
            return
        try:
            from sqlalchemy import delete as _delete  # noqa: PLC0415

            from caliber.db.models import CaliberWebhookAcceptedEvent  # noqa: PLC0415

            with self._session_factory() as session:
                session.execute(
                    _delete(CaliberWebhookAcceptedEvent).where(
                        CaliberWebhookAcceptedEvent.accepted_id == row_id,
                        CaliberWebhookAcceptedEvent.owner_id == self._owner_id,
                    )
                )
                session.commit()
        except Exception:
            logger.warning("could not clear webhook accept record", exc_info=True)
        finally:
            # On a failed clear, stop renewing the row so a different replica can recover it
            # once the lease expires. On success this also bounds the marker maps.
            self._forget_marker_target(url, event, row_id)

    def _renew_owned_leases(self) -> int:
        """Extend only this replica's currently unsettled accept rows."""
        if self._session_factory is None:
            return 0
        with self._dead_letter_lock:
            owned_ids = tuple(self._owned_accept_ids)
        if not owned_ids:
            return 0
        try:
            from sqlalchemy import update as _update  # noqa: PLC0415

            from caliber.db.models import CaliberWebhookAcceptedEvent  # noqa: PLC0415

            lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=self._accept_lease_seconds
            )
            with self._session_factory() as session:
                result = session.execute(
                    _update(CaliberWebhookAcceptedEvent)
                    .where(
                        CaliberWebhookAcceptedEvent.accepted_id.in_(owned_ids),
                        CaliberWebhookAcceptedEvent.owner_id == self._owner_id,
                    )
                    .values(lease_expires_at=lease_expires_at)
                )
                session.commit()
                return int(result.rowcount or 0)
        except Exception:
            logger.warning("could not renew webhook accept leases", exc_info=True)
            return 0

    async def _renew_leases_forever(self) -> None:
        """Heartbeat local rows and continuously recover newly expired remote rows."""
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._lease_renew_interval)
                except asyncio.TimeoutError:
                    await asyncio.to_thread(self._renew_owned_leases)
                    # Startup-only recovery strands evidence when another already-running
                    # replica crashes later. Renew ours first, then claim remote rows that
                    # have genuinely expired.
                    await asyncio.to_thread(self.recover_accepted_events)
        except asyncio.CancelledError:
            raise

    def recover_accepted_events(self) -> int:
        """Claim expired accept rows and atomically turn them into dead letters.

        Rows with a live lease belong to another replica and are never swept. Each expired
        candidate is first claimed conditionally, then the dead-letter insert and accepted
        row delete commit together. A failed insert leaves the claimed row to expire and be
        retried later instead of consuming the only durable evidence.
        """
        if self._session_factory is None:
            return 0
        try:
            from sqlalchemy import or_ as _or  # noqa: PLC0415
            from sqlalchemy import select as _select  # noqa: PLC0415
            from sqlalchemy import update as _update  # noqa: PLC0415

            from caliber.db.models import CaliberWebhookAcceptedEvent  # noqa: PLC0415

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with self._session_factory() as session:
                rows = (
                    session.execute(
                        _select(CaliberWebhookAcceptedEvent).where(
                            _or(
                                CaliberWebhookAcceptedEvent.lease_expires_at.is_(None),
                                CaliberWebhookAcceptedEvent.lease_expires_at <= now,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                candidates = [(row.url, row.event, row.accepted_id) for row in rows]
        except Exception:
            logger.warning("could not read webhook accept records", exc_info=True)
            return 0
        recovered = 0
        for url, raw_event, row_id in candidates:
            try:
                claim_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                    seconds=self._accept_lease_seconds
                )
                with self._session_factory() as session:
                    claimed = session.execute(
                        _update(CaliberWebhookAcceptedEvent)
                        .where(
                            CaliberWebhookAcceptedEvent.accepted_id == row_id,
                            _or(
                                CaliberWebhookAcceptedEvent.lease_expires_at.is_(None),
                                CaliberWebhookAcceptedEvent.lease_expires_at <= now,
                            ),
                        )
                        .values(
                            owner_id=self._owner_id,
                            lease_expires_at=claim_expires_at,
                        )
                    )
                    session.commit()
                    if not claimed.rowcount:
                        continue
            except Exception:
                logger.warning("could not claim webhook accept record", exc_info=True)
                continue
            event = raw_event if isinstance(raw_event, dict) else {}
            entry = {
                "url": url,
                "event_type": event.get("type"),
                "reason": "the owning process stopped before delivering this event",
                "kind": KIND_SHUTDOWN,
                "attempts": 0,
                "failed_at_ms": int(time.time() * 1000),
            }
            if not self._persist_dead_letter(entry, event, accepted_id=row_id):
                continue
            with self._dead_letter_lock:
                if len(self._dead_letters) == _DEAD_LETTER_CAPACITY:
                    self._dropped_dead_letters += 1
                self._dead_letters.append(entry)
            recovered += 1
        if recovered:
            logger.warning(
                "recovered %d webhook event(s) accepted by a previous process; "
                "recorded as %r dead letters for replay",
                recovered,
                KIND_SHUTDOWN,
            )
        return recovered

    async def _deliver_forever(self) -> None:
        """Own the retry loop, off the bus subscriber's critical path."""
        assert self._pending is not None
        stop_requested = self._delivery_stop
        try:
            while True:
                event = await self._pending.get()
                # Recorded before the await below. ``get()`` removes the event from the
                # queue, so between here and completion it exists *only* in this local
                # — cancelling the task then dropped it with no durable trace, which is
                # the in-flight half of N5 an independent probe reproduced
                # (``pending_before_stop=0`` and zero dead-letter rows after stop()).
                with self._dead_letter_lock:
                    self._in_flight = dict.fromkeys(self._urls, event)
                try:
                    # Each dispatch runs under its own trace ID so a webhook
                    # POST and the originating state-change can be
                    # correlated in the JSON-log stream.
                    with bind_trace_id():
                        await asyncio.to_thread(
                            self._post_to_all,
                            event,
                            True,
                            stop_requested,
                        )
                except asyncio.CancelledError:
                    # Deliberately leave ``_in_flight`` set: this is the shutdown path,
                    # and ``stop()`` is about to record it. Clearing it in a ``finally``
                    # (as a first attempt did) runs on cancellation too and erases the
                    # event before the drain can see it.
                    raise
                except Exception:
                    # A bug in delivery must not kill the loop and silently stop every
                    # future webhook; the event's own failure is already recorded.
                    logger.exception("webhook delivery raised; continuing")
                with self._dead_letter_lock:
                    self._in_flight = {}
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

    def _post_to_all(
        self,
        event: dict[str, object],
        tracked: bool = False,
        stop_requested: threading.Event | None = None,
    ) -> None:
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
            if stop_requested is not None and stop_requested.is_set():
                return
            self._deliver_with_retry(
                url,
                body,
                timestamp,
                signature,
                event,
                tracked,
                stop_requested,
            )

    def _claim_in_flight_target(self, url: str, event: dict[str, object]) -> bool:
        """Atomically claim this generation's target before settling it."""
        with self._dead_letter_lock:
            if self._in_flight.get(url, _UNSET) is not event:
                return False
            self._in_flight.pop(url, None)
            return True

    def _deliver_with_retry(
        self,
        url: str,
        body: bytes,
        timestamp: str,
        signature: str,
        event: dict[str, object],
        tracked: bool = False,
        stop_requested: threading.Event | None = None,
    ) -> None:
        """POST with exponential backoff; dead-letter after the last attempt.

        ``tracked`` says whether this dispatch is registered in ``_in_flight``, i.e.
        whether ``stop()``'s drain could also be trying to settle the same target. Only a
        tracked dispatch claims the target before recording; an untracked one (a direct
        call, or a replay) has no competing writer, so requiring a claim it could never win
        would silently drop its dead letter entirely.

        The signature is computed once and reused across attempts: re-signing with
        a fresh timestamp on each retry would look like a distinct event to a
        receiver deduplicating on it.
        """
        last_reason = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            if stop_requested is not None and stop_requested.is_set():
                return
            try:
                self._post(url, body, timestamp, signature)
                # Delivered: this target is settled and must not be dead-lettered by a
                # concurrent shutdown drain. Claimed under the same lock the drain uses,
                # because "succeeded" and "abandoned at shutdown" would otherwise both be
                # recorded for one delivery that actually went through.
                if tracked and not self._claim_in_flight_target(url, event):
                    return
                # Settle the durable accept row BEFORE advertising the delivery.
                # ``delivered`` is operations-visible via ``dead_letters()``, so
                # incrementing first published a state that had not happened yet:
                # a reader could observe delivered==N while N accept rows still sat
                # in the table looking unsettled — the exact ambiguity those rows
                # exist to remove. ``_clear_accepted`` swallows its own errors and
                # prunes in a ``finally``, so it cannot skip the increment.
                self._clear_accepted(url, event)
                self._delivered += 1
                return
            except WebhookDeliveryError as exc:
                last_reason = str(exc)
                if exc.permanent:
                    break
            except Exception as exc:
                last_reason = str(exc) or exc.__class__.__name__
            if stop_requested is not None and stop_requested.is_set():
                return
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
                if stop_requested is not None and stop_requested.is_set():
                    return
        if stop_requested is not None and stop_requested.is_set():
            return
        logger.error(
            "webhook delivery exhausted, dead-lettering: url=%s event=%r err=%s",
            url,
            event.get("type"),
            last_reason,
        )
        self._record_dead_letter(url, event, last_reason, settles_in_flight=tracked)

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
            with _OPENER.open(req, timeout=self._timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            # urllib raises for 4xx/5xx rather than returning them, so the
            # permanent/transient split has to be made here too. A refused
            # redirect also lands here as a 3xx, which is neither 2xx nor a 4xx
            # client error and therefore retries and finally dead-letters with
            # the status visible — the right reading for a destination that
            # points somewhere else.
            #
            # ``HTTPError`` is itself a file object holding the response body.
            # The success path closes via ``with``; this path has to close
            # explicitly or the socket is reclaimed by the GC later, surfacing
            # as a ResourceWarning in whichever test happens to be running.
            with contextlib.closing(exc):
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
