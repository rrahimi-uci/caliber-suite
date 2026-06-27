"""Standalone ASGI service for sandboxed tool execution."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.config import CaliberConfig
from caliber.tool_sandbox.models import ToolSandboxRunRequest, ToolSandboxTestSuiteRequest
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox, ToolSandbox

HEALTH_PATH = "/health"
RUN_PATH = "/v1/tool-sandbox/run"
TESTS_PATH = "/v1/tool-sandbox/tests"


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "caliber-tool-sandbox"})


async def run_tool(request: Request) -> JSONResponse:
    sandbox: ToolSandbox = request.app.state.sandbox
    body = await _parse_json_object(request)
    try:
        payload = ToolSandboxRunRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"detail": f"invalid sandbox run request: {exc}"}, status_code=400)
    result = sandbox.run_tool(payload)
    return JSONResponse({"data": result.model_dump(mode="json")})


async def run_tests(request: Request) -> JSONResponse:
    sandbox: ToolSandbox = request.app.state.sandbox
    body = await _parse_json_object(request)
    try:
        payload = ToolSandboxTestSuiteRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"detail": f"invalid sandbox test request: {exc}"}, status_code=400)
    result = sandbox.run_tests(payload)
    return JSONResponse({"data": result.model_dump(mode="json")})


def create_app(
    config: CaliberConfig | None = None,
    sandbox: ToolSandbox | None = None,
) -> Starlette:
    """Create the standalone sandbox ASGI app."""
    resolved = config if config is not None else CaliberConfig.load()
    app = Starlette(
        routes=[
            Route(HEALTH_PATH, health, methods=["GET"]),
            Route(RUN_PATH, run_tool, methods=["POST"]),
            Route(TESTS_PATH, run_tests, methods=["POST"]),
        ],
    )
    app.state.config = resolved
    app.state.sandbox = sandbox or LocalSubprocessToolSandbox.from_config(resolved)
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
