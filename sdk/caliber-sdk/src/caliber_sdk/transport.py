"""HTTP transport: envelopes, errors, retries, CSRF, and correlation.

Everything in this module exists because the SPA's client
(``caliber-ui/src/api/caliberApi.ts``) already had to solve it, and a developer
integrating from Python should not have to solve it again:

* responses are wrapped as ``{"data": ...}`` and must be unwrapped;
* non-2xx bodies must become typed exceptions, not raw dicts;
* GETs need a default timeout, or a hung server becomes a hung script;
* writes may need a CSRF token;
* the active project is selected by a header.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from typing import Any, Final

import httpx

from .auth import AuthProvider, NoAuth
from .errors import CaliberConfigError, CaliberTransportError, error_for_response

#: Appended to the caller's User-Agent so CALIBER operators can tell SDK
#: traffic from browser traffic in their logs.
USER_AGENT = "caliber-sdk-python"


class UnsetProjectType:
    """Sentinel distinguishing "use the ambient project" from an explicit
    ``project=None`` (deliberately unscoped). A bare default of ``None``
    could not tell those apart."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET_PROJECT"


#: Default for ``request()``'s ``project`` parameter -- resolves to
#: ``Transport.project`` (the ambient/constructor scope). Pass an explicit
#: project id string to pin a request to it regardless of the ambient scope
#: (§13.4: a request naming ``/projects/{project_id}/...`` in its path must
#: send that same id, not whatever the client happens to be scoped to), or
#: ``None`` to deliberately send no project header at all (a platform call).
UNSET_PROJECT: Final = UnsetProjectType()


class _ProjectContext:
    """Context-local project selection shared by both SDK transports.

    The constructor value is the fallback for every execution context. A
    scoped override is stored in the current thread/task context only, so a
    reusable client can be shared safely by concurrent callers without one
    request changing another caller's project header.
    """

    def __init__(self, default: str | None, *, name: str) -> None:
        self._value: ContextVar[str | None] = ContextVar(name, default=default)

    @property
    def current(self) -> str | None:
        return self._value.get()

    def set(self, project_id: str | None) -> Token[str | None]:
        return self._value.set(project_id)

    def reset(self, token: Token[str | None]) -> None:
        self._value.reset(token)


#: The management API root. Same-origin with MLflow by design.
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"

