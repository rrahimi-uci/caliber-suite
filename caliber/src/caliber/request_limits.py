"""Bound the size of a request body, whether or not the client says how big it is.

``BodySizeLimitMiddleware`` is a pure ASGI middleware installed by the reference profile
(``CALIBER_PROFILE=reference``, ``config.reference_max_body_bytes``). It answers **413** with
``{"reason": "request_too_large", "max_bytes": N}`` in two cases:

* the request declares a ``content-length`` above the limit: refused before a byte of the body is read;
* the body is streamed (chunked, or a ``content-length`` that understates it) and the bytes received pass the
  limit: the response is sent at once and the application is told the client disconnected, so it stops reading.

The limit is on the body, not on a route: a handler that buffers an unbounded upload is one request away from
exhausting memory, and the platform has many such handlers. A request at exactly the limit is accepted.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REASON = "request_too_large"


def too_large_body(max_bytes: int) -> bytes:
    return json.dumps({"reason": REASON, "max_bytes": max_bytes}).encode()


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._refuse(send)
            return

        received = 0
        refused = False

        async def limited_receive() -> Message:
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    refused = True
                    await self._refuse(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            # Once the 413 is out, whatever the application still tries to say is dropped.
            if not refused:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # After the refusal the application is told the client left, and a handler that was reading the body
            # raises for that (Starlette's ``ClientDisconnect``). That is the refusal working, not a server
            # error: swallow it. Any other failure, or one before the refusal, is the application's to report.
            if not refused:
                raise

    async def _refuse(self, send: Send) -> None:
        body = too_large_body(self.max_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None  # a malformed header is not a size; the stream count still applies
    return None
