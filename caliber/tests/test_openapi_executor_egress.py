"""Egress tests for the declarative OpenAPI executor.

These tests deliberately do **not** monkeypatch ``build_client``. That is the whole
point: the route tests replace it with a plain ``httpx.Client`` over a
``MockTransport``, which swallows the real client's signature and let a call that
raised ``TypeError: build_client() missing 1 required keyword-only argument:
'policy'`` on every un-mocked invocation pass a full green suite. Anything here
that opens a socket goes through ``caliber.egress`` for real.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import (
    PREFLIGHT_REFUSAL_CODES,
    OpenApiExecutionError,
    OpenApiPreflight,
    OpenApiPreflightError,
    _send_raw,
    active_egress_policy,
    bind_egress_policy,
    execute_openapi_http_tool,
    openapi_operation_method,
    preflight_governed_openapi_request,
)


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "kind": "openapi_http",
        "method": "GET",
        "path": "/tickets/{ticket_id}",
        "server_url": "https://tickets.example.com",
        "auth_binding": None,
        "request_content_types": [],
    }
    config.update(overrides)
    return config


class _Handler(BaseHTTPRequestHandler):
    """Records requests and replays a scripted sequence of responses."""

    statuses: list[int] = []
    received: list[dict[str, Any]] = []
    responder: Any = None

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self._respond(body=self.rfile.read(length).decode("utf-8") if length else "")

    def _respond(self, body: str = "") -> None:
        type(self).received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        responder = type(self).responder
        if callable(responder):
            status, payload, headers = responder(self.path, body, dict(self.headers))
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        statuses = type(self).statuses
        index = len(type(self).received) - 1
        status = statuses[index] if index < len(statuses) else 200
        payload = json.dumps({"ticket_id": "T-1", "status": "open"}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # Must never reach the tool result — see ``_REDACTED_RESPONSE_HEADERS``.
        self.send_header("Set-Cookie", "upstream_session=super-secret")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def local_server():
    """A real HTTP server on loopback, so the egress transport does real work."""

    _Handler.statuses = []
    _Handler.received = []
    _Handler.responder = None
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_bound_policy():
    """Force the unbound (safe-default) policy for every test in this module.

    ``_ACTIVE_EGRESS_POLICY`` is process-global — the same design as
    ``bind_sandbox_config``/``bind_module_allowlist`` — so whichever app fixture
    last called ``create_app()`` in this test session may have left a permissive
    policy bound (e.g. a route test module that allowlists its own fake upstream).
    Resetting to ``None`` *before* each test, not only after, is what makes this
    module's "default policy" assertions hold regardless of what ran earlier in
    the same pytest session.
    """

    bind_egress_policy(None)
    yield
    bind_egress_policy(None)


def test_default_policy_blocks_loopback_instead_of_raising_typeerror() -> None:
    """The regression test for the missing ``policy=`` argument.

    Before the fix this raised ``TypeError`` from ``build_client`` — the request was
    never attempted and no policy was ever consulted. It must now be a *policy*
    refusal, which proves both that the call signature is right and that the
    default is closed rather than open.
    """

    with pytest.raises(OpenApiExecutionError) as excinfo:
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://127.0.0.1:9"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )
    message = str(excinfo.value)
    assert "TypeError" not in message
    assert "loopback" in message.lower() or "blocked" in message.lower()


def test_unbound_policy_defaults_to_closed() -> None:
    policy = active_egress_policy()
    assert policy.enabled is True
    assert policy.block_loopback is True
    assert policy.block_private is True
    assert policy.block_link_local is True


def test_bound_policy_is_used_when_no_override_is_passed(local_server) -> None:
    """A policy bound at startup reaches the executor with no threading at all."""

    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
    )

    assert result["status_code"] == 200
    assert result["json"] == {"ticket_id": "T-1", "status": "open"}
    assert handler.received[0]["path"] == "/tickets/T-1"


def test_explicit_policy_overrides_a_closed_bound_policy(local_server) -> None:
    server, _handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    bind_egress_policy(EgressPolicy())  # closed: would block loopback

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )
    assert result["status_code"] == 200


def test_response_credential_headers_are_not_returned(local_server) -> None:
    server, _handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    lowered = {key.lower() for key in result["headers"]}
    assert "set-cookie" not in lowered
    assert "content-type" in lowered
    assert "super-secret" not in json.dumps(result["headers"])


def test_retry_recovers_a_retryable_status(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}", max_attempts=3),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert len(handler.received) == 2


def test_client_error_is_not_retried(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [404, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}", max_attempts=3),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 404
    assert result["attempts"] == 1
    assert len(handler.received) == 1


def test_non_idempotent_method_is_not_retried_without_an_idempotency_key(local_server) -> None:
    """A retried POST is a duplicate create. Silence is not an acceptable default."""

    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = _send_raw(
        execution_config=_config(
            method="POST",
            path="/tickets",
            server_url=f"http://{host}:{port}",
            max_attempts=3,
            request_content_types=["application/json"],
        ),
        input_schema=None,
        input_data={"body": {"title": "hello"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 503
    assert result["attempts"] == 1
    assert len(handler.received) == 1


def test_non_idempotent_method_retries_once_an_idempotency_key_is_bound(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = _send_raw(
        execution_config=_config(
            method="POST",
            path="/tickets",
            server_url=f"http://{host}:{port}",
            max_attempts=3,
            idempotency_key_header="Idempotency-Key",
            request_content_types=["application/json"],
        ),
        input_schema=None,
        input_data={"body": {"title": "hello"}, "idempotency_key": "abc-123"},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert handler.received[0]["headers"]["Idempotency-Key"] == "abc-123"
    assert handler.received[1]["headers"]["Idempotency-Key"] == "abc-123"


def test_private_range_is_blocked_by_default() -> None:
    with pytest.raises(OpenApiExecutionError):
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://10.0.0.1"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )


def test_link_local_metadata_endpoint_is_blocked_by_default() -> None:
    """The SSRF case the proposal's risk table names first."""

    with pytest.raises(OpenApiExecutionError):
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://169.254.169.254"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )


