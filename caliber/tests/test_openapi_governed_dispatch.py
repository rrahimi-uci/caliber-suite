"""B04.4 (#750): a registered OpenAPI tool reaches the downstream only through the dispatcher when its call is protected.

A workflow ``tool`` node bound to an OpenAPI integration is classified from its **registry row** (the declared level and
the method of the operation the row's own configuration selects). A read-class ``GET`` runs inline as before; every other
call is proposed, approved and sent once by the private ``openapi_http`` backend under the execution-owner lease, with the
proposal's stable key. The tests drive the real runtime executor, the real OpenAPI executor and a real loopback HTTP
server, so every claim is measured at the downstream.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
import caliber.integrations.openapi.executor as executor_module
import caliber.workflows.runtime as runtime_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberEventOutbox,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects.connectors import openapi_http
from caliber.effects.dispatcher import BACKENDS, EffectRefused, dispatch, submit_proposal
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.registered_tools import load_registered_tool_identity
from caliber.egress import EgressPolicy
from caliber.ids import new_tool_id
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.dialect_helpers import DIALECTS
from tests.effect_helpers import leased
from tests.test_registered_tool_dispatch import (
    RUN_ID,
    SENTINEL,
    _world,
    count,
    grant,
    proposals,
    tool_manifest,
)

LOOPBACK = EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
Authorizer = Callable[[Any], EffectAuthorization | None]
#: What a scripted answer is: ``(status, payload)``, or ``None`` to drop the connection after the request was read.
Answer = tuple[int, Any] | None


class Server:
    """A real loopback HTTP server that records each request and answers from a script, then 201 ``{"id": ...}``."""

    def __init__(self, script: list[Answer] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script = list(script or [])
        self.on_request: Callable[[dict[str, Any]], None] | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": self.rfile.read(length).decode("utf-8") if length else "",
                }
                outer.requests.append(record)
                if outer.on_request is not None:
                    outer.on_request(record)
                index = len(outer.requests) - 1
                answer: Answer = (
                    outer.script[index]
                    if index < len(outer.script)
                    else (201, {"id": f"d-{index}"})
                )
                if answer is None:
                    self.close_connection = True  # the effect happened; the answer is lost
                    return
                status, payload = answer
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815

            def log_message(self, *args: Any) -> None:
                """Keep the test output clean."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> Server:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server() -> Iterator[Server]:
    with Server() as running:
        yield running


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry waits no real time: the attempts are what is measured."""
    monkeypatch.setattr(executor_module, "_backoff_seconds", lambda _attempt: 0.0)


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        yield factory


@pytest.fixture(params=DIALECTS)
def each_dialect(
    request: pytest.FixtureRequest, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    with _world(request.param, tmp_path, monkeypatch) as factory:
        yield factory


def config(base_url: str, **overrides: Any) -> dict[str, Any]:
    """The execution config of a ``POST /tickets`` operation that binds a key header."""
    base: dict[str, Any] = {
        "kind": "openapi_http",
        "method": "POST",
        "path": "/tickets",
        "server_url": base_url,
        "auth_binding": None,
        "request_content_types": ["application/json"],
        "idempotency_key_header": "Idempotency-Key",
        "max_attempts": 1,
    }
    base.update(overrides)
    return base


def entry_for(
    name: str, level: str, cfg: dict[str, Any], schema: dict[str, Any] | None = None
) -> ToolRegistryEntry:
    return ToolRegistryEntry(
        name=name,
        version="1.0",
        module_path="<openapi_http>",
        callable_name="invoke",
        execution_backend="openapi_http",
        backend_config=cfg,
        input_schema=schema,
        side_effect_level=level,
    )


def seed_row(
    factory: sessionmaker[Session],
    entry: ToolRegistryEntry,
    *,
    row_config: dict[str, Any] | None = None,
    row_backend: str | None = None,
    owner: str = "@ops",
) -> None:
    """The registry row the runtime reads: written from the entry unless a test says the row differs."""
    with factory() as session:
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name=entry.name,
                version=entry.version,
                module_path=entry.module_path,
                callable_name=entry.callable_name,
                side_effect_level=entry.side_effect_level,
                execution_backend=row_backend or entry.execution_backend,
                backend_config=entry.backend_config if row_config is None else row_config,
                input_schema=entry.input_schema,
                owner=owner,
            )
        )
        session.commit()


def verify(request: Any, response: dict[str, Any]) -> ReceiptFacts:
    """A receipt verifier that confirms the call: it reads the executor's result, as a real one reads the downstream."""
    body = response.get("json") or {}
    return ReceiptFacts(
        attempt=request.view.attempt or 1,
        idempotency_key=request.view.logical_key,
        connector=request.view.connector,
        connector_version=request.view.connector_version,
        resource=request.view.resource,
        intent_hash=request.view.intent_hash,
        downstream_request_id=str(body.get("id", "d-0")),
        response_digest="sha256:" + "d" * 64,
    )


