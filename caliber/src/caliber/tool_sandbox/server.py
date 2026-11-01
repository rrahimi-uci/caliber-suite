"""Standalone ASGI service for sandboxed tool execution."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.config import CaliberConfig
from caliber.effects.dispatcher import UnsupportedEffectPath
from caliber.effects.refusals import refuse_in_reference_profile
from caliber.tool_sandbox.models import ToolSandboxRunRequest, ToolSandboxTestSuiteRequest
from caliber.tool_sandbox.service import ToolSandbox, sandbox_from_optional_config

HEALTH_PATH = "/health"
RUN_PATH = "/v1/tool-sandbox/run"
TESTS_PATH = "/v1/tool-sandbox/tests"


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "caliber-tool-sandbox"})


def _refused_in_reference_profile(request: Request, row_id: str) -> JSONResponse | None:
    """The ``409`` answer for ``row_id`` when this service was started for the reference profile, else ``None``.

    The service has no authentication or scope check, and the code cannot establish that whatever fronts it authenticates
    callers, so the reference profile does not rely on a proxy: it answers nothing but the health check (B04.7, #774).
    :func:`create_app` already refuses to build the app there; this is the same refusal for an app whose state says so.
    """
    try:
        refuse_in_reference_profile(row_id, request.app.state.config)
    except UnsupportedEffectPath as exc:
        return JSONResponse({"detail": str(exc), "reason_code": exc.code}, status_code=409)
    return None


async def run_tool(request: Request) -> JSONResponse:
    if (refused := _refused_in_reference_profile(request, "EP-027")) is not None:
        return refused
    sandbox: ToolSandbox = request.app.state.sandbox
    body = await _parse_json_object(request)
    try:
        payload = ToolSandboxRunRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"detail": f"invalid sandbox run request: {exc}"}, status_code=400)
    # Sandbox backends expose a synchronous protocol. Running one directly in this
    # async handler monopolizes the ASGI event loop for the full authored timeout,
    # making even /health unresponsive while a tool executes.
    result = await run_in_threadpool(sandbox.run_tool, payload)
    return JSONResponse({"data": result.model_dump(mode="json")})


async def run_tests(request: Request) -> JSONResponse:
    if (refused := _refused_in_reference_profile(request, "EP-028")) is not None:
        return refused
    sandbox: ToolSandbox = request.app.state.sandbox
    body = await _parse_json_object(request)
    try:
        payload = ToolSandboxTestSuiteRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"detail": f"invalid sandbox test request: {exc}"}, status_code=400)
    result = await run_in_threadpool(sandbox.run_tests, payload)
    return JSONResponse({"data": result.model_dump(mode="json")})


def create_app(
    config: CaliberConfig | None = None,
    sandbox: ToolSandbox | None = None,
) -> Starlette:
    """Create the standalone sandbox ASGI app.

    Refused in the reference profile (``UnsupportedEffectPath``, B04.7, #774): the service is unauthenticated (see
    :func:`_refused_in_reference_profile`), so there is no app to build, however it was started.
    """
    resolved = config if config is not None else CaliberConfig.load()
    refuse_in_reference_profile("EP-029", resolved)
    app = Starlette(
        routes=[
            Route(HEALTH_PATH, health, methods=["GET"]),
            Route(RUN_PATH, run_tool, methods=["POST"]),
            Route(TESTS_PATH, run_tests, methods=["POST"]),
        ],
    )
    app.state.config = resolved
    app.state.sandbox = sandbox if sandbox is not None else sandbox_from_optional_config(resolved)
    return app


async def _parse_json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"__invalid_json__": str(exc)}
    if not isinstance(parsed, dict):
        return {"__invalid_json__": "request body must be a JSON object"}
    return parsed


__all__ = ["HEALTH_PATH", "RUN_PATH", "TESTS_PATH", "create_app"]
