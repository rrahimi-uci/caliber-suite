"""Asynchronous transport, sharing every decision with the synchronous one.

The reason this exists is streaming. An ordinary request reads its whole body
before returning, which is fine; an SSE endpoint never ends, so consuming it
synchronously occupies a thread for as long as you want events. That is the case
async is actually for, and everything else here follows so a caller does not have
to mix two clients.

Two transports is two chances to drift, so the *decisions* live in
:mod:`caliber_sdk.transport` and are imported rather than restated: which methods
may be retried, which statuses are worth retrying, how backoff is computed, how
``Retry-After`` is read, how a body is decoded, when an envelope is unwrapped,
and what a CSRF failure looks like. What differs here is only the awaiting.
``test_async_parity.py`` asserts that separation holds.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from ..auth import AuthProvider, NoAuth
from ..errors import CaliberConfigError, CaliberTransportError, error_for_response
from ..transport import (
    _RETRYABLE_METHODS,
    _RETRYABLE_STATUS,
    API_PREFIX,
    USER_AGENT,
    Response,
    Transport,
    _decode,
    _unwrap,
)


class AsyncTransport:
    """Asynchronous HTTP transport against one CALIBER deployment."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthProvider | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_factor: float = 0.5,
        verify: bool | str = True,
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        cleaned = (base_url or "").strip().rstrip("/")
        if not cleaned:
            raise CaliberConfigError("base_url must not be empty")
        if not cleaned.startswith(("http://", "https://")):
            raise CaliberConfigError(f"base_url must be an http(s) URL, got {base_url!r}")
        if max_retries < 0:
            raise CaliberConfigError("max_retries must not be negative")

        self.base_url = cleaned
        self.auth = auth or NoAuth()
        self.project = project
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._user_agent = user_agent or USER_AGENT
        self._csrf_token: str | None = None
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout, verify=verify)

    async def aclose(self) -> None:
        """Close the underlying client, but only one we created.

        A caller who passed their own client owns its lifetime; closing it here
        would break the next thing that used it.
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncTransport:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- shared decisions, reused rather than restated ----------------------

    def url_for(self, path: str) -> str:
        cleaned = path if path.startswith("/") else f"/{path}"
        if not cleaned.startswith(API_PREFIX):
            cleaned = f"{API_PREFIX}{cleaned}"
        return f"{self.base_url}{cleaned}"

    def _headers(self, method: str, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
            "X-Request-Id": uuid.uuid4().hex,
        }
        headers.update(self.auth.headers())
        if self.project:
            headers["X-CALIBER-Project"] = self.project
        if method.upper() not in _RETRYABLE_METHODS and self._csrf_token:
            headers["X-CALIBER-CSRF"] = self._csrf_token
        if extra:
            headers.update(extra)
        return headers

    def _backoff(self, attempt: int) -> float:
        return self.backoff_factor * (2.0**attempt)

    # -- requests ----------------------------------------------------------

    async def bootstrap_csrf(self) -> str | None:
        try:
            response = await self.request("GET", "/csrf", _csrf_retry=False)
        except Exception:
            return None
        token = None
        if isinstance(response.data, dict):
            raw = response.data.get("csrf_token") or response.data.get("token")
            token = raw if isinstance(raw, str) and raw else None
        self._csrf_token = token
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        files: Any = None,
        data: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        _csrf_retry: bool = True,
    ) -> Response:
        """Perform one API call, returning the unwrapped payload."""
        verb = method.upper()
        url = self.url_for(path)
        attempts = self.max_retries + 1
        last_transport_error: Exception | None = None

        for attempt in range(attempts):
            request_headers = self._headers(verb, headers)
            options: dict[str, Any] = {}
            if timeout is not None:
                options["timeout"] = timeout
            try:
                raw = await self._client.request(
                    verb,
                    url,
                    params=dict(params) if params else None,
                    json=json if files is None else None,
                    files=files,
                    data=dict(data) if data else None,
                    headers=request_headers,
                    **options,
                )
            except httpx.HTTPError as exc:
                last_transport_error = exc
                if verb in _RETRYABLE_METHODS and attempt < attempts - 1:
                    # ``asyncio.sleep``, not ``time.sleep``: blocking the loop
                    # to wait out a retry would stall every other coroutine
                    # sharing it, which is the specific thing async is for.
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise CaliberTransportError(f"{verb} {url} failed: {exc}") from exc

            if (
                raw.status_code in _RETRYABLE_STATUS
                and verb in _RETRYABLE_METHODS
                and attempt < attempts - 1
            ):
                await asyncio.sleep(Transport._retry_after(raw) or self._backoff(attempt))
                continue

            payload = _decode(raw)
            request_id = raw.headers.get("X-Request-Id") or request_headers.get("X-Request-Id")

            if (
                raw.status_code == 403
                and _csrf_retry
                and verb not in _RETRYABLE_METHODS
                and Transport._looks_like_csrf_failure(payload)
                and await self.bootstrap_csrf()
            ):
                return await self.request(
                    verb,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                    _csrf_retry=False,
                )

            if raw.status_code >= 400:
                raise error_for_response(
                    status_code=raw.status_code,
                    payload=payload,
                    method=verb,
                    url=url,
                    request_id=request_id,
                )

            return Response(
                data=_unwrap(payload),
                status_code=raw.status_code,
                headers=raw.headers,
                request_id=request_id,
            )

        raise CaliberTransportError(
            f"{verb} {url} exhausted {attempts} attempts: {last_transport_error}"
        )

    async def get(self, path: str, **kwargs: Any) -> Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Response:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Response:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Response:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", path, **kwargs)

    async def download(self, path: str, **kwargs: Any) -> bytes:
        """Fetch raw bytes: no envelope, no decoding."""
        url = self.url_for(path)
        try:
            raw = await self._client.get(url, headers=self._headers("GET", None), **kwargs)
        except httpx.HTTPError as exc:
            raise CaliberTransportError(f"GET {url} failed: {exc}") from exc
        if raw.status_code >= 400:
            raise error_for_response(
                status_code=raw.status_code,
                payload=_decode(raw),
                method="GET",
                url=url,
                request_id=raw.headers.get("X-Request-Id"),
            )
        return raw.content

    async def stream_lines(
        self, path: str, *, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> AsyncIterator[str]:
        """Yield lines from a server-sent-events endpoint.

        The method this whole module exists for. No default timeout: a stream
        staying open is the success case, not a hang.
        """
        url = self.url_for(path)
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        try:
            async with self._client.stream(
                "GET",
                url,
                params=dict(params) if params else None,
                headers={**self._headers("GET", None), "Accept": "text/event-stream"},
                **options,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise error_for_response(
                        status_code=response.status_code,
                        payload=_decode(response),
                        method="GET",
                        url=url,
                        request_id=response.headers.get("X-Request-Id"),
                    )
                async for line in response.aiter_lines():
                    yield line
        except httpx.HTTPError as exc:
            raise CaliberTransportError(f"GET {url} stream failed: {exc}") from exc

    async def paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None, limit: int = 100
    ) -> AsyncIterator[Any]:
        """Yield items across ``limit``/``offset`` pages."""
        offset = 0
        while True:
            page = await self.get(path, params={**(params or {}), "limit": limit, "offset": offset})
            items = page.data
            if isinstance(items, dict):
                for key in ("items", "results", "data"):
                    if isinstance(items.get(key), list):
                        items = items[key]
                        break
            if not isinstance(items, list) or not items:
                return
            for item in items:
                yield item
            if len(items) < limit:
                return
            offset += len(items)


__all__ = ["AsyncTransport"]
