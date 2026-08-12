"""Guarded declarative HTTP execution for OpenAPI-derived tools.

This module is the one place an OpenAPI-derived tool touches the network. It
reuses :mod:`caliber.egress` exactly the way the workflow ``webhook`` node does:
a pre-check refuses the obvious destinations, and the client that actually opens
the connection carries the policy in its transport so a name that re-resolves
after the check cannot be reached.
"""

from __future__ import annotations

# This guarded executor is deliberately explicit: each auth and retry branch is
# fail-closed and kept visible for security review.
# ruff: noqa: PLR0912, PLR0915, PLR2004
import base64
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import jsonschema

from caliber.egress import EgressBlockedError, EgressPolicy, build_client, check_url
from caliber.secrets import resolve_secret

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")

#: Response headers never copied into a tool result. A tool output is persisted on
#: run rows, shown in the UI, and fed back to an agent, so upstream credential
#: material must not ride along in it.
_REDACTED_RESPONSE_HEADERS = frozenset(
    {
        "set-cookie",
        "set-cookie2",
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "proxy-authenticate",
    }
)

#: Status codes worth one more attempt. A 5xx or 429 is plausibly transient; a 4xx
#: is the caller's problem and retrying only burns the upstream's rate budget.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Methods safe to retry without an idempotency key. ``POST``/``PATCH`` are excluded
#: because a retried create is a duplicate create — see ``idempotency_key`` below.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Process-wide egress policy for OpenAPI-derived tools. Bound at startup by
#: ``create_app``. Process-global for the same reason the registered-tool module
#: allowlist is: this executor is reached from the preview route, the tool
#: test-run route, the workflow runtime, and standalone exported tools, and a
#: policy only some of those paths applied would not be a policy.
_ACTIVE_EGRESS_POLICY: EgressPolicy | None = None
_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, tuple[str, float | None]] = {}


class OpenApiExecutionError(RuntimeError):
    """Raised when a declarative OpenAPI-backed tool cannot execute safely."""


def bind_egress_policy(policy: EgressPolicy | None) -> None:
    """Install the process-wide egress policy for OpenAPI-derived tools."""

    global _ACTIVE_EGRESS_POLICY  # noqa: PLW0603 - one process-wide policy by design
    _ACTIVE_EGRESS_POLICY = policy


def active_egress_policy() -> EgressPolicy:
    """Return the bound policy, or the safe default when nothing was bound.

    Defaulting to ``EgressPolicy()`` rather than ``None`` matters: an unbound
    process (a standalone exported tool, a test, a preview path that never
    reached ``create_app``) still blocks loopback, link-local, and private
    ranges instead of running unconstrained.
    """

    return _ACTIVE_EGRESS_POLICY if _ACTIVE_EGRESS_POLICY is not None else EgressPolicy()


def bind_openapi_http_tool(
    binding: Any,
    *,
    egress_policy: EgressPolicy | None = None,
) -> Callable[..., Any]:
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
            egress_policy=egress_policy,
        )

    _invoke.__name__ = getattr(binding, "local_name", "openapi_http_tool")
    return _invoke