#: Only methods without side effects are retried automatically. Retrying a POST
#: that already reached the server would duplicate the effect it performed --
#: the SDK cannot know whether the failure happened before or after.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class Response:
    """A decoded response plus the context needed to debug it."""

    __slots__ = ("data", "headers", "request_id", "status_code")

    def __init__(
        self, *, data: Any, status_code: int, headers: Mapping[str, str], request_id: str | None
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers
        self.request_id = request_id


class Transport:
    """Synchronous HTTP transport against one CALIBER deployment.

    The ambient project is context-local. Per-request ``project=`` pins remain
    the preferred choice for resource methods whose path names the project.
    """

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
        client: httpx.Client | None = None,
        user_agent: str | None = None,
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
        self._project_context = _ProjectContext(project, name=f"caliber_sdk_project_{id(self)}")
        self.max_retries: int = max_retries
        self.backoff_factor: float = backoff_factor
        self._csrf_token: str | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout, verify=verify)
        self._user_agent = user_agent or USER_AGENT

    @property
    def project(self) -> str | None:
        """The ambient project in the current thread/task context."""
        return self._project_context.current

    @project.setter
    def project(self, value: str | None) -> None:
        self._project_context.set(value)

    def _push_project(self, project_id: str) -> Token[str | None]:
        return self._project_context.set(project_id)

    def _pop_project(self, token: Token[str | None]) -> None:
        self._project_context.reset(token)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        # Only close what we created. A caller who passed their own client is
        # sharing a connection pool on purpose, and closing it under them would
        # break unrelated requests.
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- request path ------------------------------------------------------

    def url_for(self, path: str) -> str:
        """Absolute URL for an API path, with or without the prefix."""
        cleaned = path if path.startswith("/") else f"/{path}"
        if not cleaned.startswith(API_PREFIX):
            cleaned = f"{API_PREFIX}{cleaned}"
        return f"{self.base_url}{cleaned}"

    def _headers(
        self,
        method: str,
        extra: Mapping[str, str] | None,
        project: str | UnsetProjectType | None = UNSET_PROJECT,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
            # A correlation id the caller can quote when reporting a failure,
            # and that appears on the exception when one is raised.
            "X-Request-Id": uuid.uuid4().hex,
        }
        headers.update(self.auth.headers())
        resolved_project = self.project if isinstance(project, UnsetProjectType) else project
        if resolved_project:
            headers["X-CALIBER-Project"] = resolved_project
        if method.upper() not in _RETRYABLE_METHODS and self._csrf_token:
            headers["X-CALIBER-CSRF"] = self._csrf_token
        if extra:
            headers.update(extra)
        return headers

    def bootstrap_csrf(self) -> str | None:
        """Fetch and cache a CSRF token, returning it.

        Idempotent and cheap to call. Returns ``None`` when the deployment does
        not issue one, which is not an error: CSRF enforcement is configurable
        and a Bearer client may not need it.
        """
        try:
            response = self.request("GET", "/csrf", _csrf_retry=False)
        except Exception:
            # A deployment with CSRF disabled may not serve this at all. Failing
            # the whole client for a token nothing will ask for would be wrong.
            return None
        token = None
        if isinstance(response.data, dict):
            raw = response.data.get("csrf_token") or response.data.get("token")
            token = raw if isinstance(raw, str) and raw else None
        self._csrf_token = token
        return token

    def request(
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
        project: str | UnsetProjectType | None = UNSET_PROJECT,
        _csrf_retry: bool = True,
    ) -> Response:
        """Perform one API call, returning the unwrapped payload.

        ``project``, when passed, pins the request's ``X-CALIBER-Project``
        header regardless of the ambient (constructor/``project_scope``)
        scope -- resource modules that build a ``/projects/{project_id}/...``
        path use this to send that same id, never whatever the client
        happens to be scoped to. Left at its default (``UNSET_PROJECT``),
        the ambient scope applies unchanged; ``None`` deliberately omits the
        header even if an ambient scope is set.
        """
        verb = method.upper()
        url = self.url_for(path)
        if not isinstance(project, UnsetProjectType) and headers and "X-CALIBER-Project" in headers:
            manual = headers["X-CALIBER-Project"]
            if manual != project:
                raise CaliberConfigError(
                    f"conflicting project scope: headers['X-CALIBER-Project']={manual!r} "
                    f"but project={project!r} was also passed to the same call"
                )
        attempts = self.max_retries + 1
        last_transport_error: Exception | None = None

        for attempt in range(attempts):
            request_headers = self._headers(verb, headers, project)
            # ``timeout`` is omitted entirely unless the caller set one, rather
            # than passed as USE_CLIENT_DEFAULT. Same behaviour, and it keeps
            # the SDK drivable by any httpx.Client subclass that restricts
            # kwargs -- Starlette's TestClient rejects ``timeout`` outright,
            # which is exactly what someone testing their own integration
            # against an in-process app would reach for.
            options: dict[str, Any] = {}
            if timeout is not None:
                options["timeout"] = timeout
            try:
                raw = self._client.request(
                    verb,
                    url,
                    params=dict(params) if params else None,
                    # ``json`` and ``files`` are mutually exclusive at the HTTP
                    # level: a multipart body cannot also be a JSON document.
                    json=json if files is None else None,
                    files=files,
                    data=dict(data) if data else None,
                    headers=request_headers,
                    **options,
                )
            except httpx.HTTPError as exc:
                last_transport_error = exc
                if verb in _RETRYABLE_METHODS and attempt < attempts - 1:
                    time.sleep(self._backoff(attempt))
                    continue
                raise CaliberTransportError(f"{verb} {url} failed: {exc}") from exc

            if (
                raw.status_code in _RETRYABLE_STATUS
                and verb in _RETRYABLE_METHODS
                and attempt < attempts - 1
            ):
                time.sleep(self._retry_after(raw) or self._backoff(attempt))
                continue

            payload = _decode(raw)
            request_id = raw.headers.get("X-Request-Id") or request_headers.get("X-Request-Id")

            # A write refused for a missing CSRF token is recoverable and
            # invisible to the caller: fetch one and replay exactly once. Bounded
            # by the flag so a genuine permission failure cannot loop.
            if (
                raw.status_code == 403
                and _csrf_retry
                and verb not in _RETRYABLE_METHODS
                and self._looks_like_csrf_failure(payload)
                and self.bootstrap_csrf()
            ):
                return self.request(
                    verb,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                    project=project,
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

        # Unreachable: the loop either returns or raises. Kept explicit so a
        # future edit that changes the control flow fails loudly.
        raise CaliberTransportError(
            f"{verb} {url} exhausted {attempts} attempts: {last_transport_error}"
        )

    def _backoff(self, attempt: int) -> float:
        # 2.0 rather than 2: int ** int types as Any under strict mypy, because
        # a negative exponent would make it a float.
        return self.backoff_factor * (2.0**attempt)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds: float = float(raw)
        except ValueError:
            # Retry-After may be an HTTP date. Falling back to our own backoff
            # is better than parsing dates for a value we only use as a hint.
            return None
        return max(0.0, seconds)

    @staticmethod
    def _looks_like_csrf_failure(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        detail = payload.get("detail")
        return isinstance(detail, str) and "csrf" in detail.lower()

    # -- convenience -------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)

    def download(
        self, path: str, *, project: str | UnsetProjectType | None = UNSET_PROJECT, **kwargs: Any
    ) -> bytes:
        """Fetch raw bytes.

        Separate from :meth:`request` because file content is not JSON: it has
        no envelope to unwrap and decoding it would corrupt binary data.

        ``project`` pins the request the same way it does for :meth:`request`.
        """
        url = self.url_for(path)
        try:
            raw = self._client.get(url, headers=self._headers("GET", None, project), **kwargs)
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

    def stream_lines(
        self, path: str, *, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        """Yield lines from a server-sent-events endpoint.

        Streaming needs its own path: the ordinary request reads the whole body
        before returning, which for an endpoint that never ends means blocking
        forever. No default timeout is applied either — a stream staying open is
        the success case, not a hang.
        """
        url = self.url_for(path)
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        try:
            with self._client.stream(
                "GET",
                url,
                params=dict(params) if params else None,
                headers={**self._headers("GET", None), "Accept": "text/event-stream"},
                **options,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise error_for_response(
                        status_code=response.status_code,
                        payload=_decode(response),
                        method="GET",
                        url=url,
                        request_id=response.headers.get("X-Request-Id"),
                    )
                yield from response.iter_lines()
        except httpx.HTTPError as exc:
            raise CaliberTransportError(f"GET {url} stream failed: {exc}") from exc

    def paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None, limit: int = 100
    ) -> Iterator[Any]:
        """Yield items across ``limit``/``offset`` pages.

        CALIBER's list endpoints are offset-based today. Exposing an iterator
        rather than the raw pages means the eventual move to cursors does not
        change this signature.
        """
        offset = 0
        while True:
            page = self.get(path, params={**(params or {}), "limit": limit, "offset": offset})
            items = page.data
            if isinstance(items, dict):
                for key in ("items", "results", "data"):
                    if isinstance(items.get(key), list):
                        items = items[key]
                        break
            if not isinstance(items, list) or not items:
                return
            yield from items
            if len(items) < limit:
                return
            offset += len(items)


def _decode(response: httpx.Response) -> Any:
    """Decode a JSON body, tolerating one that is not JSON at all."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        # A proxy's HTML error page, for instance. The text is more useful to a
        # human than a decode traceback from inside the SDK.
        return {"detail": response.text.strip()[:500] or None}


def _unwrap(payload: Any) -> Any:
    """Strip CALIBER's ``{"data": ...}`` envelope when present.

    Checked by shape rather than assumed: a handful of endpoints (the OpenAPI
    document among them) deliberately return an unenveloped body, and
    unwrapping those would hand back ``None``.
    """
    if isinstance(payload, dict) and set(payload) == {"data"}:
        return payload["data"]
    return payload


__all__ = [
    "API_PREFIX",
    "UNSET_PROJECT",
    "USER_AGENT",
    "Response",
    "Transport",
    "UnsetProjectType",
]
