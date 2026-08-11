"""Served OpenAPI document for the CALIBER management API.

Workflow services already publish an OpenAPI document per workflow
(:mod:`caliber.routes.services`); the management API under
``/ajax-api/2.0/mlflow/caliber`` used to publish only a route map. The current
document still derives from the *live* route table, but it also recovers
request and success-response bodies from the handlers themselves so the served
contract is complete enough to drive SDKs, generated clients, and raw HTTP
integrations without hand-maintained drift.
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
from caliber.routes.openapi_inference import infer_operation_contract

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


#: Stability tier per OpenAPI tag.
#:
#: Tags are derived from the URL, not from the Python module, because that is
#: what a caller of the HTTP API can see. The two do not correspond one to one:
#: 41 route modules produce 50 tags (``knowledge_bases`` alone yields
#: ``knowledge-bases``, ``knowledge-base-versions``, ``knowledge-runs``, and
#: ``knowledge``), so the mapping is written out rather than derived.
#:
#: ``test_every_tag_has_a_declared_stability_tier`` fails when a new tag appears
#: without a tier, which is the only way this stays correct as routes are added.
STABILITY_GA = "ga"
STABILITY_BETA = "beta"
STABILITY_INTERNAL = "internal"

_STABILITY: dict[str, str] = {
    # --- GA: the core management and automation surface -------------------
    "openapi.json": STABILITY_GA,
    "auth": STABILITY_GA,
    "csrf": STABILITY_GA,
    "me": STABILITY_GA,
    "capabilities": STABILITY_GA,
    "settings": STABILITY_GA,
    "prompts": STABILITY_GA,
    "skills": STABILITY_GA,
    "tools": STABILITY_GA,
    "agents": STABILITY_GA,
    "workflows": STABILITY_GA,
    "workflow-versions": STABILITY_GA,
    "workflow-runs": STABILITY_GA,
    "workflow-promotions": STABILITY_GA,
    "workflow-templates": STABILITY_GA,
    "workflow-components": STABILITY_GA,
    "workflow-cron-preview": STABILITY_GA,
    "workflow-files": STABILITY_GA,
    "services": STABILITY_GA,
    "projects": STABILITY_GA,
    "eval-datasets": STABILITY_GA,
    "evaluations": STABILITY_GA,
    "judges": STABILITY_GA,
    # --- Beta: real, supported, still moving ------------------------------
    "mcp-servers": STABILITY_BETA,
    "openapi-integrations": STABILITY_BETA,
    "gateway": STABILITY_BETA,
    "knowledge-bases": STABILITY_BETA,
    "knowledge-base-versions": STABILITY_BETA,
    "knowledge-runs": STABILITY_BETA,
    "knowledge": STABILITY_BETA,
    "object-store": STABILITY_BETA,
    "jobs": STABILITY_BETA,
    "review-queues": STABILITY_BETA,
    "aria": STABILITY_BETA,
    "releases": STABILITY_BETA,
    "observability": STABILITY_BETA,
    "audit-log": STABILITY_BETA,
    "events": STABILITY_BETA,
    "cookbooks": STABILITY_BETA,
    "secrets": STABILITY_BETA,
    "workflow-benchmark-reports": STABILITY_BETA,
    "playground-runs": STABILITY_BETA,
    # --- Internal: not part of the public SDK contract --------------------
    "assistant": STABILITY_INTERNAL,
    "memory": STABILITY_INTERNAL,
    "dashboard": STABILITY_INTERNAL,
    "metrics": STABILITY_INTERNAL,
    "health": STABILITY_INTERNAL,
    "readiness": STABILITY_INTERNAL,
    "gate-verdicts": STABILITY_INTERNAL,
    "llm-pricing": STABILITY_INTERNAL,
    "system": STABILITY_INTERNAL,
}

#: A tag with no declared tier is treated as internal rather than assumed GA.
#: Failing closed matters here: the tier is a promise about compatibility, and
#: silently promising one for a route nobody classified is the expensive
#: mistake. The test suite additionally rejects the omission outright.
STABILITY_DEFAULT = STABILITY_INTERNAL


def stability_for(tag: str) -> str:
    return _STABILITY.get(tag, STABILITY_DEFAULT)


def stability_summary() -> dict[str, list[str]]:
    """Tags grouped by tier, for ``/capabilities`` to serve to SDK clients."""
    summary: dict[str, list[str]] = {
        STABILITY_GA: [],
        STABILITY_BETA: [],
        STABILITY_INTERNAL: [],
    }
    for tag, tier in _STABILITY.items():
        summary[tier].append(tag)
    return {tier: sorted(tags) for tier, tags in summary.items()}


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


#: Shared error responses. Success responses are per-operation because their body
#: shape and status code now differ materially across the surface.
_SHARED_RESPONSES: dict[str, Any] = {
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


def build_openapi_document(app: Starlette) -> dict[str, Any]:
    """Describe every registered management route as an OpenAPI 3.0.3 document."""

    components = _components()
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

        tag = _tag_for(raw_path)
        methods = sorted((route.methods or set()) - _IMPLICIT_METHODS)
        for method in methods:
            contract = infer_operation_contract(
                route.endpoint,
                method=method,
                components_schemas=components["schemas"],
            )
            operation: dict[str, Any] = {
                "operationId": _operation_id(method, raw_path),
                "tags": [tag],
                # Per-operation so a generator can gate on it directly, rather
                # than joining against a tag table it may not read.
                "x-caliber-stability": stability_for(tag),
                "responses": contract["responses"],
            }
            if parameters:
                operation["parameters"] = parameters
            request_body = contract.get("requestBody")
            if request_body is not None:
                operation["requestBody"] = request_body
            paths.setdefault(normalized, {})[method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "CALIBER Management API",
            "version": __version__,
            "description": (
                "Management API for CALIBER. This document is generated from the live "
                "route table, so it always matches the running server's paths. Request "
                "and success-response bodies are inferred from the handlers themselves: "
                "typed where the route already declares a model, permissive where the "
                "route remains intentionally dynamic."
            ),
        },
        "servers": [{"url": PREFIX, "description": "CALIBER management API root"}],
        "x-caliber-schema-coverage": {
            "paths": "complete",
            "operations": "complete",
            "path_parameters": "complete",
            "request_bodies": "complete",
            "response_bodies": "complete",
        },
        "security": [{"bearerAuth": []}, {"sessionCookie": []}],
        "components": components,
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