def execute_openapi_http_tool(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """Validate input, inject auth, and perform one guarded HTTP request.

    ``egress_policy`` lets a call site with a better policy (a workflow plan, a
    request-scoped config) override the process-wide one. Omitting it does not
    mean "unrestricted" — see :func:`active_egress_policy`.
    """

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

    if config.get("kind") == "openapi_http_pack":
        config = _resolve_pack_operation(config, input_data)

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
    policy = egress_policy if egress_policy is not None else active_egress_policy()
    auth_binding = config.get("auth_binding")
    auth = validate_auth_binding(auth_binding, egress_policy=policy)
    headers.update(auth["headers"])
    query_params.update(auth["query"])

    idempotency_key = str(config.get("idempotency_key_header") or "").strip()
    if idempotency_key:
        supplied = str(input_data.get("idempotency_key") or "").strip()
        if supplied:
            headers.setdefault(idempotency_key, supplied)

    # Defence in depth, matching the webhook node: refuse the obvious destination
    # here, then let the transport re-check the address it actually connects to.
    try:
        check_url(url, policy)
    except EgressBlockedError as exc:
        raise OpenApiExecutionError(str(exc)) from exc

    timeout = float(config.get("timeout_seconds") or 30.0)
    max_attempts = _max_attempts(config, method=method, has_idempotency_key=bool(idempotency_key))
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

    attempt = 0
    last_error: Exception | None = None
    response = None
    while attempt < max_attempts:
        attempt += 1
        try:
            with build_client(policy=policy, timeout=timeout) as client:
                response = client.request(method, url, **request_kwargs)
        except EgressBlockedError as exc:
            # A block is a policy decision, not a transient fault. Retrying it would
            # only repeat the same refusal.
            raise OpenApiExecutionError(str(exc)) from exc
        except Exception as exc:  # transport-level failure: connect reset, timeout
            last_error = exc
            response = None
            if attempt >= max_attempts:
                break
            time.sleep(_backoff_seconds(attempt))
            continue
        if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
            time.sleep(_backoff_seconds(attempt))
            continue
        break

    if response is None:
        raise OpenApiExecutionError(
            f"openapi tool request failed after {attempt} attempt(s): {last_error}"
        ) from last_error

    parsed_json: Any = None
    try:
        parsed_json = response.json()
    except Exception:
        parsed_json = None
    return {
        "status_code": response.status_code,
        "text": response.text,
        "json": parsed_json,
        "headers": _safe_response_headers(response.headers),
        "attempts": attempt,
    }


def _max_attempts(config: dict[str, Any], *, method: str, has_idempotency_key: bool) -> int:
    """Resolve the attempt ceiling for one operation.

    A non-idempotent method only retries when the operator bound an idempotency
    key header, because a blind retry of a create is a duplicate create.
    """

    raw = config.get("max_attempts")
    try:
        requested = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        requested = 1
    requested = max(1, min(requested, 5))
    if requested == 1:
        return 1
    if method in _IDEMPOTENT_METHODS or has_idempotency_key:
        return requested
    return 1


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff: 0.25s, 0.5s, 1s, 2s."""

    return float(min(2.0, 0.25 * (2 ** (attempt - 1))))


def _safe_response_headers(headers: Any) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in dict(headers).items()
        if str(key).lower() not in _REDACTED_RESPONSE_HEADERS
    }


def _resolve_pack_operation(config: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    """Resolve a tool-pack config plus the caller's ``operation`` selector to
    one single-operation config, so everything below this point stays unaware
    packs exist at all.
    """

    selector = str(input_data.get("operation") or "").strip()
    if not selector:
        raise OpenApiExecutionError("openapi tool pack requires an 'operation' selector")
    operations = config.get("operations")
    if not isinstance(operations, list):
        raise OpenApiExecutionError("openapi tool pack has no bound operations")
    for entry in operations:
        if isinstance(entry, dict) and str(entry.get("operation_key")) == selector:
            resolved = dict(config)
            resolved.update(
                {
                    "method": entry.get("method"),
                    "path": entry.get("path"),
                    "request_content_types": entry.get("request_content_types") or [],
                    "response_statuses": entry.get("response_statuses") or [],
                }
            )
            return resolved
    known = [str(entry.get("operation_key")) for entry in operations if isinstance(entry, dict)]
    raise OpenApiExecutionError(
        f"openapi tool pack has no bound operation {selector!r}; expected one of {known}"
    )


def validate_auth_binding(
    auth_binding: dict[str, Any] | None,
    *,
    egress_policy: EgressPolicy | None = None,
) -> dict[str, dict[str, str]]:
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
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
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
    if kind in {"oauth_client_credentials", "oauth_refresh_token"}:
        scheme, token = _oauth_access_token(
            binding,
            egress_policy if egress_policy is not None else active_egress_policy(),
        )
        headers["Authorization"] = f"{scheme} {token}"
        return {"headers": headers, "query": query}
    raise OpenApiExecutionError(f"unsupported auth binding kind {kind!r}")


def _oauth_access_token(binding: dict[str, Any], policy: EgressPolicy) -> tuple[str, str]:
    cache_key = _oauth_cache_key(binding)
    cached = _oauth_cache_get(cache_key)
    if cached is not None:
        return cached

    token_url = str(binding.get("token_url") or "").strip()
    if not token_url:
        raise OpenApiExecutionError("oauth auth requires token_url")
    try:
        check_url(token_url, policy)
    except EgressBlockedError as exc:
        raise OpenApiExecutionError(str(exc)) from exc

    kind = str(binding.get("kind") or "")
    auth_method = str(binding.get("client_auth_method") or "").strip().lower() or "basic"
    client_id = str(binding.get("client_id") or "").strip()
    client_secret_ref = str(binding.get("client_secret_ref") or "").strip()
    client_secret = (
        _required_secret(client_secret_ref, "client_secret_ref") if client_secret_ref else ""
    )
    form: dict[str, str] = {}
    if kind == "oauth_client_credentials":
        form["grant_type"] = "client_credentials"
        if client_id and auth_method != "basic":
            form["client_id"] = client_id
        if client_secret and auth_method != "basic":
            form["client_secret"] = client_secret
    elif kind == "oauth_refresh_token":
        form["grant_type"] = "refresh_token"
        form["refresh_token"] = _required_secret(
            binding.get("refresh_token_secret_ref"),
            "refresh_token_secret_ref",
        )
        if client_id:
            form["client_id"] = client_id
        if client_secret and auth_method != "basic":
            form["client_secret"] = client_secret
    else:
        raise OpenApiExecutionError(f"unsupported oauth binding kind {kind!r}")

    scope = _oauth_scope(binding.get("scopes"))
    if scope:
        form["scope"] = scope
    audience = str(binding.get("audience") or "").strip()
    if audience:
        form["audience"] = audience
    resource = str(binding.get("resource") or "").strip()
    if resource:
        form["resource"] = resource

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if auth_method == "basic":
        if not client_id or not client_secret:
            raise OpenApiExecutionError(
                "oauth basic client auth requires client_id and client_secret_ref"
            )
        token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    elif auth_method != "body":
        raise OpenApiExecutionError("oauth client_auth_method must be 'basic' or 'body'")
    if auth_method == "body" and client_id and "client_id" not in form:
        form["client_id"] = client_id

    try:
        with build_client(policy=policy, timeout=15.0) as client:
            response = client.post(token_url, headers=headers, data=form)
    except EgressBlockedError as exc:
        raise OpenApiExecutionError(str(exc)) from exc
    except Exception as exc:
        raise OpenApiExecutionError(f"oauth token request failed: {exc}") from exc

    if response.status_code >= 400:
        raise OpenApiExecutionError(f"oauth token request returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenApiExecutionError("oauth token response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OpenApiExecutionError("oauth token response must be a JSON object")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise OpenApiExecutionError("oauth token response did not include access_token")
    token_type = (
        str(payload.get("token_type") or binding.get("prefix") or "Bearer").strip() or "Bearer"
    )
    expires_in = _oauth_expires_at(payload.get("expires_in"))
    resolved = (token_type, access_token.strip())
    _oauth_cache_put(cache_key, resolved, expires_in)
    return resolved


def _oauth_scope(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        items = [str(item).strip() for item in raw if str(item).strip()]
        return " ".join(items)
    return ""


def _oauth_expires_at(raw: object) -> float | None:
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return float(time.monotonic() + max(1, seconds - 30))


def _oauth_cache_key(binding: dict[str, Any]) -> str:
    return json.dumps(binding, sort_keys=True, default=str)


def _oauth_cache_get(cache_key: str) -> tuple[str, str] | None:
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is None:
            return None
        token, expires_at = cached
        if expires_at is not None and expires_at <= time.monotonic():
            _TOKEN_CACHE.pop(cache_key, None)
            return None
        scheme, _, access_token = token.partition(" ")
        if not access_token:
            return None
        return scheme, access_token


def _oauth_cache_put(
    cache_key: str,
    resolved: tuple[str, str],
    expires_at: float | None,
) -> None:
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE[cache_key] = (f"{resolved[0]} {resolved[1]}", expires_at)


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
            headers.setdefault(
                "Content-Type",
                request_content_types[0] if request_content_types else "application/json",
            )
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
