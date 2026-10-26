"""Bound the size of a request body, whether or not the client says how big it is.

``BodySizeLimitMiddleware`` is a pure ASGI middleware installed by the reference profile
(``CALIBER_PROFILE=reference``, ``config.reference_max_body_bytes``). It answers **413** with
``{"reason": "request_too_large", "max_bytes": N}`` in two cases:

* the request declares a ``content-length`` above the limit: refused before a byte of the body is read;
* the body is streamed (chunked, or a ``content-length`` that understates it) and the bytes received pass the
  limit: the response is sent at once and the application is told the client disconnected, so it stops reading.

The limit is on the body, not on a route: a handler that buffers an unbounded upload is one request away from
exhausting memory, and the platform has many such handlers. A request at exactly the limit is accepted.

Three limits apply, the largest that matches winning, so the default can never be *lower* than a route's own:

* ``max_bytes``: every request;
* ``multipart_max_bytes``: a ``multipart/form-data`` request (the file-upload routes: skill zips, attachments,
  workspace imports, storage objects). Starlette spools these parts to disk, and each route then enforces its own,
  tighter limit;
* ``path_limits``: an exact path with a limit of its own (the GitHub webhook route, which enforces
  ``github_webhook_max_body_bytes``).

A refusal carries the request's trace id (``x-request-id``, echoed from the caller or generated here, because this
middleware runs outside the trace middleware) and logs one warning line. A client that sends a very large body
without waiting for the response may see a connection reset instead of the 413: the body is refused, not drained.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from caliber.observability.trace import _read_inbound_header, new_trace_id

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REASON = "request_too_large"


def too_large_body(max_bytes: int) -> bytes:
    return json.dumps({"reason": REASON, "max_bytes": max_bytes}).encode()


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        *,
        multipart_max_bytes: int | None = None,
        path_limits: Mapping[str, int] | None = None,
    ) -> None:
        for limit in (max_bytes, multipart_max_bytes, *(path_limits or {}).values()):
            if limit is not None and limit <= 0:
                raise ValueError("body size limits must be positive")
        self.app = app
        self.max_bytes = max_bytes
        self.multipart_max_bytes = multipart_max_bytes
        self.path_limits = dict(path_limits or {})

    def limit_for(self, scope: Scope) -> int:
        """The largest limit that applies to this request; ``max_bytes`` is the floor."""
        limit = self.max_bytes
        if self.multipart_max_bytes is not None and _is_multipart(scope):
            limit = max(limit, self.multipart_max_bytes)
        return max(limit, self.path_limits.get(scope.get("path", ""), 0))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await self._refuse(scope, send, limit, f"declared {declared} bytes")
            return

        received = 0
        refused = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    refused = True
                    if not response_started:
                        await self._refuse(scope, send, limit, f"streamed over {limit} bytes")
                    # Otherwise the application already began its own response: a second one cannot be sent,
                    # so the application is only told the client left and the connection ends with it.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # Once refused, whatever the application still tries to say is dropped.
            if refused:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # After the refusal the application is told the client left, and a handler that was reading the body
            # raises for that (Starlette's ``ClientDisconnect``). That is the refusal working, not a server
            # error: swallow it. Any other failure, or one before the refusal, is the application's to report.
            if not refused:
                raise

    async def _refuse(self, scope: Scope, send: Send, limit: int, why: str) -> None:
        trace_id = _read_inbound_header(scope) or new_trace_id()
        logger.warning(
            "request body refused: %s %s %s (limit %d, trace %s)",
            scope.get("method", "?"),
            scope.get("path", "?"),
            why,
            limit,
            trace_id,
        )
        body = too_large_body(limit)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                    (b"x-request-id", trace_id.encode("ascii", "replace")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _is_multipart(scope: Scope) -> bool:
    for name, value in scope.get("headers", []):
        if name == b"content-type":
            return bytes(value).lower().lstrip().startswith(b"multipart/form-data")
    return False


def _declared_length(scope: Scope) -> int | None:
    """The largest well-formed ``content-length`` the request declares (a repeated header is not trusted to agree)."""
    sizes: list[int] = []
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                sizes.append(int(value))
            except ValueError:
                continue  # a malformed header is not a size; the stream count still applies
    return max(sizes) if sizes else None