class OpenApiRun:
    """One plan over a tool node bound to an OpenAPI tool; ``authorized`` records every proposal the runtime asked to authorize."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        entry: ToolRegistryEntry,
        *,
        seed: bool = True,
        row_config: dict[str, Any] | None = None,
        authorize: Authorizer | None = None,
        receipt_verifier: Callable[[Any, dict[str, Any]], Any] = verify,
        context: bool = True,
        lookup: bool = True,
        binding: dict[str, Any] | None = None,
        egress: EgressPolicy | None = LOOPBACK,
    ) -> None:
        self.factory = factory
        self.authorized: list[str] = []
        user_authorize = authorize
        if seed:
            seed_row(factory, entry, row_config=row_config)

        def authorize_and_record(proposal: Any) -> EffectAuthorization | None:
            self.authorized.append(proposal.proposal_id)
            if user_authorize is not None:
                return user_authorize(proposal)
            return grant(factory, proposal.proposal_id)

        resolver = InMemoryToolResolver([entry])
        manifest = parse_manifest(tool_manifest(entry.name, **(binding or {})))
        self.plan = RuntimePlan(
            ir=build_ir(manifest, resolver, version="1"),
            resolver=resolver,
            workflow_version_id="version-b04-4",
            egress_policy=egress,
            registered_tool_identity=(
                (lambda name, version: _identity(factory, name, version)) if lookup else None
            ),
            governed_effects=(
                GovernedEffectContext(
                    session_factory=factory,
                    workflow_run_id=RUN_ID,
                    authorize=authorize_and_record,
                    receipt_verifier=receipt_verifier,
                )
                if context
                else None
            ),
        )

    def execute(self, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        text = json.dumps({"body": {"summary": "disk full"}} if arguments is None else arguments)
        return execute(self.plan, text, executor=FakeWorkflowExecutor(), **kwargs)


def _identity(factory: sessionmaker[Session], name: str, version: str) -> Any:
    with factory() as session:
        return load_registered_tool_identity(session, name=name, version=version)


# ---------------------------------------------------------------- a protected call is a governed effect


def test_a_post_reaches_the_downstream_only_after_the_claim_and_with_the_stable_key(
    each_dialect: sessionmaker[Session], server: Server
) -> None:
    factory = each_dialect
    seen: dict[str, Any] = {}

    def at_request(_record: dict[str, Any]) -> None:
        with factory() as session:
            row = session.execute(select(CaliberEffectProposal)).scalar_one()
            seen["state"] = row.state
            seen["consumed"] = session.execute(
                select(CaliberRuntimeApprovalRequest.consumed_at)
            ).scalar_one()
            seen["outbox"] = session.execute(
                select(func.count()).select_from(CaliberEventOutbox)
            ).scalar_one()
        seen["key"] = _record["headers"].get("idempotency-key")

    server.on_request = at_request
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry)

    result = run.execute()

    assert result.status == "completed", result.error
    assert len(server.requests) == 1
    request = server.requests[0]
    assert (request["method"], request["path"]) == ("POST", "/tickets")
    assert json.loads(request["body"]) == {"summary": "disk full"}
    # The request left only after the claim committed: dispatched, approval consumed, outbox event written.
    assert seen["state"] == "dispatched"
    assert seen["consumed"] is not None
    assert seen["outbox"] == 1
    (proposal,) = proposals(factory)
    assert proposal.state == "committed"
    assert proposal.connector == "openapi-http"
    assert proposal.resource == f"openapi/POST {server.url}/tickets"
    assert proposal.node_id == "tool_node"
    # The platform's stable key is the idempotency key, and nobody else chose it.
    assert seen["key"] == proposal.logical_key
    # An administrator-registered tool is not in the inventory: the recorded class is the fail-closed one.
    assert proposal.effect_class == "irreversible_write"
    assert count(factory, CaliberEffectReceipt) == 1
    assert "d-0" in result.output


def test_the_proposal_records_the_row_operation_and_the_input_verbatim_but_not_the_auth_binding(
    factory: sessionmaker[Session], server: Server
) -> None:
    """What is stored is the caller's input as given (so credentials never belong in it) and never the row's auth binding."""
    cfg = config(
        server.url,
        auth_binding={
            "kind": "bearer",
            "secret_ref": "env://B044_TOKEN",
        },  # a reference, not a secret
    )
    entry = entry_for("open_ticket", "write", cfg)
    run = OpenApiRun(factory, entry)
    run.execute(
        {
            "body": {"summary": "disk full"},
            "header_params": {"X-Trace": "t-1"},
            "query_params": {"q": "a"},
        }
    )

    (proposal,) = proposals(factory)
    with factory() as session:
        recorded = dispatcher_module.read_proposal(session, proposal.proposal_id)
    assert recorded["args"] == {
        "tool": "open_ticket",
        "version": "1.0",
        "input": {
            "body": {"summary": "disk full"},
            "header_params": {"X-Trace": "t-1"},
            "query_params": {"q": "a"},
        },
    }, "the caller's header_params and query_params are stored verbatim"
    assert "B044_TOKEN" not in json.dumps(recorded)


def test_a_get_on_a_read_row_runs_inline_with_no_proposal(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(server.url, method="GET", path="/tickets/{ticket_id}", idempotency_key_header=None)
    entry = entry_for("get_ticket", "read", cfg)
    run = OpenApiRun(factory, entry)

    result = run.execute({"path_params": {"ticket_id": "T-1"}})

    assert result.status == "completed", result.error
    assert [(r["method"], r["path"]) for r in server.requests] == [("GET", "/tickets/T-1")]
    assert run.authorized == []
    assert proposals(factory) == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_row_that_under_declares_read_cannot_make_a_write_inline(
    factory: sessionmaker[Session], server: Server, method: str
) -> None:
    """The method of the row's own operation is a floor: a ``read`` level does not make a ``POST`` inline."""
    cfg = config(server.url, method=method, path="/tickets/{ticket_id}")
    entry = entry_for("open_ticket", "read", cfg)
    run = OpenApiRun(factory, entry)

    result = run.execute({"path_params": {"ticket_id": "T-1"}, "body": {}})

    assert result.status == "completed", result.error
    assert len(run.authorized) == 1
    assert [(r["method"]) for r in server.requests] == [method]
    assert proposals(factory)[0].state == "committed"