def test_oauth_client_credentials_fetches_a_token_and_caches_it(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    def responder(
        path: str, body: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if path == "/oauth/token":
            assert headers["Authorization"].startswith("Basic ")
            assert "grant_type=client_credentials" in body
            return (
                200,
                {"access_token": "oauth-token", "token_type": "Bearer", "expires_in": 3600},
                {"Content-Type": "application/json"},
            )
        return 200, {"ticket_id": "T-1", "status": "open"}, {"Content-Type": "application/json"}

    handler.responder = responder
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    config = _config(
        server_url=f"http://{host}:{port}",
        auth_binding={
            "kind": "oauth_client_credentials",
            "token_url": f"http://{host}:{port}/oauth/token",
            "client_id": "client-id",
            "client_secret_ref": "env://OPENAPI_OAUTH_SECRET",
            "scopes": ["tickets.read"],
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("OPENAPI_OAUTH_SECRET", "super-secret")
        first = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )
        second = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )

    assert first["status_code"] == 200
    assert second["status_code"] == 200
    auth_headers = [item["headers"].get("Authorization", "") for item in handler.received]
    assert auth_headers.count("Bearer oauth-token") == 2
    assert sum(1 for item in handler.received if item["path"] == "/oauth/token") == 1


def test_oauth_refresh_token_uses_refresh_grant(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    def responder(
        path: str, body: str, _headers: dict[str, str]
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if path == "/oauth/token":
            assert "grant_type=refresh_token" in body
            assert "refresh_token=refresh-me" in body
            assert "client_id=refresh-client" in body
            return (
                200,
                {"access_token": "refreshed-token", "token_type": "Bearer"},
                {"Content-Type": "application/json"},
            )
        return 200, {"ticket_id": "T-1", "status": "resolved"}, {"Content-Type": "application/json"}

    handler.responder = responder
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    config = _config(
        server_url=f"http://{host}:{port}",
        auth_binding={
            "kind": "oauth_refresh_token",
            "token_url": f"http://{host}:{port}/oauth/token",
            "client_id": "refresh-client",
            "client_auth_method": "body",
            "refresh_token_secret_ref": "env://OPENAPI_REFRESH_TOKEN",
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("OPENAPI_REFRESH_TOKEN", "refresh-me")
        result = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )

    assert result["json"]["status"] == "resolved"
    assert handler.received[-1]["headers"]["Authorization"] == "Bearer refreshed-token"


# ---------------------------------------------------------------- B04.4 (#750): the proposal-time preflight


def _preflight(
    config: dict[str, Any] | None = None,
    input_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> OpenApiPreflight:
    return preflight_governed_openapi_request(
        execution_config=_config(method="POST", idempotency_key_header="Idempotency-Key")
        if config is None
        else config,
        input_schema=kwargs.pop("input_schema", None),
        input_data={"path_params": {"ticket_id": "T-1"}} if input_data is None else input_data,
        egress_policy=kwargs.pop(
            "egress_policy", EgressPolicy(allowed_hosts=frozenset({"tickets.example.com"}))
        ),
    )


def _refused(code: str, *args: Any, **kwargs: Any) -> OpenApiPreflightError:
    with pytest.raises(OpenApiPreflightError) as raised:
        _preflight(*args, **kwargs)
    assert raised.value.code == code
    assert str(raised.value) == code, "a refusal carries its code and nothing else"
    assert code in PREFLIGHT_REFUSAL_CODES
    return raised.value


def test_preflight_describes_the_operation_without_sending(local_server) -> None:
    server, handler = local_server
    config = _config(server_url=f"http://127.0.0.1:{server.server_address[1]}", method="POST")

    checked = _preflight(
        config,
        {"path_params": {"ticket_id": "T-1"}, "body": {"a": 1}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert (checked.method, checked.path) == ("POST", "/tickets/{ticket_id}")
    assert checked.server_url == f"http://127.0.0.1:{server.server_address[1]}"
    assert checked.idempotency_key_header is None
    assert checked.expected_resource_version is None
    assert handler.received == [], "a preflight sends nothing"


def test_preflight_applies_the_egress_policy_to_the_request_url() -> None:
    config = _config(server_url="http://127.0.0.1:9", method="POST")

    _refused("openapi_egress_blocked", config, egress_policy=EgressPolicy())


def test_preflight_uses_the_bound_process_policy_when_none_is_given() -> None:
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    config = _config(server_url="http://127.0.0.1:9", method="POST")

    checked = preflight_governed_openapi_request(
        execution_config=config, input_schema=None, input_data={"path_params": {"ticket_id": "T"}}
    )
    assert checked.method == "POST"

    bind_egress_policy(None)
    with pytest.raises(OpenApiPreflightError) as raised:
        preflight_governed_openapi_request(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T"}},
        )
    assert raised.value.code == "openapi_egress_blocked"


@pytest.mark.parametrize("kind", ["oauth_client_credentials", "oauth_refresh_token"])
def test_preflight_applies_the_egress_policy_to_an_oauth_token_url(kind: str) -> None:
    config = _config(
        method="POST",
        auth_binding={"kind": kind, "token_url": "http://169.254.169.254/token"},
    )

    _refused("openapi_egress_blocked", config)


def test_preflight_resolves_no_secret_and_makes_no_token_request(local_server) -> None:
    """An unresolvable secret is the transport's failure; the preflight only checks where the call would go."""
    server, handler = local_server
    port = server.server_address[1]
    config = _config(
        server_url=f"http://127.0.0.1:{port}",
        method="POST",
        auth_binding={
            "kind": "oauth_client_credentials",
            "token_url": f"http://127.0.0.1:{port}/oauth/token",
            "client_id": "c",
            "client_secret_ref": "env://B044_SECRET_THAT_IS_NOT_SET",
        },
    )

    _preflight(config, egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    assert handler.received == []


def test_preflight_refuses_an_unsupported_auth_kind_and_a_malformed_binding() -> None:
    _refused("openapi_auth_unsupported", _config(method="POST", auth_binding={"kind": "kerberos"}))
    _refused("openapi_config_invalid", _config(method="POST", auth_binding="bearer"))


@pytest.mark.parametrize("kind", ["none", "bearer", "api_key", "basic", "header"])
def test_preflight_accepts_every_supported_auth_kind_without_resolving_it(kind: str) -> None:
    config = _config(method="POST", auth_binding={"kind": kind, "secret_ref": "env://NOT_SET"})

    assert _preflight(config).method == "POST"


@pytest.mark.parametrize(
    ("config", "code"),
    [
        (None, "openapi_config_invalid"),
        ({}, "openapi_config_invalid"),
        (_config(server_url=""), "openapi_config_invalid"),
        (_config(path="tickets"), "openapi_config_invalid"),
        (_config(server_url="http://user:pw@tickets.example.com"), "openapi_config_invalid"),
        (_config(server_url="http://tickets.example.com?token=x"), "openapi_config_invalid"),
        (_config(server_url="http://tickets.example.com#frag"), "openapi_config_invalid"),
        (_config(server_url="ftp://tickets.example.com"), "openapi_config_invalid"),
        (_config(server_url="http://tickets.example.com:notaport"), "openapi_config_invalid"),
        (_config(server_url="http://tickets.example.com\n"), "openapi_config_invalid"),
        (_config(server_url="http://"), "openapi_config_invalid"),
    ],
    ids=[
        "none",
        "empty",
        "no-server",
        "relative-path",
        "userinfo",
        "query",
        "fragment",
        "scheme",
        "port",
        "control-character",
        "no-host",
    ],
)
def test_preflight_refuses_a_configuration_it_cannot_name_in_a_resource(
    config: dict[str, Any] | None, code: str
) -> None:
    with pytest.raises(OpenApiPreflightError) as raised:
        preflight_governed_openapi_request(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
            egress_policy=EgressPolicy(allowed_hosts=frozenset({"tickets.example.com"})),
        )
    assert raised.value.code == code
    assert "pw" not in str(raised.value)


def test_preflight_refuses_input_that_breaks_the_schema_without_echoing_it() -> None:
    schema = {"type": "object", "properties": {"body": {"type": "integer"}}}

    error = _refused(
        "openapi_input_invalid",
        _config(method="POST"),
        {"path_params": {"ticket_id": "T-1"}, "body": "SECRET-VALUE-77"},
        input_schema=schema,
    )

    assert "SECRET-VALUE-77" not in str(error)


def test_preflight_refuses_a_missing_or_dot_path_parameter() -> None:
    _refused("openapi_input_invalid", _config(method="POST"), {"path_params": {}})
    _refused("openapi_input_invalid", _config(method="POST"), {"path_params": {"ticket_id": ".."}})


def test_preflight_selects_a_pack_operation_from_the_configuration() -> None:
    pack = {
        "kind": "openapi_http_pack",
        "server_url": "https://tickets.example.com",
        "auth_binding": None,
        "operations": [
            {"operation_key": "list", "method": "GET", "path": "/tickets"},
            {"operation_key": "create", "method": "POST", "path": "/tickets"},
        ],
    }

    assert openapi_operation_method(pack, {"operation": "list"}) == "GET"
    assert openapi_operation_method(pack, {"operation": "create"}) == "POST"
    for bad in ({}, {"operation": "drop_tables"}, {"operation": " "}):
        with pytest.raises(OpenApiPreflightError) as raised:
            openapi_operation_method(pack, bad)
        assert raised.value.code == "openapi_operation_unresolved"
    with pytest.raises(OpenApiPreflightError) as raised:
        openapi_operation_method({**pack, "operations": "nope"}, {"operation": "list"})
    assert raised.value.code == "openapi_operation_unresolved"
    with pytest.raises(OpenApiPreflightError) as raised:
        openapi_operation_method(None, {})
    assert raised.value.code == "openapi_config_invalid"


@pytest.mark.parametrize(
    "input_data",
    [
        {"idempotency_key": "mine"},
        {"header_params": {"Idempotency-Key": "mine"}},
        {"header_params": {"idempotency-key": "mine"}},
        {"idempotency_key": "mine", "header_params": {"Idempotency-Key": "mine"}},
    ],
    ids=["input", "header", "header-lowercase", "both-equal"],
)
def test_preflight_refuses_any_caller_supplied_key_for_an_operation_that_binds_one(
    input_data: dict[str, Any],
) -> None:
    """The platform owns the key: even an equal one is refused, never sent next to the proposal's."""
    _refused(
        "openapi_key_override_refused",
        _config(method="POST", idempotency_key_header="Idempotency-Key"),
        {"path_params": {"ticket_id": "T-1"}, **input_data},
    )


def test_preflight_ignores_a_key_input_for_an_operation_that_binds_no_header() -> None:
    checked = _preflight(
        _config(method="POST", idempotency_key_header=None),
        {"path_params": {"ticket_id": "T-1"}, "idempotency_key": "unused"},
    )

    assert checked.idempotency_key_header is None


def test_preflight_lifts_the_precondition_out_of_the_replayed_input() -> None:
    config = _config(method="PUT", expected_resource_version_header="If-Match")
    original = {
        "path_params": {"ticket_id": "T-1"},
        "header_params": {"if-match": "3", "X-Trace": "t"},
        "expected_resource_version": "3",
        "body": {"a": 1},
    }

    checked = _preflight(config, original)

    assert checked.expected_resource_version == "3"
    assert checked.expected_resource_version_header == "If-Match"
    assert checked.replay_input == {
        "path_params": {"ticket_id": "T-1"},
        "header_params": {"X-Trace": "t"},
        "body": {"a": 1},
    }
    assert original["expected_resource_version"] == "3", "the caller's input is not changed"
    assert original["header_params"] == {"if-match": "3", "X-Trace": "t"}


def test_preflight_takes_the_precondition_from_either_place_and_none_when_absent() -> None:
    config = _config(method="PUT", expected_resource_version_header="If-Match")
    base = {"path_params": {"ticket_id": "T-1"}}

    assert (
        _preflight(config, {**base, "expected_resource_version": 7}).expected_resource_version
        == "7"
    )
    assert (
        _preflight(config, {**base, "header_params": {"If-Match": "8"}}).expected_resource_version
        == "8"
    )
    assert _preflight(config, base).expected_resource_version is None


@pytest.mark.parametrize(
    "extra",
    [
        {"header_params": {"If-Match": "4"}, "expected_resource_version": "3"},
        {"header_params": {"If-Match": " "}},
        {"expected_resource_version": " "},
    ],
    ids=["disagree", "blank-header", "blank-input"],
)
def test_preflight_refuses_a_conflicting_or_blank_precondition(extra: dict[str, Any]) -> None:
    _refused(
        "openapi_precondition_conflict",
        _config(method="PUT", expected_resource_version_header="If-Match"),
        {"path_params": {"ticket_id": "T-1"}, **extra},
    )


def test_preflight_leaves_a_precondition_alone_for_an_operation_that_binds_no_header() -> None:
    checked = _preflight(
        _config(method="PUT"),
        {"path_params": {"ticket_id": "T-1"}, "expected_resource_version": "3"},
    )

    assert checked.expected_resource_version is None
    assert checked.replay_input["expected_resource_version"] == "3"


@pytest.mark.parametrize(
    "header",
    ["X-HTTP-Method-Override", "x-method-override", "X-HTTP-Method", "Transfer-Encoding", "Host"],
)
def test_the_executor_and_the_preflight_both_refuse_a_method_override_or_framing_header(
    local_server, header: str
) -> None:
    server, handler = local_server
    config = _config(server_url=f"http://127.0.0.1:{server.server_address[1]}")
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    input_data = {"path_params": {"ticket_id": "T-1"}, "header_params": {header: "DELETE"}}

    with pytest.raises(OpenApiExecutionError) as refused:
        execute_openapi_http_tool(execution_config=config, input_schema=None, input_data=input_data)
    assert refused.value.never_sent is True
    assert "DELETE" not in str(refused.value) and header not in str(refused.value)
    error = _refused("openapi_header_forbidden", config, input_data)
    assert "DELETE" not in str(error)
    assert handler.received == []


def test_an_ordinary_header_param_still_reaches_the_downstream(local_server) -> None:
    server, handler = local_server
    config = _config(server_url=f"http://127.0.0.1:{server.server_address[1]}")
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    execute_openapi_http_tool(
        execution_config=config,
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}, "header_params": {"X-Trace": "t"}},
    )

    assert handler.received[0]["headers"]["X-Trace"] == "t"


def test_the_preflight_and_the_executor_agree_on_the_url_the_request_would_use(
    local_server,
) -> None:
    """The preflight's egress check is for the URL the executor then sends to: both read the same operation."""
    server, handler = local_server
    config = _config(server_url=f"http://127.0.0.1:{server.server_address[1]}")
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    input_data = {"path_params": {"ticket_id": "T-1"}}

    checked = preflight_governed_openapi_request(
        execution_config=config, input_schema=None, input_data=input_data
    )
    result = execute_openapi_http_tool(
        execution_config=config, input_schema=None, input_data=input_data
    )

    assert result["status_code"] == 200
    assert handler.received[0]["path"] == checked.path.replace("{ticket_id}", "T-1")
