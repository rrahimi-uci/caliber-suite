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

The dispatcher is best-effort for v1 — a failed POST is logged with the
event type and status code, but not retried. Retries with exponential
backoff land in a follow-up milestone alongside a queue table; for now
the operator's expectation is "we tell you when something happens, but
your receiver needs to be reliable enough to absorb the occasional
outage." That matches what Slack / PagerDuty integration shapes look
like in OSS land.

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
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator
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
    ) -> None:
        self._bus = bus
        self._urls = list(urls)
        self._secret = secret.encode("utf-8") if secret else b""
        self._event_filter = event_filter
        self._timeout = timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

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
        self._task = asyncio.create_task(self._run(), name="caliber.webhook_dispatcher")
        logger.info(
            "webhook dispatcher started (urls=%d, events=%s)",
            len(self._urls),
            sorted(self._event_filter) if self._event_filter else "*",
        )

    async def stop(self) -> None:
        """Stop the subscription. Cheap operation — no graceful drain
        needed because each POST is bounded by ``timeout_seconds``."""
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("webhook dispatcher stopped")

    async def _run(self) -> None:
        """Subscribe to the bus and dispatch matching events until stopped.

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
                # Each dispatch runs under its own trace ID so a webhook
                # POST and the originating state-change can be
                # correlated in the JSON-log stream.
                with bind_trace_id():
                    await asyncio.to_thread(self._post_to_all, event)
        except asyncio.CancelledError:
            raise
        finally:
            await subscription.aclose()

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
            try:
                self._post(url, body, timestamp, signature)
            except Exception as exc:
                logger.warning(
                    "webhook POST failed: url=%s event=%r err=%s",
                    url,
                    event.get("type"),
                    exc,
                )

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
        with urllib.request.urlopen(req, timeout=self._timeout) as response:  # noqa: S310
            # 2xx is fine; anything else gets logged so an operator can
            # see receiver-side failures without running a debugger.
            status = response.status
            if status >= _HTTP_2XX_CEILING:
                logger.warning("webhook receiver returned non-2xx: url=%s status=%d", url, status)


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
) -> WebhookDispatcher:
    """Construct a dispatcher from CALIBER config + the live environment.

    The ``secret_env_var`` config field accepts either a bare env-var
    name (backwards compatible) or a :mod:`caliber.secrets` URI
    (``env://X``, ``file:///abs/path``) — both flow through
    :func:`resolve_secret`. The secret value never lands on the
    config object so it can't leak into a serialized config dump or
    an audit log.
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
    )


def _parse_filter(csv: str) -> frozenset[str] | None:
    parts = {entry.strip() for entry in csv.split(",") if entry.strip()}
    if not parts or "*" in parts:
        return None
    return frozenset(parts)