def test_a_get_on_a_write_row_is_dispatched(factory: sessionmaker[Session], server: Server) -> None:
    """The declared level also floors the class: a ``GET`` of a row declared ``write`` is still proposed."""
    cfg = config(server.url, method="GET", path="/export", idempotency_key_header=None)
    entry = entry_for("export_tickets", "write", cfg)
    run = OpenApiRun(factory, entry)

    result = run.execute({"query_params": {"since": "2026-01-01"}})

    assert result.status == "completed", result.error
    assert len(run.authorized) == 1
    assert len(server.requests) == 1
    assert server.requests[0]["headers"].get("idempotency-key") is None, (
        "no key header is bound, so no key is sent"
    )


def test_an_external_action_row_is_recorded_as_irreversible(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("send_refund", "external_action", config(server.url))
    OpenApiRun(factory, entry).execute()
    assert proposals(factory)[0].effect_class == "irreversible_write"


# ---------------------------------------------------------------- the registry row decides, never the binding


def test_a_binding_whose_configuration_differs_from_the_row_is_refused_and_sends_nothing(
    factory: sessionmaker[Session], server: Server
) -> None:
    """A manifest that claims a ``GET`` for what the row says is a ``POST`` sends nothing and records nothing."""
    claimed = config(server.url, method="GET", path="/tickets/{ticket_id}")
    row = config(server.url)
    entry = entry_for("open_ticket", "read", claimed)
    run = OpenApiRun(factory, entry, row_config=row)

    result = run.execute({"path_params": {"ticket_id": "T-1"}})

    assert result.status == "error"
    assert "does not match its registry row" in (result.error or "")
    assert server.requests == []
    assert proposals(factory) == []


def test_a_binding_with_another_server_than_the_row_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config("http://127.0.0.1:9"))
    run = OpenApiRun(factory, entry, row_config=config(server.url))

    result = run.execute()

    assert "does not match its registry row" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_a_binding_whose_input_schema_differs_from_the_row_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for(
        "open_ticket", "write", config(server.url), schema={"type": "object", "properties": {}}
    )
    seed_row(factory, replace(entry, input_schema={"type": "object", "required": ["body"]}))
    run = OpenApiRun(factory, entry, seed=False)

    result = run.execute()

    assert "does not match its registry row" in (result.error or "")
    assert server.requests == []


def test_a_row_that_is_not_an_openapi_tool_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    seed_row(factory, entry, row_backend="python_callable")
    run = OpenApiRun(factory, entry, seed=False)

    result = run.execute()

    assert "does not match its registry row" in (result.error or "")
    assert server.requests == []


def test_a_tool_with_no_readable_row_is_refused_not_sent(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry, seed=False)

    result = run.execute()

    assert result.status == "error"
    assert "refused: unsupported_effect_path" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_a_reader_that_raises_is_reported_without_its_message(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry)

    def reader(_name: str, _version: str) -> Any:
        raise ConnectionError(f"the store said {SENTINEL}")

    run.plan.registered_tool_identity = reader

    result = run.execute()

    assert "identity could not be read: ConnectionError" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert server.requests == []


def test_a_protected_call_without_a_governed_context_is_refused_before_it_is_sent(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))

    result = OpenApiRun(factory, entry, context=False).execute()

    assert "requires a governed effect context to run protected OpenAPI tool" in (
        result.error or ""
    )
    assert server.requests == [] and proposals(factory) == []


def test_a_read_call_without_a_governed_context_still_runs(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(server.url, method="GET", path="/tickets", idempotency_key_header=None)
    entry = entry_for("list_tickets", "read", cfg)

    result = OpenApiRun(factory, entry, context=False).execute({"query_params": {"q": "x"}})

    assert result.status == "completed", result.error
    assert len(server.requests) == 1


def test_a_governed_context_without_a_registry_reader_refuses_the_call(
    factory: sessionmaker[Session], server: Server
) -> None:
    """With an authority but no way to read the row there is no trusted operation: refused, not sent."""
    entry = entry_for("open_ticket", "write", config(server.url))

    result = OpenApiRun(factory, entry, lookup=False).execute()

    assert "refused: unsupported_effect_path" in (result.error or "")
    assert server.requests == []


def test_an_ungoverned_plan_keeps_calling_the_tool_directly(
    factory: sessionmaker[Session], server: Server
) -> None:
    """A plan with neither a registry reader nor a context (an export, a test plan) is unchanged (#1051)."""
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry, context=False, lookup=False)

    result = run.execute()

    assert result.status == "completed", result.error
    assert len(server.requests) == 1
    assert proposals(factory) == []


def test_a_preview_never_reaches_the_downstream(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry)

    result = run.execute(preview=True)

    assert result.status == "completed", result.error
    assert server.requests == [] and proposals(factory) == []


def test_a_protected_call_replaced_by_an_injected_callable_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry)
    calls: list[Any] = []

    result = run.execute(extra_tools={"open_ticket": lambda *a, **k: calls.append((a, k))})

    assert "replaced by an injected callable and cannot be governed" in (result.error or "")
    assert calls == [] and server.requests == [] and proposals(factory) == []


