"""Trace-ID propagation primitives.

A single per-request trace ID flows through CALIBER so log lines and
metrics can be correlated end-to-end. Two parts:

* A :class:`contextvars.ContextVar` — :data:`trace_id_var` — that any
  module can read to see the active trace ID. Each ASGI request sets it
  in the middleware below and resets it on the way out. Background tasks
  (worker, poller) call :func:`bind_trace_id` to attach a fresh ID at
  the start of every tick so their log lines correlate with the task's
  iteration even though no HTTP request fired them.
* A Starlette middleware — :class:`TraceIdMiddleware` — that reads
  ``X-Request-Id``/``X-Trace-Id`` from the request (or generates a
  short trace ID), pushes it onto the context var, and echoes it back
  on the response. The header echo lets upstream reverse proxies and
  load testers correlate their own log lines with CALIBER's.

The implementation is dependency-free — no OpenTelemetry, no structlog
— so the observability stack stays small until the production deploy
actually needs richer tracing.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator, Iterator, MutableMapping
from contextvars import ContextVar
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Headers we read inbound. We accept both ``X-Request-Id`` (the de facto
# proxy convention) and ``X-Trace-Id`` (what a few SDKs send), in that
# order; the first non-empty value wins.
_INBOUND_HEADERS: Final[tuple[bytes, ...]] = (b"x-request-id", b"x-trace-id")

# Outbound: a single header that downstream observability tools can
# pivot on. Matches the inbound name so the round-trip is symmetric.
_OUTBOUND_HEADER: Final[bytes] = b"x-request-id"

# The active trace ID for whatever logical operation is running on this
# task. Empty string means "no trace bound" — log records render that
# as a missing field rather than a string ``""``.
trace_id_var: ContextVar[str] = ContextVar("caliber_trace_id", default="")


def current_trace_id() -> str:
    """Return the active trace ID, or empty string if none is bound."""
    return trace_id_var.get()


def new_trace_id() -> str:
    """Generate a short, URL-safe trace ID.

    16 hex chars is ~64 bits of randomness — plenty for the scale
    CALIBER targets (single-org deployments, not multi-tenant SaaS at
    hyperscale). Short enough to fit in a log line without dwarfing the
    rest of the record.
    """
    return secrets.token_hex(8)


@contextlib.contextmanager
def bind_trace_id(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace ID for the duration of a ``with`` block.

    Used by background tasks (worker tick, poller tick) so every log
    line emitted during one iteration shares a trace ID. The default —
    ``None`` — generates a fresh ID; callers can pass an explicit value
    when they need to correlate with an upstream operation.
    """
    resolved = trace_id or new_trace_id()
    token = trace_id_var.set(resolved)
    try:
        yield resolved
    finally:
        trace_id_var.reset(token)


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class TraceIdMiddleware:
    """ASGI middleware that sets ``trace_id_var`` per request.

    Implemented as a raw ASGI middleware (not :class:`BaseHTTPMiddleware`)
    so the trace ID survives streaming responses — ``BaseHTTPMiddleware``
    runs the response in a separate task with its own context, which
    breaks ``contextvars`` propagation. The raw form keeps the
    request and response in the same task.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        inbound = _read_inbound_header(scope)
        trace_id = inbound or new_trace_id()
        token = trace_id_var.set(trace_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                msg: MutableMapping[str, Any] = message
                headers = list(msg.get("headers") or [])
                headers.append((_OUTBOUND_HEADER, trace_id.encode("ascii")))
                msg["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            trace_id_var.reset(token)


def _read_inbound_header(scope: Scope) -> str | None:
    """Return the first non-empty value of any recognized trace header.

    Header names are case-insensitive per RFC 7230; Starlette/ASGI gives
    us lower-case bytes so we match against ``_INBOUND_HEADERS``.
    """
    raw_headers: Any = scope.get("headers") or ()
    for name, value in raw_headers:
        if name in _INBOUND_HEADERS and value:
            try:
                decoded: str = value.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if decoded:
                # Cap to a reasonable length so a malicious caller can't
                # blow up every log line.
                return decoded[:64]
    return None


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _bind_for_test(trace_id: str) -> AsyncIterator[None]:
    """Async-friendly variant of :func:`bind_trace_id`, used by tests
    that drive the middleware from outside the ASGI stack."""
    token = trace_id_var.set(trace_id)
    try:
        yield
    finally:
        trace_id_var.reset(token)
