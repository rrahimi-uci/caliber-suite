"""B04.4b (#750): the public OpenAPI entry sends only reads; a write reaches the network only through the private raw transport.

``execute_openapi_http_tool`` used to be both the public entry and the raw transport. It now refuses any protected (non-read)
operation with ``unsupported_effect_path`` before anything is prepared or sent, and the dispatcher's private backends call
``_send_raw`` instead, so a dispatched effect can never re-enter the public entry (or the dispatcher). The tests measure at a real
loopback server (nothing sent) or at a faked HTTP client (a commit followed by a timeout), and drive the real runtime, dispatcher
and reconciler.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
import caliber.integrations.openapi.executor as executor_module
import caliber.workflows.runtime as runtime_module
from caliber.effects.reconcile import (
    CONNECTORS,
    NOT_BOUND,
    Answer,
    ask_downstream,
    commit_found,
    reconcile_one,
)
from caliber.effects.records import UNKNOWN, proposal_state, proposal_view
from caliber.integrations.openapi.executor import (
    READ_METHODS,
    UNSUPPORTED_EFFECT_PATH,
    OpenApiExecutionError,
    OpenApiUnsupportedEffectPathError,
    _send_raw,
    bind_openapi_http_tool,
    execute_openapi_http_tool,
)
from tests.effect_helpers import leased
from tests.test_effect_dispatcher import _document_request
from tests.test_openapi_governed_dispatch import (
    LOOPBACK,
    OpenApiRun,
    Server,
    _pack,
    config,
    entry_for,
    verify,
)
from tests.test_registered_tool_dispatch import SENTINEL, _world, proposals


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "_backoff_seconds", lambda _attempt: 0.0)


@pytest.fixture
def server() -> Iterator[Server]:
    with Server() as running:
        yield running


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        yield factory


def _call(execute: Any, cfg: dict[str, Any], **extra: Any) -> Any:
    return execute(
        execution_config=cfg,
        input_schema=None,
        input_data={"body": {"title": SENTINEL}, **extra},
        egress_policy=LOOPBACK,
    )


# ------------------------------------------------------------ the public entry refuses a protected method


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "Delete", "TRACE"])
def test_the_public_entry_refuses_a_protected_method_and_sends_nothing(
    method: str, server: Server
) -> None:
    cfg = config(server.url, method=method, path="/tickets/T-1", idempotency_key_header=None)

    with pytest.raises(OpenApiUnsupportedEffectPathError) as refused:
        _call(execute_openapi_http_tool, cfg)

    assert server.requests == [], "nothing is sent, not even once"
    error = refused.value
    assert isinstance(error, OpenApiExecutionError), (
        "a caller that catches the executor's error sees a refusal"
    )
    assert error.code == UNSUPPORTED_EFFECT_PATH == "unsupported_effect_path"
    assert error.never_sent is True and error.attempts == 0 and error.attempt_log == []


def test_the_refusal_is_payload_free(server: Server) -> None:
    """No method, URL, path, header, tool or input value is in the message, its repr or its arguments."""
    cfg = config(server.url, method="POST", path="/tickets/{ticket_id}")

    with pytest.raises(OpenApiUnsupportedEffectPathError) as refused:
        execute_openapi_http_tool(
            execution_config=cfg,
            input_schema=None,
            input_data={
                "path_params": {"ticket_id": SENTINEL},
                "header_params": {"X-Note": SENTINEL},
                "query_params": {"q": SENTINEL},
                "body": {"title": SENTINEL},
            },
            egress_policy=LOOPBACK,
        )

    text = " ".join([str(refused.value), repr(refused.value), repr(refused.value.args)])
    for leaked in (SENTINEL, server.url, "/tickets", "POST", "127.0.0.1", "X-Note"):
        assert leaked not in text
    assert text.count(UNSUPPORTED_EFFECT_PATH) >= 1


def test_the_refusal_comes_before_validation_and_secret_resolution(server: Server) -> None:
    """A protected call is refused on its method alone: an invalid input or an unresolvable secret never gets that far."""
    cfg = config(server.url, method="POST")
    cfg["auth_binding"] = {"kind": "bearer", "secret_ref": "env:NO_SUCH_SECRET_FOR_B04_4B"}

    with pytest.raises(OpenApiUnsupportedEffectPathError):
        execute_openapi_http_tool(
            execution_config=cfg,
            input_schema={"type": "object", "required": ["never"]},
            input_data={},
            egress_policy=LOOPBACK,
        )

    assert server.requests == []


def test_the_bound_callable_of_a_tool_refuses_a_protected_method_too(server: Server) -> None:
    """``bind_openapi_http_tool`` (model-chosen calls, exports) is the public entry with a name, not a back door."""
    tool = bind_openapi_http_tool(
        SimpleNamespace(
            local_name="open_ticket", backend_config=config(server.url), input_schema=None
        ),
        egress_policy=LOOPBACK,
    )

    with pytest.raises(OpenApiUnsupportedEffectPathError):
        tool({"body": {"title": "x"}})

    assert server.requests == []


def test_a_pack_operation_the_input_selects_is_classified_by_its_method(server: Server) -> None:
    """A pack carries a read and a write; the operation is chosen by the input, the method read from the configuration."""
    pack = _pack(server)

    listed = execute_openapi_http_tool(
        execution_config=pack,
        input_schema=None,
        input_data={"operation": "list"},
        egress_policy=LOOPBACK,
    )
    assert listed["status_code"] == 201 and [r["method"] for r in server.requests] == ["GET"]

    with pytest.raises(OpenApiUnsupportedEffectPathError):
        execute_openapi_http_tool(
            execution_config=pack,
            input_schema=None,
            input_data={"operation": "create", "body": {"summary": "x"}},
            egress_policy=LOOPBACK,
        )
    assert [r["method"] for r in server.requests] == ["GET"], "the write was not sent"


# ------------------------------------------------------------ reads keep the public path


def test_a_get_is_sent_through_the_public_entry(server: Server) -> None:
    cfg = config(server.url, method="GET", path="/tickets/T-1", idempotency_key_header=None)

    result = execute_openapi_http_tool(
        execution_config=cfg, input_schema=None, input_data={}, egress_policy=LOOPBACK
    )

    assert result["status_code"] == 201 and result["attempts"] == 1
    assert [r["method"] for r in server.requests] == ["GET"]


@pytest.mark.parametrize("method", sorted(READ_METHODS))
def test_every_read_method_is_delegated_to_the_transport_unchanged(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def raw(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"status_code": 200}

    monkeypatch.setattr(executor_module, "_send_raw", raw)
    cfg = config("http://127.0.0.1:9", method=method, path="/tickets")

    result = execute_openapi_http_tool(
        execution_config=cfg,
        input_schema={"type": "object"},
        input_data={"q": 1},
        egress_policy=LOOPBACK,
    )

    assert result == {"status_code": 200}
    assert seen == [
        {
            "execution_config": cfg,
            "input_schema": {"type": "object"},
            "input_data": {"q": 1},
            "egress_policy": LOOPBACK,
        }
    ]


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        {},
        {
            "kind": "openapi_http",
            "method": "POST",
            "path": "/x",
        },  # no server: the method cannot decide it
        {"kind": "openapi_http_pack", "server_url": "http://127.0.0.1:9", "operations": []},
    ],
)
def test_a_configuration_with_no_operation_gets_the_transports_own_refusal(
    cfg: dict[str, Any] | None,
) -> None:
    """Nothing to classify is not a protected-effect refusal: the transport refuses the configuration, sending nothing."""
    with pytest.raises(OpenApiExecutionError) as refused:
        execute_openapi_http_tool(
            execution_config=cfg, input_schema=None, input_data={}, egress_policy=LOOPBACK
        )

    assert not isinstance(refused.value, OpenApiUnsupportedEffectPathError)
    assert refused.value.never_sent is True and refused.value.attempts == 0


# ------------------------------------------------------------ the private transport still sends any method, with its safeguards


def test_the_private_transport_sends_a_protected_method_without_retrying_it_without_a_key(
    server: Server,
) -> None:
    server.script = [(503, {}), (201, {"id": "second"})]
    cfg = config(server.url, idempotency_key_header=None, max_attempts=3)

    result = _call(_send_raw, cfg)

    assert result["status_code"] == 503 and result["attempts"] == 1
    assert [r["method"] for r in server.requests] == ["POST"]


def test_the_private_transport_keeps_the_header_denylist_and_the_egress_policy(
    server: Server,
) -> None:
    cfg = config(server.url)
    with pytest.raises(OpenApiExecutionError) as forbidden:
        _send_raw(
            execution_config=cfg,
            input_schema=None,
            input_data={"header_params": {"X-HTTP-Method-Override": "DELETE"}},
            egress_policy=LOOPBACK,
        )
    with pytest.raises(OpenApiExecutionError) as blocked:
        _send_raw(
            execution_config=cfg, input_schema=None, input_data={}
        )  # the default policy blocks loopback

    assert forbidden.value.never_sent is True and blocked.value.never_sent is True
    assert server.requests == []


# ------------------------------------------------------------ the dispatcher sends through the private transport only


def test_a_governed_write_is_dispatched_once_and_never_re_enters_the_public_entry(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count what a governed POST crosses: one dispatch, one raw send, zero calls of the public entry."""
    counts = {"dispatch": 0, "public": 0, "raw": 0}
    real_dispatch, real_public, real_raw = (
        runtime_module.dispatch,
        executor_module.execute_openapi_http_tool,
        executor_module._send_raw,
    )

    def counting_dispatch(*args: Any, **kwargs: Any) -> Any:
        counts["dispatch"] += 1
        return real_dispatch(*args, **kwargs)

    def counting_public(*args: Any, **kwargs: Any) -> Any:
        counts["public"] += 1
        return real_public(*args, **kwargs)

    def counting_raw(*args: Any, **kwargs: Any) -> Any:
        counts["raw"] += 1
        return real_raw(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "dispatch", counting_dispatch)
    monkeypatch.setattr(executor_module, "execute_openapi_http_tool", counting_public)
    monkeypatch.setattr(executor_module, "_send_raw", counting_raw)
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

    result = run.execute()

    assert result.status == "completed", result.error
    assert counts == {"dispatch": 1, "public": 0, "raw": 1}
    assert [r["method"] for r in server.requests] == ["POST"]
    (proposal,) = proposals(factory)
    assert proposal.state == "committed"
    assert server.requests[0]["headers"]["idempotency-key"] == proposal.logical_key