# ---------------------------------------------------------------- refusals before a proposal exists


def test_a_blocked_destination_is_refused_before_a_proposal_or_an_approval_exists(
    factory: sessionmaker[Session], server: Server
) -> None:
    """Egress is checked at proposal time: a call that can never be sent never asks for an approval."""
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry, egress=EgressPolicy())  # the default policy blocks loopback

    result = run.execute()

    assert result.status == "error"
    assert "OpenAPI effect proposal refused: openapi_egress_blocked" in (result.error or "")
    assert run.authorized == []
    assert proposals(factory) == []
    assert count(factory, CaliberRuntimeApprovalRequest) == 0
    assert server.requests == []


def test_a_blocked_oauth_token_url_is_refused_at_proposal_time(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(
        server.url,
        auth_binding={
            "kind": "oauth_client_credentials",
            "token_url": "http://169.254.169.254/latest/token",
            "client_id": "c",
            "client_secret_ref": "env://B044_CLIENT_SECRET",
        },
    )
    run = OpenApiRun(factory, entry_for("open_ticket", "write", cfg))

    result = run.execute()

    assert "openapi_egress_blocked" in (result.error or "")
    assert proposals(factory) == [] and server.requests == []


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"body": {}, "idempotency_key": "caller-chosen"}, "openapi_key_override_refused"),
        (
            {"body": {}, "header_params": {"idempotency-key": "caller-chosen"}},
            "openapi_key_override_refused",
        ),
        (
            {"body": {}, "header_params": {"Idempotency-Key": "caller-chosen"}},
            "openapi_key_override_refused",
        ),
    ],
    ids=["input-key", "header-key-lowercase", "header-key"],
)
def test_a_caller_supplied_idempotency_key_is_refused_not_sent_next_to_the_platforms(
    factory: sessionmaker[Session], server: Server, arguments: dict[str, Any], code: str
) -> None:
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

    result = run.execute(arguments)

    assert f"OpenAPI effect proposal refused: {code}" in (result.error or "")
    assert "caller-chosen" not in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_a_refusal_never_carries_the_input(factory: sessionmaker[Session], server: Server) -> None:
    """Schema failures and unresolved parameters are reported by code, never with the value that failed."""
    schema = {
        "type": "object",
        "properties": {"body": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    }
    entry = entry_for("open_ticket", "write", config(server.url), schema=schema)
    run = OpenApiRun(factory, entry)

    result = run.execute({"body": {"n": SENTINEL}})

    assert "OpenAPI effect proposal refused: openapi_input_invalid" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_a_missing_path_parameter_is_refused_before_a_proposal(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(server.url, method="DELETE", path="/tickets/{ticket_id}")
    run = OpenApiRun(factory, entry_for("close_ticket", "write", cfg))

    result = run.execute({"body": {}})

    assert "openapi_input_invalid" in (result.error or "")
    assert proposals(factory) == [] and server.requests == []


def test_empty_arguments_are_refused_as_an_inline_call_refuses_them(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

    result = run.execute({})

    assert "openapi tools accept one object argument" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


@pytest.mark.parametrize(
    "server_url",
    [
        "http://user:pw@127.0.0.1:1",
        "http://127.0.0.1:1?token=x",
        "ftp://127.0.0.1",
        "http://127.0.0.1:notaport",
    ],
    ids=["userinfo", "query", "scheme", "port"],
)
def test_a_server_that_cannot_be_named_in_a_resource_is_refused(
    factory: sessionmaker[Session], server_url: str
) -> None:
    """The server text goes into the proposal's resource: credentials or a query there would be persisted."""
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server_url)))

    result = run.execute()

    assert "openapi_config_invalid" in (result.error or "")
    assert "pw" not in (result.error or "")
    assert proposals(factory) == []


def test_an_unsupported_auth_kind_is_refused_at_proposal_time(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(server.url, auth_binding={"kind": "kerberos"})
    run = OpenApiRun(factory, entry_for("open_ticket", "write", cfg))

    result = run.execute()

    assert "openapi_auth_unsupported" in (result.error or "")
    assert proposals(factory) == [] and server.requests == []


# ---------------------------------------------------------------- If-Match is preserved


def _versioned(server: Server, **overrides: Any) -> dict[str, Any]:
    return config(
        server.url,
        method="PUT",
        path="/tickets/{ticket_id}",
        expected_resource_version_header="If-Match",
        **overrides,
    )


def test_the_precondition_is_recorded_in_the_proposal_and_sent_as_the_bound_header(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("update_ticket", "write", _versioned(server)))

    result = run.execute(
        {"path_params": {"ticket_id": "T-1"}, "body": {"s": "x"}, "expected_resource_version": "3"}
    )

    assert result.status == "completed", result.error
    assert server.requests[0]["headers"]["if-match"] == "3"
    (proposal,) = proposals(factory)
    assert proposal.expected_resource_version == "3"
    with factory() as session:
        recorded = dispatcher_module.read_proposal(session, proposal.proposal_id)
    assert "expected_resource_version" not in recorded["args"]["input"], (
        "the intent carries the precondition once; the replayed input cannot disagree with it"
    )


def test_a_header_param_precondition_is_lifted_and_a_matching_input_agrees(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("update_ticket", "write", _versioned(server)))

    result = run.execute(
        {
            "path_params": {"ticket_id": "T-1"},
            "header_params": {"if-match": "3", "X-Trace": "t"},
            "expected_resource_version": "3",
        }
    )

    assert result.status == "completed", result.error
    assert server.requests[0]["headers"]["if-match"] == "3"
    assert server.requests[0]["headers"]["x-trace"] == "t"
    assert proposals(factory)[0].expected_resource_version == "3"


def test_conflicting_preconditions_are_refused_before_a_proposal(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("update_ticket", "write", _versioned(server)))

    result = run.execute(
        {
            "path_params": {"ticket_id": "T-1"},
            "header_params": {"If-Match": "4"},
            "expected_resource_version": "3",
        }
    )

    assert "openapi_precondition_conflict" in (result.error or "")
    assert proposals(factory) == [] and server.requests == []


def test_a_proposal_that_expects_a_version_the_row_cannot_send_is_refused_not_dropped(
    server: Server,
) -> None:
    """If a recorded precondition could not be sent, the transport refuses instead of sending without it."""
    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )

    with pytest.raises(executor_module.OpenApiPreflightError) as refused:
        openapi_http.transport_request(
            operation,
            resource=f"openapi/POST {server.url}/tickets",
            args={"tool": "open_ticket", "version": "1.0", "input": {"body": {}}},
            expected_resource_version="9",
            logical_key="k" * 64,
        )

    assert refused.value.code == openapi_http.INTENT_MISMATCH
    assert server.requests == []


