"""HTTP error handling.

Starlette's default :class:`HTTPException` renders ``text/plain``. The CALIBER
API contract is JSON everywhere — including
errors — so we register a handler that returns ``{"detail": ..., "status_code": ...}``
and lock the shape with tests so a refactor cannot silently regress it.
"""

from __future__ import annotations

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse


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
    return JSONResponse(
        {"detail": exc.detail, "status_code": exc.status_code},
        status_code=exc.status_code,
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
