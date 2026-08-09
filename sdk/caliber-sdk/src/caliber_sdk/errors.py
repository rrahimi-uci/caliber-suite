"""Exception hierarchy for the CALIBER SDK.

The server renders errors two ways (``caliber.routes._errors``): a plain
``{"detail", "status_code"}`` object, and a structured validation failure that
adds ``errors: [{loc, msg, type}, ...]``. Both are normalised here so callers
never parse a response body themselves.

Every exception carries the status, the server's detail, and the request
context. That last part is what makes a failure in someone else's CI log
actionable rather than "the SDK raised".
"""

from __future__ import annotations

from typing import Any


class CaliberError(Exception):
    """Base class for everything this SDK raises.

    A caller who wants "any SDK failure" catches this and nothing else.
    """


class CaliberConfigError(CaliberError):
    """The client was constructed with an unusable configuration."""


class CaliberTransportError(CaliberError):
    """The request never produced an HTTP response.

    Connection refused, DNS failure, timeout. Distinct from
    :class:`CaliberAPIError` because there is no server verdict to inspect --
    and because retrying is often correct here and often wrong there.
    """


class CaliberAPIError(CaliberError):
    """The server returned a non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: str | None = None,
        method: str | None = None,
        url: str | None = None,
        request_id: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url
        self.request_id = request_id
        #: The decoded body, when there was one. Kept so a caller can read
        #: fields this SDK does not model rather than being blocked by it.
        self.payload = payload

    def __str__(self) -> str:
        location = f"{self.method} {self.url}" if self.method and self.url else ""
        parts = [f"[{self.status_code}]", super().__str__()]
        if location:
            parts.append(f"({location})")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(part for part in parts if part)


class CaliberAuthenticationError(CaliberAPIError):
    """401 — no usable identity. The credential is missing, wrong, or revoked."""


class CaliberPermissionError(CaliberAPIError):
    """403 — authenticated, but the identity lacks the required scope."""


class CaliberNotFoundError(CaliberAPIError):
    """404 — no such resource.

    CALIBER also returns this for a resource that exists but belongs to another
    user, deliberately: distinguishing the two would let a caller enumerate ids.
    """


class CaliberConflictError(CaliberAPIError):
    """409 — the request conflicts with current state (duplicate name, etc.)."""


class CaliberValidationError(CaliberAPIError):
    """400 with a structured ``errors`` list."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        #: ``[{"loc": [...], "msg": "...", "type": "..."}]`` as the server sent it.
        self.errors = errors

    def __str__(self) -> str:
        if not self.errors:
            return super().__str__()
        fields = ", ".join(
            ".".join(str(part) for part in item.get("loc", [])) or "<body>"
            for item in self.errors
        )
        return f"{super().__str__()} — invalid: {fields}"


class CaliberRateLimitError(CaliberAPIError):
    """429 — too many requests."""


class CaliberServerError(CaliberAPIError):
    """5xx — the server failed. Usually worth retrying; never worth assuming."""


#: Status code -> exception class. Anything unmapped falls back by class of
#: status, so a new 4xx does not arrive as a bare CaliberAPIError with no
#: indication of whether retrying could help.
_BY_STATUS: dict[int, type[CaliberAPIError]] = {
    400: CaliberValidationError,
    401: CaliberAuthenticationError,
    403: CaliberPermissionError,
    404: CaliberNotFoundError,
    409: CaliberConflictError,
    429: CaliberRateLimitError,
}


def error_for_response(
    *,
    status_code: int,
    payload: Any,
    method: str,
    url: str,
    request_id: str | None = None,
) -> CaliberAPIError:
    """Build the right exception for a non-2xx response.

    Tolerates a body that is not the documented shape -- an HTML error page from
    a proxy in front of CALIBER is a realistic response, and it must produce a
    usable exception rather than a KeyError inside the SDK.
    """
    detail: str | None = None
    structured: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_detail = payload.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) else None
        raw_errors = payload.get("errors")
        if isinstance(raw_errors, list):
            structured = [item for item in raw_errors if isinstance(item, dict)]

    message = detail or f"request failed with status {status_code}"
    kwargs: dict[str, Any] = {
        "status_code": status_code,
        "detail": detail,
        "method": method,
        "url": url,
        "request_id": request_id,
        "payload": payload,
    }

    if status_code == 400 and structured:
        return CaliberValidationError(message, errors=structured, **kwargs)
    mapped = _BY_STATUS.get(status_code)
    if mapped is CaliberValidationError:
        return CaliberValidationError(message, errors=structured, **kwargs)
    if mapped is not None:
        return mapped(message, **kwargs)
    if status_code >= 500:
        return CaliberServerError(message, **kwargs)
    return CaliberAPIError(message, **kwargs)


__all__ = [
    "CaliberAPIError",
    "CaliberAuthenticationError",
    "CaliberConfigError",
    "CaliberConflictError",
    "CaliberError",
    "CaliberNotFoundError",
    "CaliberPermissionError",
    "CaliberRateLimitError",
    "CaliberServerError",
    "CaliberTransportError",
    "CaliberValidationError",
    "error_for_response",
]
