"""Guarded declarative HTTP execution for OpenAPI-derived tools."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import jsonschema

from caliber.egress import EgressBlockedError, build_client
from caliber.secrets import resolve_secret

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")


class OpenApiExecutionError(RuntimeError):
    """Raised when a declarative OpenAPI-backed tool cannot execute safely."""


def bind_openapi_http_tool(binding: Any) -> Callable[..., Any]:
    """Bind an OpenAPI-backed tool to an executable callable."""

    def _invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and kwargs:
            raise OpenApiExecutionError(
                f"openapi tool {getattr(binding, 'local_name', 'tool')!r} got both args and kwargs"
            )
        if args:
            if len(args) != 1 or not isinstance(args[0], dict):
                raise OpenApiExecutionError("openapi tools accept one object argument")
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return execute_openapi_http_tool(
            execution_config=getattr(binding, "backend_config", None),
            input_schema=getattr(binding, "input_schema", None),
            input_data=payload,
        )

    _invoke.__name__ = getattr(binding, "local_name", "openapi_http_tool")
    return _invoke


def execute_openapi_http_tool(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """Validate input, inject auth, and perform one guarded HTTP request."""

    config = dict(execution_config or {})
    if not config:
        raise OpenApiExecutionError("openapi tool has no backend_config")
    if input_schema:
        try:
            jsonschema.validate(instance=input_data, schema=input_schema)
        except jsonschema.ValidationError as exc:
            raise OpenApiExecutionError(
                f"openapi tool input failed schema validation: {exc.message}"
            ) from exc

    method = str(config.get("method") or "GET").upper()
    path = str(config.get("path") or "")
    server_url = str(config.get("server_url") or "").rstrip("/")
    if not server_url:
        raise OpenApiExecutionError("openapi tool has no server_url configured")
    if not path.startswith("/"):
        raise OpenApiExecutionError(f"openapi tool path must start with '/': {path!r}")

    path_params = _as_mapping(input_data.get("path_params"))
    query_params = _as_mapping(input_data.get("query_params"))
    header_params = _as_mapping(input_data.get("header_params"))
    body = input_data.get("body")

    url = server_url + _render_path(path, path_params)
    headers = {str(key): str(value) for key, value in header_params.items()}
    auth_binding = config.get("auth_binding")
    auth = validate_auth_binding(auth_binding)
    headers.update(auth["headers"])
    query_params.update(auth["query"])

    timeout = float(config.get("timeout_seconds") or 30.0)
    request_content_types = [
        str(item)
        for item in (config.get("request_content_types") or [])
        if isinstance(item, str) and item.strip()
    ]
    request_kwargs = _request_kwargs(
        method=method,
        headers=headers,
        body=body,
        request_content_types=request_content_types,
        query_params=query_params,
    )
    try:
        with build_client(timeout=timeout) as client:
            response = client.request(method, url, **request_kwargs)
    except EgressBlockedError as exc:
        raise OpenApiExecutionError(str(exc)) from exc

    parsed_json: Any = None
    try:
        parsed_json = response.json()
    except Exception:
        parsed_json = None
    return {
        "status_code": response.status_code,
        "text": response.text,
        "json": parsed_json,
        "headers": dict(response.headers),
    }


def validate_auth_binding(auth_binding: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Resolve an auth binding to concrete headers/query params.

    Returns only the materialized transport pieces; callers remain responsible
    for never logging them.
    """

    binding = dict(auth_binding or {})
    kind = str(binding.get("kind") or "none")
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    if kind == "none":
        return {"headers": headers, "query": query}
    if kind == "bearer":
        secret = _required_secret(binding.get("secret_ref"), "secret_ref")
        scheme = str(binding.get("prefix") or "Bearer").strip() or "Bearer"
        headers["Authorization"] = f"{scheme} {secret}"
        return {"headers": headers, "query": query}
    if kind == "api_key":
        secret = _required_secret(binding.get("secret_ref"), "secret_ref")
        header_name = str(binding.get("header_name") or "").strip()
        query_name = str(binding.get("query_param_name") or "").strip()
        if header_name:
            headers[header_name] = secret
        if query_name:
            query[query_name] = secret
        return {"headers": headers, "query": query}
    if kind == "basic":
        username = str(binding.get("username") or "").strip()
        if not username:
            raise OpenApiExecutionError("basic auth requires username")
        password = _required_secret(binding.get("password_secret_ref"), "password_secret_ref")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        return {"headers": headers, "query": query}
    if kind == "header":
        header_name = str(binding.get("header_name") or "").strip()
        if not header_name:
            raise OpenApiExecutionError("header auth requires header_name")
        secret = _required_secret(binding.get("secret_ref"), "secret_ref")
        prefix = str(binding.get("prefix") or "").strip()
        headers[header_name] = f"{prefix} {secret}".strip() if prefix else secret
        return {"headers": headers, "query": query}
    raise OpenApiExecutionError(f"unsupported auth binding kind {kind!r}")


def _required_secret(raw_source: object, field_name: str) -> str:
    source = str(raw_source or "").strip()
    if not source:
        raise OpenApiExecutionError(f"auth binding requires {field_name}")
    resolved = resolve_secret(source)
    if not resolved:
        raise OpenApiExecutionError(f"could not resolve secret source {source!r}")
    return resolved


def _render_path(path: str, path_params: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in path_params:
            raise OpenApiExecutionError(f"missing required path parameter {name!r}")
        value = path_params[name]
        return quote("" if value is None else str(value), safe="")

    return _PATH_PARAM.sub(replace, path)


def _request_kwargs(
    *,
    method: str,
    headers: dict[str, str],
    body: Any,
    request_content_types: list[str],
    query_params: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headers": headers}
    if query_params:
        kwargs["params"] = query_params
    if method == "GET":
        return kwargs
    if isinstance(body, (dict, list)):
        if _prefers_json(request_content_types):
            kwargs["json"] = body
        else:
            headers.setdefault("Content-Type", request_content_types[0] if request_content_types else "application/json")
            kwargs["content"] = json.dumps(body)
        return kwargs
    if body is not None:
        if request_content_types:
            headers.setdefault("Content-Type", request_content_types[0])
        kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)
    return kwargs


def _prefers_json(request_content_types: list[str]) -> bool:
    if not request_content_types:
        return True
    return any("json" in item.lower() for item in request_content_types)


def _as_mapping(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {}
