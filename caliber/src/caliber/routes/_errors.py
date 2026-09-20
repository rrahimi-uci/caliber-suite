"""HTTP error handling.

Starlette's default :class:`HTTPException` renders ``text/plain``. The CALIBER
API contract is JSON everywhere — including
errors — so we register a handler that returns ``{"detail": ..., "status_code": ...}``
and lock the shape with tests so a refactor cannot silently regress it.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse


class CaliberHTTPException(HTTPException):
    """An :class:`HTTPException` that also carries a machine-readable ``reason_code``.

    ``detail`` stays the human-readable string every existing caller already
    parses; ``reason_code`` is a new, optional, additive transport field
    (Phase 0 item 6, ``docs/workspace-plan.md`` section 13.6) that lets an SDK
    caller distinguish failure reasons programmatically instead of
    string-matching ``detail``. A route that has not migrated yet keeps
    raising a bare :class:`HTTPException` -- ``http_exception_handler`` below
    renders that exactly as before, with no ``reason_code`` key at all, so
    this is backward compatible by construction rather than by convention.

    This is deliberately not enforced across every route in one pass: most
    call sites still raise a bare ``HTTPException`` with only a ``detail``
    string. Migrating the rest is a named, separate follow-up (see
    ``docs/workspace-plan.md`` Phase 0 item 6), not something this class does
    for free.
    """

    def __init__(
        self,
        status_code: int,
        detail: str | None = None,
        *,
        reason_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        #: A stable, machine-readable string a caller can switch on without
        #: parsing ``detail`` (e.g. ``"resource_type_adapter_unavailable"``).
        #: ``None`` when the raising route has not been migrated yet.
        self.reason_code = reason_code


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`HTTPException` as a JSON envelope.

    The signature uses ``Exception`` rather than ``HTTPException`` to match the
    Starlette ``exception_handlers`` contract (the dict values are typed
    against the base class). At runtime, Starlette only routes this handler
    HTTPException instances per the mapping key, so the narrowing is safe.
    """
    if not isinstance(exc, HTTPException):
        # Defensive: should never trigger because the mapping keys on HTTPException.
        raise exc
    body: dict[str, Any] = {"detail": exc.detail, "status_code": exc.status_code}
    # Only ``CaliberHTTPException`` carries this attribute, and only some of
    # those set it -- a bare ``HTTPException`` (still the overwhelming
    # majority of call sites) renders identically to before this field
    # existed, which is what makes this additive rather than breaking.
    reason_code = getattr(exc, "reason_code", None)
    if reason_code is not None:
        body["reason_code"] = reason_code
    return JSONResponse(
        body,
        status_code=exc.status_code,
        # ``HTTPException.headers`` was being dropped, which silently discarded
        # protocol-significant headers: a 429 lost its ``Retry-After``, so a client
        # had no way to know how long to back off and would simply retry immediately.
        # Starlette's own handler forwards these; ours must too.
        headers=exc.headers or None,
    )


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render a Pydantic :class:`ValidationError` as a structured 400.

    Body shape::

        {
          "detail": "request body validation failed",
          "status_code": 400,
          "errors": [
            {"loc": ["category"], "msg": "field required", "type": "missing"},
            ...
          ]
        }

    Same signature reasoning as :func:`http_exception_handler`: Starlette types
    handler values against the base ``Exception``.
    """
    if not isinstance(exc, ValidationError):
        raise exc
    return JSONResponse(
        {
            "detail": "request body validation failed",
            "status_code": 400,
            "errors": [
                {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                for err in exc.errors()
            ],
        },
        status_code=400,
    )