# ---------------------------------------------------------------- retries, no key, ambiguity


def test_an_operation_with_no_key_header_is_sent_once_even_for_a_method_the_executor_retries(
    factory: sessionmaker[Session],
) -> None:
    with Server([(503, {}), (201, {"id": "second"})]) as server:
        cfg = config(
            server.url,
            method="PUT",
            path="/tickets/T-1",
            idempotency_key_header=None,
            max_attempts=3,
        )
        run = OpenApiRun(factory, entry_for("update_ticket", "write", cfg))

        result = run.execute({"body": {"s": "x"}})

        assert result.status == "error"
        assert "OpenAPI effect did not commit: ambiguous:http_503" in (result.error or "")
        assert len(server.requests) == 1, "without an emitted key the request is not repeated"
    assert proposals(factory)[0].state == "unknown"


def test_a_retry_repeats_the_request_with_the_same_emitted_key(
    factory: sessionmaker[Session],
) -> None:
    with Server([(503, {}), (201, {"id": "second"})]) as server:
        run = OpenApiRun(
            factory, entry_for("open_ticket", "write", config(server.url, max_attempts=3))
        )

        result = run.execute()

        assert result.status == "completed", result.error
        assert len(server.requests) == 2
        keys = {r["headers"]["idempotency-key"] for r in server.requests}
        assert keys == {proposals(factory)[0].logical_key}


def test_the_connection_dropped_after_the_commit_leaves_the_effect_unknown_and_is_not_resent(
    factory: sessionmaker[Session],
) -> None:
    """The downstream received (and so may have committed) the request, but the answer never came: unknown, once."""
    with Server([None]) as server:
        cfg = config(server.url, idempotency_key_header=None, max_attempts=3)
        run = OpenApiRun(factory, entry_for("open_ticket", "write", cfg))

        result = run.execute()

        assert result.status == "error"
        assert "did not commit: ambiguous" in (result.error or "")
        assert len(server.requests) == 1
        (proposal,) = proposals(factory)
        assert proposal.state == "unknown"

        again = OpenApiRun(factory, entry_for("open_ticket", "write", cfg), seed=False).execute()

        assert "was already recorded as unknown; not run again" in (again.error or "")
        assert len(server.requests) == 1, (
            "an unknown effect is settled by lookup, never by a second send"
        )


def test_a_downstream_rejection_is_unknown_because_the_connector_documents_none(
    factory: sessionmaker[Session],
) -> None:
    """The capability promises no rejection status, so even a 4xx proves nothing about the effect."""
    with Server([(412, {"error": SENTINEL})]) as server:
        run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

        result = run.execute()

        assert "did not commit: ambiguous:unrecognised_4xx" in (result.error or "")
        assert SENTINEL not in (result.error or "")
        assert proposals(factory)[0].state == "unknown"


def test_a_retry_is_never_made_by_the_binding_for_a_governed_call(
    factory: sessionmaker[Session],
) -> None:
    with Server([(503, {})]) as server:
        cfg = config(server.url, idempotency_key_header=None, max_attempts=1)
        run = OpenApiRun(
            factory, entry_for("open_ticket", "write", cfg), binding={"max_retries": 3}
        )

        run.execute()

        assert len(server.requests) == 1


def test_a_receipt_that_cannot_be_verified_does_not_become_committed_and_is_not_resent(
    factory: sessionmaker[Session], server: Server
) -> None:
    """A delayed lookup: the downstream answered 201 but its own state does not show the request yet."""
    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry, receipt_verifier=lambda _request, _response: None)

    result = run.execute()

    assert result.status == "error"
    assert "did not commit: receipt_unverified" in (result.error or "")
    (proposal,) = proposals(factory)
    assert proposal.state == "unknown"
    assert count(factory, CaliberEffectReceipt) == 0
    assert len(server.requests) == 1

    later = OpenApiRun(factory, entry, seed=False, receipt_verifier=verify).execute()

    assert "was already recorded as unknown; not run again" in (later.error or "")
    assert len(server.requests) == 1