def test_a_governed_write_never_goes_through_the_public_entry_even_when_it_refuses_writes(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the backend called the public entry, its own POST would be refused: the commit shows it does not."""

    def refuse(**_kwargs: Any) -> Any:
        raise AssertionError("the dispatcher backend called the public entry")

    monkeypatch.setattr(executor_module, "execute_openapi_http_tool", refuse)
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)))

    assert run.execute().status == "completed"
    assert len(server.requests) == 1


def test_a_read_node_stays_inline_through_the_public_entry_with_no_proposal(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    public: list[str] = []
    real = executor_module.execute_openapi_http_tool

    def counting(**kwargs: Any) -> Any:
        public.append(kwargs["execution_config"]["method"])
        return real(**kwargs)

    monkeypatch.setattr(executor_module, "execute_openapi_http_tool", counting)
    reading = entry_for(
        "get_ticket",
        "read",
        config(server.url, method="GET", path="/tickets/T-1", idempotency_key_header=None),
    )
    run = OpenApiRun(factory, reading)

    result = run.execute({"query_params": {"x": "1"}})

    assert result.status == "completed", result.error
    assert public == ["GET"] and run.authorized == [] and proposals(factory) == []
    assert [r["method"] for r in server.requests] == ["GET"]


# ------------------------------------------------------------ timeout after commit, then the reconciler and a late receipt


class _CommitsThenTimesOut:
    """A downstream that receives and commits each request, and whose answer never arrives (a read timeout)."""

    def __init__(self) -> None:
        self.committed: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, *, policy: Any, timeout: float) -> _CommitsThenTimesOut:
        return self

    def __enter__(self) -> _CommitsThenTimesOut:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.committed.append((method, url, dict(kwargs.get("headers") or {})))
        raise httpx.ReadTimeout("the answer never came")


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "idempotency_key_header": None,
            "max_attempts": 3,
        },  # no key: sent once whatever the row allows
        {"max_attempts": 1},  # a key, but a single attempt
    ],
    ids=["no-key", "one-attempt"],
)
def test_a_timeout_after_the_commit_leaves_the_effect_unknown_is_not_resent_and_is_settled_later(
    overrides: dict[str, Any], factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    downstream = _CommitsThenTimesOut()
    monkeypatch.setattr(executor_module, "build_client", downstream)
    cfg = config("http://127.0.0.1:9", **overrides)
    run = OpenApiRun(factory, entry_for("open_ticket", "write", cfg))

    result = run.execute()

    assert result.status == "error"
    assert "did not commit: ambiguous" in (result.error or "")
    assert len(downstream.committed) == 1, (
        "the answer was lost after the commit: it is not sent a second time"
    )
    (proposal,) = proposals(factory)
    assert proposal.state == "unknown"

    # the node is replayed (a retry of the run, a restart): the unknown effect is not sent again
    again = OpenApiRun(factory, entry_for("open_ticket", "write", cfg), seed=False).execute()
    assert "was already recorded as unknown; not run again" in (again.error or "")
    assert len(downstream.committed) == 1

    # the reconciler has no lookup for this connector, so it leaves the effect unknown and asks nothing
    asked: list[str] = []
    with factory() as session:
        lease = leased(session, "reconciler-1")
        session.commit()
    outcome = reconcile_one(
        factory,
        proposal.proposal_id,
        lease=lease,
        targets={("openapi-http", "PRJ-ep"): "http://127.0.0.1:9"},
        lookup=lambda _connector, _base, key: asked.append(key) or Answer(404, {}),
    )
    assert (outcome.outcome, outcome.state) == (NOT_BOUND, UNKNOWN)
    assert asked == [] and len(downstream.committed) == 1

    # an authoritative receipt that arrives later (the receipt callback, an operator) settles it: still no second send
    with factory() as session:
        view = proposal_view(session, proposal.proposal_id)
        facts = verify(SimpleNamespace(view=view), {"json": {"id": "late-1"}})
        commit_found(session, proposal.proposal_id, facts, reason="late_receipt")
        session.commit()
        assert proposal_state(session, proposal.proposal_id).state == "committed"
    assert len(downstream.committed) == 1


def test_a_delayed_lookup_of_the_reconciler_still_works_through_the_public_read_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciler asks by key with a ``GET`` through the public entry: a record that is not visible yet is a 404, then a 200."""
    stored = {"request_id": "DR-1", "idempotency_key": "key-1"}
    with Server([(404, {"detail": "not found"}), (200, stored)]) as downstream:
        monkeypatch.setattr(executor_module, "_ACTIVE_EGRESS_POLICY", LOOPBACK)
        connector = CONNECTORS["document-request"]

        first = ask_downstream(connector, downstream.url, "key-1")
        second = ask_downstream(connector, downstream.url, "key-1")

        assert (first.status_code, second.status_code) == (404, 200)
        assert second.body == stored
        assert [r["method"] for r in downstream.requests] == ["GET", "GET"]
        assert all("idempotency_key=key-1" in r["path"] for r in downstream.requests)


def test_the_document_request_backend_sends_its_post_and_its_lookup_through_the_private_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The submit is a ``POST`` and would be refused by the public entry: the backend reaches the network only via ``_send_raw``."""
    sent: list[str] = []
    stored = {
        "request_id": "downstream-1",
        "idempotency_key": "dispatch-key",
        "case_id": "CASE-1",
        "document_type": "pay_statement",
        "case_version": 8,
    }

    def raw(**kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs["execution_config"]["method"])
        return {"status_code": 201 if len(sent) == 1 else 200, "json": stored}

    def public(**_kwargs: Any) -> Any:
        raise AssertionError("the backend called the public entry")

    monkeypatch.setattr(executor_module, "_send_raw", raw)
    monkeypatch.setattr(executor_module, "execute_openapi_http_tool", public)

    result = dispatcher_module._Backend().perform(
        _document_request(), {"base_url": "https://downstream.invalid"}
    )

    assert result.verify is not None and result.verify() is not None
    assert sent == ["POST", "GET"]


def test_the_response_of_a_refused_public_call_is_json_serialisable_text() -> None:
    """The refusal text is what a tool result or an Aria reply carries; it must round-trip through JSON unchanged."""
    text = str(OpenApiUnsupportedEffectPathError())
    assert json.loads(json.dumps({"error": text}))["error"] == text
    assert text.startswith(UNSUPPORTED_EFFECT_PATH)


# ------------------------------------------------------------ classified and sent are the same snapshot


def test_what_is_classified_is_what_is_sent_even_if_the_caller_changes_the_configuration_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check-then-send gap: the configuration the caller still holds is changed to a POST right after the method was read."""
    sent: list[dict[str, Any]] = []
    cfg = config("http://127.0.0.1:9", method="GET", path="/tickets", idempotency_key_header=None)
    data: dict[str, Any] = {"query_params": {"q": "a"}}
    real = executor_module.openapi_operation_method

    def read_then_swap(execution_config: Any, input_data: Any) -> str:
        method = real(execution_config, input_data)
        cfg["method"] = "POST"  # the caller's own objects, mutated after the check
        data["query_params"]["q"] = "changed"
        return method

    monkeypatch.setattr(executor_module, "openapi_operation_method", read_then_swap)
    monkeypatch.setattr(
        executor_module, "_send_raw", lambda **kwargs: sent.append(kwargs) or {"status_code": 200}
    )

    execute_openapi_http_tool(
        execution_config=cfg, input_schema=None, input_data=data, egress_policy=LOOPBACK
    )

    assert sent[0]["execution_config"]["method"] == "GET"
    assert sent[0]["input_data"] == {"query_params": {"q": "a"}}


@pytest.mark.parametrize("cfg", [["not", "a", "mapping"], 42])
def test_a_configuration_that_is_not_a_mapping_is_refused_as_not_sent(cfg: Any) -> None:
    with pytest.raises((TypeError, ValueError)) as refused:
        execute_openapi_http_tool(
            execution_config=cfg, input_schema=None, input_data={}, egress_policy=LOOPBACK
        )

    assert refused.value.never_sent is True  # type: ignore[attr-defined]
    assert refused.value.attempts == 0  # type: ignore[attr-defined]


def test_an_input_that_cannot_be_copied_is_refused_as_not_sent() -> None:
    class NoCopy:
        def __deepcopy__(self, _memo: Any) -> Any:
            raise TypeError("no copy")

    with pytest.raises(TypeError) as refused:
        execute_openapi_http_tool(
            execution_config=config("http://127.0.0.1:9", method="GET", path="/x"),
            input_schema=None,
            input_data={"body": NoCopy()},
            egress_policy=LOOPBACK,
        )

    assert refused.value.never_sent is True  # type: ignore[attr-defined]


# ------------------------------------------------------------ a read carries no body


@pytest.mark.parametrize("method", sorted(READ_METHODS))
def test_a_read_never_carries_a_request_body(method: str) -> None:
    """``HEAD`` and ``OPTIONS`` with a body are a request-smuggling surface; the executor drops it for every read method."""
    from caliber.integrations.openapi.executor import _request_kwargs

    for body in ({"a": 1}, ["a"], "text", b"bytes"):
        kwargs = _request_kwargs(
            method=method,
            headers={},
            body=body,
            request_content_types=["application/json"],
            query_params={"q": "1"},
        )
        assert kwargs == {"headers": {}, "params": {"q": "1"}}, (method, body)


def test_a_write_still_carries_its_body() -> None:
    from caliber.integrations.openapi.executor import _request_kwargs

    kwargs = _request_kwargs(
        method="POST",
        headers={},
        body={"a": 1},
        request_content_types=["application/json"],
        query_params={},
    )
    assert kwargs["json"] == {"a": 1}


# ------------------------------------------------------------ the development-profile legacy entry (#1068)


def test_the_development_profile_sends_a_protected_call_inline_through_the_named_legacy_entry_only(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No context, development profile: the pre-dispatcher call runs, through the named legacy entry, not the public one."""
    calls = {"public": 0, "legacy": 0}
    real_legacy = runtime_module.bind_legacy_openapi_http_tool

    def refuse_public(**_kwargs: Any) -> Any:
        calls["public"] += 1
        raise AssertionError("the public entry was used for a protected call")

    def counting_legacy(*args: Any, **kwargs: Any) -> Any:
        calls["legacy"] += 1
        return real_legacy(*args, **kwargs)

    monkeypatch.setattr(executor_module, "execute_openapi_http_tool", refuse_public)
    monkeypatch.setattr(runtime_module, "bind_legacy_openapi_http_tool", counting_legacy)
    run = OpenApiRun(
        factory,
        entry_for("open_ticket", "write", config(server.url)),
        context=False,
        legacy=True,
    )

    result = run.execute()

    assert result.status == "completed", result.error
    assert calls == {"public": 0, "legacy": 1}
    assert [r["method"] for r in server.requests] == ["POST"]
    assert run.authorized == [] and proposals(factory) == []  # not the governed path


def test_the_reference_profile_never_binds_the_legacy_entry(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``legacy_guarded_effects`` is False there: a protected call with no context is refused, and nothing is sent."""
    monkeypatch.setattr(
        runtime_module,
        "bind_legacy_openapi_http_tool",
        lambda *_a, **_k: pytest.fail("the legacy entry was bound outside the development profile"),
    )
    run = OpenApiRun(
        factory, entry_for("open_ticket", "write", config(server.url)), context=False, legacy=False
    )

    result = run.execute()

    assert "requires a governed effect context" in (result.error or "")
    assert server.requests == []


def test_a_bound_context_routes_through_the_dispatcher_and_never_binds_the_legacy_entry(
    factory: sessionmaker[Session], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "bind_legacy_openapi_http_tool",
        lambda *_a, **_k: pytest.fail("the legacy entry was bound next to a governed context"),
    )
    run = OpenApiRun(factory, entry_for("open_ticket", "write", config(server.url)), legacy=True)

    assert run.execute().status == "completed"
    assert len(run.authorized) == 1 and proposals(factory)[0].state == "committed"


def test_a_model_chosen_write_stays_refused_in_the_development_profile(server: Server) -> None:
    """The legacy entry is bound only for a tool node's inline call: the agent loops bind the public entry."""
    from caliber.workflows.runtime import _bind

    binding = SimpleNamespace(
        binding_type="registered_function",
        execution_backend="openapi_http",
        backend_config=config(server.url),
        input_schema=None,
        local_name="open_ticket",
    )

    with pytest.raises(OpenApiUnsupportedEffectPathError):
        _bind(binding, None, egress_policy=LOOPBACK)({"body": {"title": "x"}})  # type: ignore[arg-type]

    assert server.requests == []


@pytest.mark.parametrize(
    ("flag", "context", "allowed"),
    [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
)
def test_the_legacy_path_is_allowed_only_for_the_flag_with_no_bound_context(
    flag: bool, context: bool, allowed: bool
) -> None:
    """One helper decides it for the HTTP, tool and OpenAPI routes and the legacy binding: never next to a context."""
    plan = SimpleNamespace(
        legacy_guarded_effects=flag, governed_effects=object() if context else None
    )

    assert runtime_module._legacy_effects_allowed(plan) is allowed  # type: ignore[arg-type]
