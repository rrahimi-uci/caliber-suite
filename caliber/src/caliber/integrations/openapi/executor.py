"""Guarded declarative HTTP execution for OpenAPI-derived tools.

This module is the one place an OpenAPI-derived tool touches the network. It
reuses :mod:`caliber.egress` exactly the way the workflow ``webhook`` node does:
a pre-check refuses the obvious destinations, and the client that actually opens
the connection carries the policy in its transport so a name that re-resolves
after the check cannot be reached.

There are two entries. :func:`execute_openapi_http_tool` is the public one and sends only a **read** operation
(``GET``, ``HEAD``, ``OPTIONS``); any other method is refused with ``unsupported_effect_path``
(:class:`OpenApiUnsupportedEffectPathError`). :func:`_send_raw` is the private raw transport that sends any method: only the
dispatcher's private backends (document request and ``openapi_http``, B04.4, #750) and the two named development-profile
entries below (:func:`bind_legacy_openapi_http_tool` and :func:`execute_legacy_openapi_draft_preview`, which a reference
profile never reaches) may call it, and ``tests/test_effect_path_matrix.py`` fails when any other production module names it. A
protected call of a workflow tool node is proposed, approved and sent by the ``openapi_http`` backend, which calls
:func:`preflight_governed_openapi_request` first so that everything that can be refused without sending is refused before
an approval is requested. The callers that still reach the public entry are pinned in the same test.
"""

from __future__ import annotations

# This guarded executor is deliberately explicit: each auth and retry branch is
# fail-closed and kept visible for security review.
# ruff: noqa: PLR0912, PLR0915, PLR2004
import base64
import copy
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jsonschema

from caliber.egress import EgressBlockedError, EgressPolicy, build_client, check_url
from caliber.secrets import resolve_secret

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")

#: Methods that read: the only ones the public entry sends. Any other method is a protected effect.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The stable, payload-free code a protected call refused at the public entry carries (the dispatcher's refusal code too).
UNSUPPORTED_EFFECT_PATH = "unsupported_effect_path"

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

#: Request headers a caller-supplied ``header_params`` may never set (lower case). A method-override header lets a ``GET``
#: the platform classified as a read act as a write at a downstream that honours it, and the hop-by-hop and framing headers
#: are the transport's to set, so a caller could use them to smuggle or re-route a request. The refusal never names the
#: header or its value.
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "expect",
    }
)


def _has_forbidden_header(header_params: dict[str, Any]) -> bool:
    return any(name.strip().lower() in _FORBIDDEN_REQUEST_HEADERS for name in header_params)


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


#: Transport failures that prove the request never reached the downstream: the connection was never established
#: (refused, no route, DNS, a TLS handshake that failed, a connect timeout) or no connection was ever taken from the
#: pool, or our own request was refused as malformed before it was sent. Anything else (a dropped read, a read or write timeout, a protocol error, an unknown exception) may have
#: happened after the downstream received the request, so it is not in this set.
_NEVER_SENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,  # our own request is malformed (an illegal header value): refused before it is sent
)