def test_a_verifier_that_raises_leaves_the_effect_unknown_and_it_is_not_resent(
    factory: sessionmaker[Session], server: Server
) -> None:
    def verifier(_request: Any, _response: dict[str, Any]) -> Any:
        raise ConnectionError("the lookup store went away")

    entry = entry_for("open_ticket", "write", config(server.url))
    run = OpenApiRun(factory, entry, receipt_verifier=verifier)

    result = run.execute()

    assert result.status == "error"
    assert proposals(factory)[0].state == "unknown"
    assert len(server.requests) == 1
    again = OpenApiRun(factory, entry, seed=False).execute()
    assert "was already recorded as unknown; not run again" in (again.error or "")
    assert len(server.requests) == 1


# ---------------------------------------------------------------- the boundary still holds


def test_a_write_whose_proposal_gets_no_authorization_never_reaches_the_downstream(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(
        factory,
        entry_for("open_ticket", "write", config(server.url)),
        authorize=lambda _proposal: None,
    )

    result = run.execute()

    assert "OpenAPI effect authorization was not granted" in (result.error or "")
    assert server.requests == []
    assert proposals(factory)[0].state == "proposed"


def test_a_stale_lease_never_reaches_the_downstream(
    factory: sessionmaker[Session], server: Server
) -> None:
    def authorize(proposal: Any) -> EffectAuthorization:
        stale = grant(factory, proposal.proposal_id, owner="old-owner")
        with factory() as session:
            leased(session, "new-owner")
            session.commit()
        return stale

    run = OpenApiRun(
        factory, entry_for("open_ticket", "write", config(server.url)), authorize=authorize
    )

    result = run.execute()

    assert "dispatch refused" in (result.error or "")
    assert server.requests == []


def test_replaying_a_finished_node_does_not_send_again(
    factory: sessionmaker[Session], server: Server
) -> None:
    entry = entry_for("open_ticket", "write", config(server.url))
    assert OpenApiRun(factory, entry).execute().status == "completed"

    second = OpenApiRun(factory, entry, seed=False)
    result = second.execute()

    assert "was already recorded as committed; not run again" in (result.error or "")
    assert len(server.requests) == 1
    assert second.authorized == []


def test_two_executors_of_one_node_send_once(
    each_dialect: sessionmaker[Session], server: Server, request: pytest.FixtureRequest
) -> None:
    if request.node.callspec.id == "sqlite":
        pytest.skip("the claim lock is a PostgreSQL row lock; SQLite serializes writers anyway")
    factory = each_dialect
    entry = entry_for("open_ticket", "write", config(server.url))
    seed_row(factory, entry)
    barrier = threading.Barrier(2, timeout=30)
    shared: dict[str, EffectAuthorization] = {}
    guard = threading.Lock()
    outcomes: list[Any] = []

    def authorize(proposal: Any) -> EffectAuthorization:
        with guard:
            if "approval" not in shared:
                shared["approval"] = grant(factory, proposal.proposal_id)
        barrier.wait()
        return shared["approval"]

    def one() -> None:
        outcomes.append(OpenApiRun(factory, entry, seed=False, authorize=authorize).execute())

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(result.status for result in outcomes) == ["completed", "error"]
    assert len(server.requests) == 1
    assert proposals(factory)[0].state == "committed"


# ---------------------------------------------------------------- tool packs


def _pack(server: Server) -> dict[str, Any]:
    return {
        "kind": "openapi_http_pack",
        "server_url": server.url,
        "auth_binding": None,
        "max_attempts": 1,
        "operations": [
            {
                "operation_key": "list",
                "method": "GET",
                "path": "/tickets",
                "request_content_types": [],
                "idempotency_key_header": None,
            },
            {
                "operation_key": "create",
                "method": "POST",
                "path": "/tickets",
                "request_content_types": ["application/json"],
                "idempotency_key_header": "Idempotency-Key",
            },
        ],
    }


def test_a_pack_is_classified_by_the_operation_its_row_selects(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("tickets", "read", _pack(server)))

    listed = run.execute({"operation": "list", "query_params": {"q": "a"}})
    assert listed.status == "completed", listed.error
    assert run.authorized == [] and proposals(factory) == []
    assert [r["method"] for r in server.requests] == ["GET"]

    created = run.execute({"operation": "create", "body": {"summary": "x"}})
    assert created.status == "completed", created.error
    assert len(run.authorized) == 1
    assert [r["method"] for r in server.requests] == ["GET", "POST"]
    (proposal,) = proposals(factory)
    assert proposal.resource == f"openapi/POST {server.url}/tickets"


def test_a_pack_operation_the_row_does_not_bind_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("tickets", "write", _pack(server)))

    result = run.execute({"operation": "delete_everything"})

    assert "refused: openapi_operation_unresolved" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


# ---------------------------------------------------------------- the private backend refuses what it was not given


def _dispatch_recorded(
    factory: sessionmaker[Session], proposal_id: str, backend_config: dict[str, Any]
) -> Any:
    authorization = grant(factory, proposal_id, owner="runtime-owner")
    return dispatch(
        factory,
        proposal_id,
        approval_id=authorization.approval_id,
        approver_scopes=authorization.approver_scopes,
        lease=authorization.lease,
        backend_kind="openapi_http",
        backend_config=backend_config,
        evidence_hash=authorization.evidence_hash,
        approval_validator=runtime_module.approval_valid_at_commit,
    )


