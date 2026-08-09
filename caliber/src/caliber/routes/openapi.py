"""Served OpenAPI document for the CALIBER management API.

Workflow services already publish an OpenAPI document per workflow
(:mod:`caliber.routes.services`); the management API under
``/ajax-api/2.0/mlflow/caliber`` did not publish one at all. Without it a typed
SDK has to be written by hand against routes nobody promised to keep, and it
drifts silently the first time a path changes.

**The document is derived from the live route table, never hand-maintained.**
``build_openapi_document`` walks ``app.routes`` at call time, so a route added,
renamed, or removed is reflected without anyone remembering to update a
specification. That is the whole point: a hand-written contract that disagrees
with the server is worse than no contract, because clients trust it.

Scope, stated honestly. This is a *route-level* document: paths, methods, path
parameters, the global envelope and error shapes, and the authentication
schemes. It does not yet describe per-operation request and response bodies,
because most handlers do not declare them in a machine-readable way — that is
the work in M0-PR2, and ``x-caliber-schema-coverage`` records the gap in the
document itself rather than leaving readers to infer it.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber import __version__
from caliber.auth import require_user

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPENAPI_PATH = PREFIX + "/openapi.json"

#: Starlette path converters (``{path:path}``, ``{amount:g}``) are not OpenAPI
#: syntax. OpenAPI wants the bare name and a typed parameter declaration.
_CONVERTER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*):[a-zA-Z]+\}")
_PATH_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: Methods Starlette adds for free and that carry no contract of their own.
_IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})

#: Converter name -> OpenAPI schema for the path parameter it produces.
_CONVERTER_SCHEMA: dict[str, dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "g": {"type": "number"},
    "path": {"type": "string"},
    "uuid": {"type": "string", "format": "uuid"},
}


def _normalize_path(path: str) -> str:
    """``/x/{id:path}`` -> ``/x/{id}``."""
    return _CONVERTER.sub(r"{\1}", path)


def _converters_in(path: str) -> dict[str, str]:
    """Map each path parameter to its declared converter (default ``str``)."""
    converters = dict(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*):([a-zA-Z]+)\}", path))
    for name in _PATH_PARAM.findall(_normalize_path(path)):
        converters.setdefault(name, "str")
    return converters


def _tag_for(path: str) -> str:
    """Group operations by the first segment after the prefix.

    ``/ajax-api/2.0/mlflow/caliber/workflow-runs/{id}`` -> ``workflow-runs``.
    A tag is what makes a generated client's module layout obvious, so it is
    derived from the URL rather than from the Python module, which callers of
    the HTTP API cannot see.
    """
    remainder = path[len(PREFIX) :].lstrip("/")
    if not remainder:
        return "root"
    first = remainder.split("/", 1)[0]
    return _normalize_path(first).strip("{}") or "root"


def _operation_id(method: str, path: str) -> str:
    """A stable, unique identifier generators use for method names."""
    remainder = _normalize_path(path)[len(PREFIX) :].strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", remainder).strip("_") or "root"
    return f"{method.lower()}_{slug}"


def _components() -> dict[str, Any]:
    return {
        "responses": _SHARED_RESPONSES,
        "securitySchemes": {
            # An SDK authenticates with the session token in Authorization;
            # the browser uses the HttpOnly cookie. Both resolve to the same
            # identity in caliber.auth.
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "caliber_session"},
            "csrfToken": {
                "type": "apiKey",
                "in": "header",
                "name": "X-CALIBER-CSRF",
                "description": (
                    "Required on mutating requests when CSRF enforcement is enabled. "
                    "Obtain from GET /csrf."
                ),
            },
            "projectScope": {
                "type": "apiKey",
                "in": "header",
                "name": "X-CALIBER-Project",
                "description": "Selects the active project/workspace for the request.",
            },
        },
        "schemas": {
            "Envelope": {
                "type": "object",
                "description": "Standard success wrapper for JSON responses.",
                "properties": {"data": {}},
                "required": ["data"],
            },
            "Error": {
                "type": "object",
                "description": "Rendered from HTTPException by routes/_errors.py.",
                "properties": {
                    "detail": {"type": "string"},
                    "status_code": {"type": "integer"},
                },
                "required": ["detail", "status_code"],
            },
            "ValidationError": {
                "type": "object",
                "description": "Structured 400 emitted for request-body validation failures.",
                "properties": {
                    "detail": {"type": "string"},
                    "status_code": {"type": "integer"},
                    "errors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "loc": {"type": "array", "items": {}},
                                "msg": {"type": "string"},
                                "type": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["detail", "status_code"],
            },
        },
    }


#: Every operation shares these. Declared once under ``components.responses``
#: and referenced, rather than inlined per operation: inlining the same five
#: blocks across 355 operations produced a 348 KB document, most of it the same
#: bytes repeated. Reuse is also what the specification's components section is
#: for, so generators emit one shared error type instead of 355 identical ones.
_SHARED_RESPONSES: dict[str, Any] = {
    "Success": {
        "description": "Success. JSON payloads are wrapped in the standard envelope.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}},
    },
    "ValidationFailed": {
        "description": "Validation or request error.",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ValidationError"}}
        },
    },
    "Unauthenticated": {
        "description": "Not authenticated.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    },
    "Forbidden": {
        "description": "Authenticated but not authorized.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    },
    "NotFound": {
        "description": "Not found.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    },
}

_RESPONSE_REFS: dict[str, Any] = {
    "200": {"$ref": "#/components/responses/Success"},
    "400": {"$ref": "#/components/responses/ValidationFailed"},
    "401": {"$ref": "#/components/responses/Unauthenticated"},
    "403": {"$ref": "#/components/responses/Forbidden"},
    "404": {"$ref": "#/components/responses/NotFound"},
}


def _responses() -> dict[str, Any]:
    return dict(_RESPONSE_REFS)


def build_openapi_document(app: Starlette) -> dict[str, Any]:
    """Describe every registered management route as an OpenAPI 3.0.3 document."""

    paths: dict[str, dict[str, Any]] = {}
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        raw_path = route.path
        if not raw_path.startswith(PREFIX):
            continue

        normalized = _normalize_path(raw_path)
        converters = _converters_in(raw_path)
        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": _CONVERTER_SCHEMA.get(kind, {"type": "string"}),
            }
            for name, kind in sorted(converters.items())
        ]

        methods = sorted((route.methods or set()) - _IMPLICIT_METHODS)
        for method in methods:
            operation: dict[str, Any] = {
                "operationId": _operation_id(method, raw_path),
                "tags": [_tag_for(raw_path)],
                "responses": _responses(),
            }
            if parameters:
                operation["parameters"] = parameters
            if method not in {"GET"}:
                # Bodies are not yet modelled (M0-PR2). Say so in the document
                # rather than implying an empty request is correct.
                operation["x-caliber-request-body"] = "not-yet-modelled"
            paths.setdefault(normalized, {})[method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "CALIBER Management API",
            "version": __version__,
            "description": (
                "Management API for CALIBER. This document is generated from the live "
                "route table, so it always matches the running server's paths. Request "
                "and response bodies are not yet modelled per operation; see "
                "x-caliber-schema-coverage."
            ),
        },
        "servers": [{"url": PREFIX, "description": "CALIBER management API root"}],
        "x-caliber-schema-coverage": {
            "paths": "complete",
            "operations": "complete",
            "path_parameters": "complete",
            "request_bodies": "not-yet-modelled",
            "response_bodies": "envelope-only",
        },
        "security": [{"bearerAuth": []}, {"sessionCookie": []}],
        "components": _components(),
        "paths": paths,
    }


async def get_openapi(request: Request) -> JSONResponse:
    """Serve the management OpenAPI document.

    Authenticated, matching ``/capabilities``: the document enumerates the whole
    management surface, which is reconnaissance for an unauthenticated caller
    and is not needed before login by any supported client.
    """
    require_user(request)
    # Built per request rather than cached: the route table is fixed after
    # startup, the walk is cheap, and a cache would be one more thing that can
    # disagree with the server it describes.
    return JSONResponse(build_openapi_document(request.app))


def register(app: Starlette) -> None:
    app.routes.append(Route(OPENAPI_PATH, get_openapi, methods=["GET"]))


__all__ = ["OPENAPI_PATH", "build_openapi_document", "register"]