class OpenApiExecutionError(RuntimeError):
    """Raised when a declarative OpenAPI-backed tool cannot execute safely.

    ``never_sent`` is ``True`` only when the request is known never to have reached the downstream: the call was
    refused before any attempt (input validation, configuration, authentication binding, the egress policy), or every
    attempt failed to connect. It is ``False`` when any attempt may have reached the downstream (a dropped read, a
    timeout after the request left, a 5xx answer that was then retried into a connection failure), and a caller that
    needs to know whether an effect could have happened must treat ``False`` as "it may have". ``attempts`` is how many
    requests were tried (0 when none was).
    """

    def __init__(
        self,
        message: str = "",
        *,
        never_sent: bool = False,
        attempts: int = 0,
        attempt_log: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.never_sent = never_sent
        self.attempts = attempts
        self.attempt_log = list(attempt_log or [])


class OpenApiUnsupportedEffectPathError(OpenApiExecutionError):
    """The public entry was asked to send a protected (non-read) operation outside the dispatcher.

    ``code`` is :data:`UNSUPPORTED_EFFECT_PATH` and the message is fixed: it names no method, URL, tool or input value.
    Nothing was prepared or sent (``never_sent`` is ``True``, ``attempts`` is 0), so a caller may treat it as a refusal.
    """

    code = UNSUPPORTED_EFFECT_PATH

    def __init__(self) -> None:
        super().__init__(
            "unsupported_effect_path: a protected OpenAPI call is sent only by the effect dispatcher",
            never_sent=True,
            attempts=0,
        )


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
    """Bind an OpenAPI-backed tool to an executable callable: the public entry, so it sends only a read."""
    return _bind_tool(binding, egress_policy, lambda **kw: execute_openapi_http_tool(**kw))  # noqa: PLW0108 - looked up at call time


def bind_legacy_openapi_http_tool(
    binding: Any,
    *,
    egress_policy: EgressPolicy | None = None,
) -> Callable[..., Any]:
    """**Development-profile compatibility only**: bind a tool that sends any method through the private raw transport.

    This is not the governed path: no proposal, no exact-intent approval, no execution-owner lease and no receipt. It exists so
    a manifest-backed plan outside the reference profile that has no governed context bound (``RuntimePlan.legacy_guarded_effects``,
    #1068) keeps running a protected OpenAPI tool node the way it did before the dispatcher routing. The workflow runtime is the
    only module that may name it (pinned by ``tests/test_effect_path_matrix.py``), and only for such a plan; the reference
    profile never binds it, so there a protected call without a context is refused.
    """
    return _bind_tool(binding, egress_policy, lambda **kw: _send_raw(**kw))  # noqa: PLW0108 - looked up at call time


def execute_legacy_openapi_draft_preview(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """**Development-profile only**: fire one draft-preview call of any method through the private raw transport.

    This is the operator's draft preview (inventory EP-033, exempt) outside the reference profile: one real upstream call
    fired by a human and gated by the draft's ``allow_in_preview``, with no proposal, approval, lease or receipt. The reference
    profile never reaches it (preview there sends only a read through :func:`execute_openapi_http_tool`, #1072), because
    ``allow_in_preview`` is set by the same operator and a write fired this way would be self-approvable. The preview route is
    the only module that may name it (pinned by ``tests/test_effect_path_matrix.py``).
    """
    return _send_raw(
        execution_config=execution_config,
        input_schema=input_schema,
        input_data=input_data,
        egress_policy=egress_policy,
    )


def _bind_tool(
    binding: Any,
    egress_policy: EgressPolicy | None,
    send: Callable[..., dict[str, Any]],
) -> Callable[..., Any]:
    def _invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and kwargs:
            raise OpenApiExecutionError(
                f"openapi tool {getattr(binding, 'local_name', 'tool')!r} got both args and kwargs",
                never_sent=True,
            )
        if args:
            if len(args) != 1 or not isinstance(args[0], dict):
                raise OpenApiExecutionError(
                    "openapi tools accept one object argument", never_sent=True
                )
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return send(
            execution_config=getattr(binding, "backend_config", None),
            input_schema=getattr(binding, "input_schema", None),
            input_data=payload,
            egress_policy=egress_policy,
        )

    _invoke.__name__ = getattr(binding, "local_name", "openapi_http_tool")
    return _invoke


@dataclass(frozen=True)
class _Prepared:
    """A request that passed every check that can run before anything is sent."""

    policy: EgressPolicy
    method: str
    url: str
    request_kwargs: dict[str, Any] = field(repr=False)  # holds the auth headers
    timeout: float
    max_attempts: int
    idempotency_key: str | None


def _required_config(execution_config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(execution_config or {})
    if not config:
        raise OpenApiExecutionError("openapi tool has no backend_config")
    return config


def _validate_input(input_schema: dict[str, Any] | None, input_data: dict[str, Any]) -> None:
    if input_schema:
        try:
            jsonschema.validate(instance=input_data, schema=input_schema)
        except jsonschema.ValidationError as exc:
            raise OpenApiExecutionError(
                f"openapi tool input failed schema validation: {exc.message}"
            ) from exc


def _operation_parts(config: dict[str, Any]) -> tuple[str, str, str]:
    """The method, the path template and the server URL of one single-operation config."""
    method = str(config.get("method") or "GET").upper()
    path = str(config.get("path") or "")
    server_url = str(config.get("server_url") or "").rstrip("/")
    if not server_url:
        raise OpenApiExecutionError("openapi tool has no server_url configured")
    if not path.startswith("/"):
        raise OpenApiExecutionError(f"openapi tool path must start with '/': {path!r}")
    return method, path, server_url


def _prepare_request(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None,
) -> _Prepared:
    config = _required_config(execution_config)
    _validate_input(input_schema, input_data)
    if config.get("kind") == "openapi_http_pack":
        config = _resolve_pack_operation(config, input_data)
    method, path, server_url = _operation_parts(config)

    path_params = _as_mapping(input_data.get("path_params"))
    query_params = _as_mapping(input_data.get("query_params"))
    header_params = _as_mapping(input_data.get("header_params"))
    if _has_forbidden_header(header_params):
        raise OpenApiExecutionError(
            "header_params may not set a method-override, framing or hop-by-hop header"
        )
    body = input_data.get("body")

    url = server_url + _render_path(path, path_params)
    headers = {str(key): str(value) for key, value in header_params.items()}
    key_header = str(config.get("idempotency_key_header") or "").strip()
    key = ""
    if key_header:
        key = (
            _single_idempotency_key(
                headers, key_header, str(input_data.get("idempotency_key") or "")
            )
            or ""
        )
    expected_header = str(config.get("expected_resource_version_header") or "").strip()
    expected = None
    if expected_header:
        expected = _single_bound_header_value(
            headers,
            expected_header,
            input_data.get("expected_resource_version", _MISSING),
            field_name="expected_resource_version",
        )

    policy = egress_policy if egress_policy is not None else active_egress_policy()
    auth_binding = config.get("auth_binding")
    auth = validate_auth_binding(auth_binding, egress_policy=policy)
    headers.update(auth["headers"])
    query_params.update(auth["query"])

    key_sent = False
    if key_header:
        key = _single_idempotency_key(headers, key_header, key) or ""
        if key:
            headers[key_header] = key
            key_sent = True
    # ``key_sent``: the outgoing request really carries exactly one key (whether it came from the ``idempotency_key``
    # input or from ``header_params``, in any header-name case). A bound header with no key behind it must not turn on
    # retries of a POST, and two different keys are refused rather than sent.

    # Defence in depth, matching the webhook node: refuse the obvious destination
    # here, then let the transport re-check the address it actually connects to.
    try:
        check_url(url, policy)
    except EgressBlockedError as exc:
        raise OpenApiExecutionError(str(exc)) from exc

    if expected_header:
        expected = _single_bound_header_value(
            headers,
            expected_header,
            expected if expected is not None else _MISSING,
            field_name="expected_resource_version",
        )
        if expected is not None:
            headers[expected_header] = expected

    timeout = _timeout_seconds(config.get("timeout_seconds"))
    max_attempts = _max_attempts(config, method=method, has_idempotency_key=key_sent)
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

    return _Prepared(
        policy=policy,
        method=method,
        url=url,
        request_kwargs=request_kwargs,
        timeout=timeout,
        max_attempts=max_attempts,
        idempotency_key=key if key_sent else None,
    )


def execute_openapi_http_tool(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """Validate input, inject auth, and perform one guarded **read** request (the public entry).

    This entry sends only an operation whose method reads (``GET``, ``HEAD``, ``OPTIONS``). Any other method is a protected
    effect, which only the dispatcher's private ``openapi_http`` backend (and the document-request backend) may send, through
    :func:`_send_raw`: here it is refused with :class:`OpenApiUnsupportedEffectPathError` (``unsupported_effect_path``) before
    anything is prepared, resolved or sent, so no caller of this function can perform a write without a recorded, approved
    proposal. The method is read from the **configuration** (a pack's operation is selected by the input), never from a
    claim; a configuration that resolves to no operation falls through to the transport's own configuration refusal.
    Whether a read-method operation is *declared* a write (``side_effect_level``) is the caller's to check (the workflow
    runtime classifies from the registry row; Aria refuses a tool whose level is not ``read``).

    ``egress_policy`` lets a call site with a better policy (a workflow plan, a
    request-scoped config) override the process-wide one. Omitting it does not
    mean "unrestricted" — see :func:`active_egress_policy`.
    """

    try:
        # One snapshot is both classified and sent, so a caller (or another thread) that changes the configuration or the
        # input after the check cannot turn the call into a different operation than the one that was checked.
        config = copy.deepcopy(execution_config)
        data = copy.deepcopy(input_data)
        try:
            method: str | None = openapi_operation_method(config, data)
        except OpenApiPreflightError:
            method = None  # no operation to classify: the transport refuses the configuration, sending nothing
    except Exception as exc:
        # A configuration that is not even a mapping, or an input that cannot be copied: refused, nothing was sent.
        exc.never_sent = True  # type: ignore[attr-defined]
        exc.attempts = 0  # type: ignore[attr-defined]
        raise
    if method is not None and method not in READ_METHODS:
        raise OpenApiUnsupportedEffectPathError
    return _send_raw(
        execution_config=config,
        input_schema=input_schema,
        input_data=data,
        egress_policy=egress_policy,
    )


def _send_raw(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """The **private raw transport**: validate, inject auth and send one request of any method, with no governance of its own.

    Only the dispatcher's private backends (document request, ``openapi_http``) and the operator's draft preview route may
    call it, and ``tests/test_effect_path_matrix.py`` fails when any other production module names it. It never calls
    :func:`execute_openapi_http_tool`, so a dispatched effect cannot re-enter the dispatcher or the public entry. Retries are
    bounded by :func:`_max_attempts`: a non-idempotent method is retried only when the request really carries a key.
    """

    try:
        prepared = _prepare_request(
            execution_config=execution_config,
            input_schema=input_schema,
            input_data=input_data,
            egress_policy=egress_policy,
        )
    except Exception as exc:
        # Refused before any request was built, so nothing was sent. Any exception type gets the marker (a ValueError from
        # a malformed timeout, a schema error), so a caller classifying it never mistakes it for "may have been sent".
        exc.never_sent = True  # type: ignore[attr-defined]
        exc.attempts = 0  # type: ignore[attr-defined]
        raise
    policy, method, url = prepared.policy, prepared.method, prepared.url
    request_kwargs, timeout, max_attempts = (
        prepared.request_kwargs,
        prepared.timeout,
        prepared.max_attempts,
    )

    attempt = 0
    last_error: Exception | None = None
    response = None
    possibly_sent = False  # whether any attempt may have reached the downstream
    earlier_sent = False  # whether any attempt *before the last one* may have
    attempt_log: list[dict[str, Any]] = []
    while attempt < max_attempts:
        attempt += 1
        earlier_sent = possibly_sent
        record: dict[str, Any] = {
            "attempt": attempt,
            "emitted_idempotency_key": prepared.idempotency_key,
        }
        try:
            with build_client(policy=policy, timeout=timeout) as client:
                response = client.request(method, url, **request_kwargs)
        except EgressBlockedError as exc:
            # A block is a policy decision, not a transient fault. Retrying it would
            # only repeat the same refusal.
            record.update(
                {
                    "status": "blocked",
                    "transport_outcome": "never_sent",
                    "error_type": type(exc).__name__,
                }
            )
            attempt_log.append(record)
            raise OpenApiExecutionError(
                str(exc),
                never_sent=not possibly_sent,
                attempts=attempt,
                attempt_log=attempt_log,
            ) from exc
        except Exception as exc:  # transport-level failure: connect reset, timeout
            last_error = exc
            response = None
            never_sent = isinstance(exc, _NEVER_SENT_ERRORS)
            if not never_sent:
                possibly_sent = True
            record.update(
                {
                    "status": "transport_error",
                    "transport_outcome": "never_sent" if never_sent else "ambiguous",
                    "error_type": type(exc).__name__,
                }
            )
            attempt_log.append(record)
            if attempt >= max_attempts:
                break
            time.sleep(_backoff_seconds(attempt))
            continue
        possibly_sent = True  # the downstream answered, so it received the request
        record.update(
            {
                "status": "response",
                "status_code": response.status_code,
                "transport_outcome": "response",
            }
        )
        attempt_log.append(record)
        if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
            time.sleep(_backoff_seconds(attempt))
            continue
        break

    if response is None:
        raise OpenApiExecutionError(
            f"openapi tool request failed after {attempt} attempt(s): {last_error}",
            never_sent=not possibly_sent,
            attempts=attempt,
            attempt_log=attempt_log,
        ) from last_error

    parsed_json: Any = None
    try:
        parsed_json = response.json()
    except Exception:
        parsed_json = None
    replay_confirmed = bool(
        prepared.idempotency_key
        and response.status_code == 200
        and any(item.get("status_code") == 201 for item in attempt_log)
    )
    replay_candidate = bool(prepared.idempotency_key and response.status_code == 200)
    return {
        "status_code": response.status_code,
        "text": response.text,
        "json": parsed_json,
        "headers": _safe_response_headers(response.headers),
        "attempts": attempt,
        "attempt_log": attempt_log,
        "replay_disposition": (
            "replay_confirmed"
            if replay_confirmed
            else "replay_candidate"
            if replay_candidate
            else "not_replay"
        ),
        # The executor has not performed the connector's authoritative lookup;
        # a 2xx response is therefore never advertised as a verified receipt.
        "receipt_disposition": "unverified",
        # Whether an attempt before the last one may have reached the downstream: a 4xx then proves nothing about it.
        "earlier_attempts_may_have_been_sent": earlier_sent,
    }


#: Every refusal :func:`preflight_governed_openapi_request` and :func:`openapi_operation_method` raise. A code names a class of
#: problem and never carries a value from the input, the configuration or the downstream.
PREFLIGHT_CONFIG_INVALID = "openapi_config_invalid"
PREFLIGHT_INPUT_INVALID = "openapi_input_invalid"
PREFLIGHT_OPERATION_UNRESOLVED = "openapi_operation_unresolved"
PREFLIGHT_KEY_OVERRIDE = "openapi_key_override_refused"
PREFLIGHT_PRECONDITION_CONFLICT = "openapi_precondition_conflict"
PREFLIGHT_EGRESS_BLOCKED = "openapi_egress_blocked"
PREFLIGHT_AUTH_UNSUPPORTED = "openapi_auth_unsupported"
PREFLIGHT_HEADER_FORBIDDEN = "openapi_header_forbidden"
PREFLIGHT_REFUSAL_CODES = (
    PREFLIGHT_CONFIG_INVALID,
    PREFLIGHT_INPUT_INVALID,
    PREFLIGHT_OPERATION_UNRESOLVED,
    PREFLIGHT_KEY_OVERRIDE,
    PREFLIGHT_PRECONDITION_CONFLICT,
    PREFLIGHT_EGRESS_BLOCKED,
    PREFLIGHT_AUTH_UNSUPPORTED,
    PREFLIGHT_HEADER_FORBIDDEN,
)
_AUTH_KINDS = frozenset(
    {
        "none",
        "bearer",
        "api_key",
        "basic",
        "header",
        "oauth_client_credentials",
        "oauth_refresh_token",
    }
)
_OAUTH_KINDS = frozenset({"oauth_client_credentials", "oauth_refresh_token"})


class OpenApiPreflightError(Exception):
    """A governed OpenAPI call is refused before anything is proposed or sent; ``code`` is one of :data:`PREFLIGHT_REFUSAL_CODES`."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OpenApiPreflight:
    """What a governed call is, as checked without sending: the operation and the input a proposal may record.

    ``path`` is the path **template** (the parameters are not substituted), so the operation is the same for every call.
    ``replay_input`` is the input without the two things the platform owns: the precondition (lifted into
    ``expected_resource_version``, which the proposal records and the transport sends as the bound header) and the
    idempotency key (refused when supplied, never recorded: the transport sends the proposal's stable key).
    """

    method: str
    server_url: str
    path: str
    idempotency_key_header: str | None
    expected_resource_version_header: str | None
    expected_resource_version: str | None
    replay_input: dict[str, Any] = field(repr=False)


def _preflight_operation(
    execution_config: dict[str, Any] | None, input_data: dict[str, Any]
) -> tuple[dict[str, Any], str, str, str]:
    """The single-operation config, method, path template and server URL, or a typed refusal."""
    try:
        config = _required_config(execution_config)
    except OpenApiExecutionError:
        raise OpenApiPreflightError(PREFLIGHT_CONFIG_INVALID) from None
    if config.get("kind") == "openapi_http_pack":
        try:
            config = _resolve_pack_operation(config, input_data)
        except OpenApiExecutionError:
            raise OpenApiPreflightError(PREFLIGHT_OPERATION_UNRESOLVED) from None
    try:
        method, path, server_url = _operation_parts(config)
    except OpenApiExecutionError:
        raise OpenApiPreflightError(PREFLIGHT_CONFIG_INVALID) from None
    return config, method, path, server_url


def openapi_operation_method(
    execution_config: dict[str, Any] | None, input_data: dict[str, Any]
) -> str:
    """The HTTP method the call will use, resolved from the **configuration** (a pack's operation is selected by the input).

    The method is what classifies the call, so it is read from the registry row's own configuration, never from a claim. A
    configuration that names no operation the input can select is refused (:data:`PREFLIGHT_OPERATION_UNRESOLVED`).
    """
    return _preflight_operation(execution_config, input_data)[1]


def _check_server_url(server_url: str) -> None:
    """An ``http(s)`` origin with a host, a valid port, and no credentials, query or fragment: it is recorded in a resource name."""
    try:
        parts = urlsplit(server_url)
        _ = parts.port  # a non-numeric or out-of-range port raises ValueError
    except ValueError:
        raise OpenApiPreflightError(PREFLIGHT_CONFIG_INVALID) from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or any(ch.isspace() or not ch.isprintable() for ch in server_url)
    ):
        raise OpenApiPreflightError(PREFLIGHT_CONFIG_INVALID)


def _check_auth_target(auth_binding: object, policy: EgressPolicy) -> None:
    """The auth kind must be one the executor supports, and an OAuth token URL passes the egress policy: no secret is resolved."""
    if auth_binding is None:
        return
    if not isinstance(auth_binding, dict):
        raise OpenApiPreflightError(PREFLIGHT_CONFIG_INVALID)
    kind = str(auth_binding.get("kind") or "none")
    if kind not in _AUTH_KINDS:
        raise OpenApiPreflightError(PREFLIGHT_AUTH_UNSUPPORTED)
    if kind in _OAUTH_KINDS:
        try:
            check_url(str(auth_binding.get("token_url") or "").strip(), policy)
        except EgressBlockedError:
            raise OpenApiPreflightError(PREFLIGHT_EGRESS_BLOCKED) from None


def preflight_governed_openapi_request(
    *,
    execution_config: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    input_data: dict[str, Any],
    egress_policy: EgressPolicy | None = None,
) -> OpenApiPreflight:
    """Check a governed OpenAPI call before it is proposed, so an approval is never spent on a call that cannot be made.

    Nothing is sent and no secret is resolved. The input must satisfy the tool's schema; a pack's operation must be one the
    configuration binds; the server must be a plain ``http(s)`` origin (its text goes into the proposal's resource); the
    request URL and an OAuth token URL must pass the egress policy (the transport applies it again when it connects);
    ``header_params`` may not set a method-override, framing or hop-by-hop header (:data:`PREFLIGHT_HEADER_FORBIDDEN`).
    When the operation binds an idempotency key header, **the platform owns the key**: an ``idempotency_key`` input or a
    ``header_params`` entry for that header is refused (:data:`PREFLIGHT_KEY_OVERRIDE`) rather than sent next to the
    proposal's key. When it binds a precondition header, the ``expected_resource_version`` input and a ``header_params``
    entry must agree (:data:`PREFLIGHT_PRECONDITION_CONFLICT`), and the value is lifted into
    :attr:`OpenApiPreflight.expected_resource_version`. Every refusal is an :class:`OpenApiPreflightError` with a stable
    code and no value from the input.
    """
    config, method, path, server_url = _preflight_operation(execution_config, input_data)
    try:
        _validate_input(input_schema, input_data)
    except OpenApiExecutionError:
        raise OpenApiPreflightError(PREFLIGHT_INPUT_INVALID) from None
    _check_server_url(server_url)
    header_params = _as_mapping(input_data.get("header_params"))
    if _has_forbidden_header(header_params):
        raise OpenApiPreflightError(PREFLIGHT_HEADER_FORBIDDEN)
    try:
        url = server_url + _render_path(path, _as_mapping(input_data.get("path_params")))
    except OpenApiExecutionError:
        raise OpenApiPreflightError(PREFLIGHT_INPUT_INVALID) from None

    key_header = str(config.get("idempotency_key_header") or "").strip()
    if key_header and (
        str(input_data.get("idempotency_key") or "").strip()
        or any(name.lower() == key_header.lower() for name in header_params)
    ):
        raise OpenApiPreflightError(PREFLIGHT_KEY_OVERRIDE)

    replay = dict(input_data)
    expected_header = str(config.get("expected_resource_version_header") or "").strip()
    expected: str | None = None
    if expected_header:
        try:
            expected = _single_bound_header_value(
                {name: str(value) for name, value in header_params.items()},
                expected_header,
                input_data.get("expected_resource_version", _MISSING),
                field_name="expected_resource_version",
            )
        except OpenApiExecutionError:
            raise OpenApiPreflightError(PREFLIGHT_PRECONDITION_CONFLICT) from None
        replay.pop("expected_resource_version", None)
        if "header_params" in replay:
            replay["header_params"] = {
                name: value
                for name, value in header_params.items()
                if name.lower() != expected_header.lower()
            }

    policy = egress_policy if egress_policy is not None else active_egress_policy()
    try:
        check_url(url, policy)
    except EgressBlockedError:
        raise OpenApiPreflightError(PREFLIGHT_EGRESS_BLOCKED) from None
    _check_auth_target(config.get("auth_binding"), policy)
    return OpenApiPreflight(
        method=method,
        server_url=server_url,
        path=path,
        idempotency_key_header=key_header or None,
        expected_resource_version_header=expected_header or None,
        expected_resource_version=expected,
        replay_input=replay,
    )


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


_MISSING = object()


def _single_bound_header_value(
    headers: dict[str, str],
    header: str,
    supplied: object,
    *,
    field_name: str,
) -> str | None:
    """Resolve one canonical proposal-bound value and reject conflicting headers."""

    values: list[str] = []
    for name in [name for name in headers if name.lower() == header.lower()]:
        value = str(headers.pop(name)).strip()
        if not value:
            raise OpenApiExecutionError(f"the {header} header must not be blank")
        values.append(value)
    if supplied is not _MISSING and supplied is not None:
        value = str(supplied).strip()
        if not value:
            raise OpenApiExecutionError(f"{field_name} must not be blank")
        values.append(value)
    if len(set(values)) > 1:
        raise OpenApiExecutionError(f"conflicting values were supplied for {field_name}")
    return values[0] if values else None


def _timeout_seconds(raw: object) -> float:
    try:
        value = 30.0 if raw is None else float(str(raw))
    except (TypeError, ValueError) as exc:
        # Keep malformed direct-call configuration as ValueError for callers that
        # validate executor inputs before the request is built. The public executor
        # annotates this pre-flight exception with never_sent/attempts metadata.
        raise ValueError("timeout_seconds must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    return value


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
                    "idempotency_key_header": entry.get("idempotency_key_header"),
                    "expected_resource_version_header": entry.get(
                        "expected_resource_version_header"
                    ),
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


def _single_idempotency_key(headers: dict[str, str], header: str, supplied: str) -> str | None:
    """The one idempotency key the request carries, with every case-variant of the header removed from ``headers``.

    ``None`` means no key. Two different keys (the ``idempotency_key`` input and a ``header_params`` entry, or two
    spellings of the header), or a blank value in the header, are refused: sending either would make the emitted key
    ambiguous or turn a bad key into a keyless request.
    """
    values: list[str] = []
    for name in [name for name in headers if name.lower() == header.lower()]:
        value = str(headers.pop(name)).strip()
        if not value:
            raise OpenApiExecutionError(f"the {header} header must not be blank")
        values.append(value)
    if supplied.strip():
        values.append(supplied.strip())
    if len(set(values)) > 1:
        raise OpenApiExecutionError(f"two different idempotency keys were supplied for {header}")
    return values[0] if values else None


def _render_path(path: str, path_params: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in path_params:
            raise OpenApiExecutionError(f"missing required path parameter {name!r}")
        value = path_params[name]
        text = "" if value is None else str(value)
        if text in (
            ".",
            "..",
        ):  # quote() leaves dots alone and the client would collapse the segment
            raise OpenApiExecutionError(f"path parameter {name!r} must not be a dot segment")
        return quote(text, safe="")

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
    # A read carries no body: a body on HEAD or OPTIONS is a request-smuggling surface.
    if method in READ_METHODS:
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