def _proposed(
    factory: sessionmaker[Session], operation: openapi_http.OpenApiOperation, **fields: Any
) -> str:
    checked = openapi_http.preflight(operation, {"body": {"summary": "x"}})
    args = {**fields.pop("args", openapi_http.intent_args(operation, checked))}
    with factory() as session:
        recorded = submit_proposal(
            session,
            workflow_run_id=RUN_ID,
            node_id="n",
            occurrence_id="n",
            connector=fields.pop("connector", openapi_http.CONNECTOR),
            connector_version="1",
            resource=fields.pop("resource", openapi_http.operation_resource(checked)),
            args=args,
            admission=dispatcher_module.admit_backend("openapi_http"),
            **fields,
        )
        session.commit()
        return recorded.proposal_id


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"resource": "openapi/DELETE http://127.0.0.1:1/tickets"}, "another operation"),
        (
            {"args": {"tool": "other_tool", "version": "1.0", "input": {"body": {}}}},
            "another tool",
        ),
        ({"args": {"tool": "open_ticket", "version": "1.0", "input": "not an object"}}, "input"),
        ({"expected_resource_version": "7"}, "a precondition the row cannot send"),
    ],
    ids=["resource", "tool", "input-shape", "precondition"],
)
def test_the_backend_sends_nothing_for_a_proposal_that_is_not_its_operation(
    factory: sessionmaker[Session], server: Server, fields: dict[str, Any], why: str
) -> None:
    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )
    if "resource" in fields:
        fields = {"resource": fields["resource"].replace("127.0.0.1:1", server.url[7:])}
    proposal_id = _proposed(factory, operation, **fields)

    outcome = _dispatch_recorded(
        factory, proposal_id, {"operation": operation, "receipt_verifier": verify}
    )

    assert outcome.state == "failed", why
    assert outcome.reason == "not_sent"
    assert server.requests == []


def test_the_backend_refuses_a_proposal_for_another_connector_before_sending(
    factory: sessionmaker[Session], server: Server
) -> None:
    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )
    with pytest.raises(EffectRefused) as refused:
        _proposed(factory, operation, connector="http")
    assert refused.value.code == "connector_not_admitted"
    assert server.requests == []


@pytest.mark.parametrize(
    "backend_config",
    [
        {"receipt_verifier": verify},
        {"operation": "open_ticket", "receipt_verifier": verify},
        {"operation": {"tool": "open_ticket"}, "receipt_verifier": verify},
        {"receipt_verifier": verify, "base_url": "http://127.0.0.1:1"},
    ],
    ids=["no-operation", "operation-by-name", "operation-as-data", "unlisted-key"],
)
def test_dispatch_admits_only_a_registry_operation_object_as_its_setting(
    factory: sessionmaker[Session], server: Server, backend_config: dict[str, Any]
) -> None:
    """A name or a dict (data a manifest or a model could write) is never an operation; nothing is claimed or sent."""
    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )
    proposal_id = _proposed(factory, operation)

    with pytest.raises(EffectRefused) as refused:
        _dispatch_recorded(factory, proposal_id, backend_config)

    assert refused.value.code == "connector_config_not_admitted"
    assert server.requests == []
    assert proposals(factory)[0].state == "proposed"


def test_the_backend_refuses_when_its_own_settings_are_missing(
    factory: sessionmaker[Session], server: Server
) -> None:
    from caliber.effects.dispatcher import EffectRequest

    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )
    proposal_id = _proposed(factory, operation)
    with factory() as session:
        view = dispatcher_module.proposal_view(session, proposal_id)
        intent = dispatcher_module.read_proposal(session, proposal_id)
    backend = BACKENDS["openapi_http"].backend
    request = EffectRequest(view=view, intent=intent)

    for settings in ({}, {"operation": operation}, {"receipt_verifier": verify}):
        with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
            backend.perform(request, settings)  # type: ignore[union-attr]
        assert refused.value.code == "backend_config_missing"
    wrong = replace(view, connector="http")
    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        backend.perform(  # type: ignore[union-attr]
            EffectRequest(view=wrong, intent=intent),
            {"operation": operation, "receipt_verifier": verify},
        )
    assert refused.value.code == "connector_binding_mismatch"
    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        backend.perform(  # type: ignore[union-attr]
            EffectRequest(view=view, intent={**intent, "args": "not a mapping"}),
            {"operation": operation, "receipt_verifier": verify},
        )
    assert refused.value.code == "connector_intent_unsupported"
    assert server.requests == []


def test_a_verifier_that_returns_something_else_than_receipt_facts_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    from caliber.effects.dispatcher import EffectRequest

    operation = openapi_http.OpenApiOperation(
        "open_ticket", "1.0", config(server.url), egress_policy=LOOPBACK
    )
    proposal_id = _proposed(factory, operation)
    with factory() as session:
        view = dispatcher_module.proposal_view(session, proposal_id)
        intent = dispatcher_module.read_proposal(session, proposal_id)

    result = BACKENDS["openapi_http"].backend.perform(  # type: ignore[union-attr]
        EffectRequest(view=view, intent=intent),
        {"operation": operation, "receipt_verifier": lambda _r, _s: {"not": "facts"}},
    )

    assert result.verify is not None
    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        result.verify()
    assert refused.value.code == "receipt_verifier_invalid"


def test_the_connector_is_admitted_and_only_for_its_own_settings() -> None:
    admitted = dispatcher_module.admit_backend("openapi_http")
    assert (admitted.connector, admitted.connector_version) == ("openapi-http", "1")
    assert set(admitted.accepted_risks) == {"write_not_exactly_once", "key_retention_unconfirmed"}
    assert admitted.config_keys == ("operation", "receipt_verifier")
    assert BACKENDS["openapi_http"].capability is openapi_http.CAPABILITY
    capability = openapi_http.CAPABILITY
    assert capability.idempotency_key_header is None, "the connector promises no key behaviour"
    assert capability.lookup_by_key is False and capability.definitive_rejection_statuses == set()


def test_no_proposal_without_an_admission_names_the_openapi_connector(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session, pytest.raises(EffectRefused) as refused:
        submit_proposal(
            session,
            workflow_run_id=RUN_ID,
            node_id="n",
            occurrence_id="n",
            connector="openapi-http",
            connector_version="1",
            resource="openapi/POST http://x/y",
            args={"tool": "n"},
        )
    assert refused.value.code == "connector_not_admitted"


# ---------------------------------------------------------------- defence in depth


def test_a_row_that_looks_like_a_refused_builtin_but_is_an_openapi_tool_is_refused(
    factory: sessionmaker[Session], server: Server
) -> None:
    """Even a row that is the seed's own `read_text_file` in every field is never sent as an OpenAPI call."""
    from caliber.workflows.builtin_tools import (
        BUILTIN_TOOL_MODULE_BY_NAME,
        BUILTIN_TOOL_OWNER,
        BUILTIN_TOOL_VERSION,
    )

    entry = replace(
        entry_for("read_text_file", "read", config(server.url, method="GET")),
        version=BUILTIN_TOOL_VERSION,
        module_path=BUILTIN_TOOL_MODULE_BY_NAME["read_text_file"],
        callable_name="read_text_file",
    )
    seed_row(factory, entry, owner=BUILTIN_TOOL_OWNER)

    result = OpenApiRun(factory, entry, seed=False).execute({"query_params": {"q": "x"}})

    assert result.status == "error"
    assert "refused: unsupported_effect_path" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_an_incomplete_governed_context_is_refused_before_a_proposal(
    factory: sessionmaker[Session], server: Server
) -> None:
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))
    assert run.plan.governed_effects is not None
    run.plan.governed_effects = replace(run.plan.governed_effects, authorize=None)  # type: ignore[arg-type]

    result = run.execute()

    assert "governed OpenAPI effect context is incomplete" in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


def test_a_recorded_input_that_still_carries_a_precondition_is_not_sent(server: Server) -> None:
    """The proposal records the precondition once, outside the input; an input that carries one too is not what was approved."""
    operation = openapi_http.OpenApiOperation(
        "update_ticket", "1.0", _versioned(server), egress_policy=LOOPBACK
    )

    with pytest.raises(executor_module.OpenApiPreflightError) as refused:
        openapi_http.transport_request(
            operation,
            resource=f"openapi/PUT {server.url}/tickets/{{ticket_id}}",
            args={
                "tool": "update_ticket",
                "version": "1.0",
                "input": {"path_params": {"ticket_id": "T-1"}, "expected_resource_version": "3"},
            },
            expected_resource_version="3",
            logical_key="k" * 64,
        )

    assert refused.value.code == openapi_http.INTENT_MISMATCH
    assert server.requests == []


# ---------------------------------------------------------------- method-override and framing headers


@pytest.mark.parametrize(
    "header",
    [
        "X-HTTP-Method-Override",
        "x-http-method-override",
        "X-HTTP-Method",
        "X-Method-Override",
        "Transfer-Encoding",
        "Content-Length",
        "Host",
        "Connection",
        "Upgrade",
        " X-HTTP-Method-Override ",
    ],
)
def test_an_inline_read_cannot_carry_a_method_override_or_framing_header(
    factory: sessionmaker[Session], server: Server, header: str
) -> None:
    """A GET classified read by the row's method must not be turned into a write by a header the downstream honours."""
    cfg = config(server.url, method="GET", path="/tickets", idempotency_key_header=None)
    run = OpenApiRun(factory, entry_for("list_tickets", "read", cfg))

    result = run.execute({"header_params": {header: "DELETE"}})

    assert result.status == "error"
    assert "may not set a method-override, framing or hop-by-hop header" in (result.error or "")
    assert "DELETE" not in (result.error or "")
    assert server.requests == [] and proposals(factory) == []


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "transfer-encoding", "Host"])
def test_a_governed_write_cannot_carry_a_method_override_or_framing_header(
    factory: sessionmaker[Session], server: Server, header: str
) -> None:
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

    result = run.execute({"body": {}, "header_params": {header: "x"}})

    assert "OpenAPI effect proposal refused: openapi_header_forbidden" in (result.error or "")
    assert run.authorized == [] and proposals(factory) == [] and server.requests == []


def test_an_ordinary_header_is_still_sent_inline(
    factory: sessionmaker[Session], server: Server
) -> None:
    cfg = config(server.url, method="GET", path="/tickets", idempotency_key_header=None)
    run = OpenApiRun(factory, entry_for("list_tickets", "read", cfg))

    result = run.execute({"header_params": {"X-Trace": "t-1"}})

    assert result.status == "completed", result.error
    assert server.requests[0]["headers"]["x-trace"] == "t-1"
