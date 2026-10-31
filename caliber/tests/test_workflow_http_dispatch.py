"""B04.2 runtime integration for governed webhook and API-request nodes."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import caliber.effects.dispatcher as dispatcher_module
import caliber.workflows.runtime as runtime_module
from caliber.db.models import CaliberEffectProposal
from caliber.effects.dispatcher import EffectOutcome, EffectRefused
from caliber.effects.receipts import ReceiptFacts
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    ToolExecutionError,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.effect_helpers import leased
from tests.test_effect_dispatcher import EVIDENCE, _approval, _validator
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database
from tests.test_workflow_api_request_node import _manifest as api_manifest
from tests.test_workflow_api_request_node import _url_node
from tests.test_workflow_webhook_node import _ALLOW_UNRESOLVABLE
from tests.test_workflow_webhook_node import _manifest as webhook_manifest


def _http_manifest(kind: str, url: str) -> dict[str, Any]:
    if kind == "webhook":
        return webhook_manifest(url)
    return api_manifest(_url_node(url=url))


def _direct_http_request() -> dict[str, Any]:
    return {
        "url": "https://example.test/direct-http",
        "method": "POST",
        "headers": {},
        "timeout_seconds": 5,
        "body": "hello",
    }


def _direct_http_plan(
    *,
    authorize: Any = None,
    receipt_verifier: Any = None,
    sender: Any = None,
) -> RuntimePlan:
    resolver = InMemoryToolResolver({})
    manifest = parse_manifest(_http_manifest("webhook", _direct_http_request()["url"]))
    if authorize is None:
        authorize = lambda _proposal: EffectAuthorization(
            approval_id="approval",
            approver_scopes=frozenset({"caliber.approver"}),
            lease=object(),
        )
    if receipt_verifier is None:
        receipt_verifier = lambda _request, _response: None
    if sender is None:
        sender = lambda _request: {"status_code": 201, "request_id": "direct"}
    session_factory = lambda: nullcontext(SimpleNamespace(commit=lambda: None))
    return RuntimePlan(
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        workflow_version_id="direct-http-version",
        webhook_sender=sender,
        egress_policy=_ALLOW_UNRESOLVABLE,
        governed_effects=GovernedEffectContext(
            session_factory=session_factory,
            workflow_run_id="RUN-direct-http",
            authorize=authorize,
            receipt_verifier=receipt_verifier,
        ),
    )


def _fake_recorded_proposal(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(proposal_id="proposal-direct-http", state="proposed")


def _run_direct_http_effect(plan: RuntimePlan) -> Any:
    return runtime_module._perform_http_effect(
        plan,
        node=plan.ir.nodes["hook"],
        node_id="hook",
        request=_direct_http_request(),
    )


@pytest.mark.parametrize("kind", ["webhook", "api_request"])
def test_manifest_http_nodes_use_proposal_dispatch_and_receipt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Both public node types share the same governed HTTP execution boundary."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _validator)
    monkeypatch.setattr(runtime_module, "approval_valid_at_commit", _validator)
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        captured: dict[str, Any] = {}

        def sender(request: dict[str, Any]) -> dict[str, Any]:
            captured.update(request)
            return {
                "status_code": 201,
                "request_id": f"{kind}-request-1",
                "text": "created",
                "json": {"created": True},
                "headers": {},
            }

        def authorize(proposal: Any) -> EffectAuthorization:
            with factory() as session:
                approval_id = _approval(session, proposal.proposal_id)
                lease = leased(session, f"runtime-{kind}")
                session.commit()
            return EffectAuthorization(
                approval_id=approval_id,
                approver_scopes=frozenset({"caliber.approver"}),
                lease=lease,
                evidence_hash=EVIDENCE,
            )

        def verify(request: Any, response: dict[str, Any]) -> ReceiptFacts:
            return ReceiptFacts(
                attempt=request.view.attempt or 1,
                idempotency_key=request.view.logical_key,
                connector=request.view.connector,
                connector_version=request.view.connector_version,
                resource=request.view.resource,
                intent_hash=request.view.intent_hash,
                downstream_request_id=response["request_id"],
                response_digest="sha256:" + "c" * 64,
            )

        resolver = InMemoryToolResolver({})
        manifest = parse_manifest(_http_manifest(kind, "https://example.test/governed-http"))
        plan = RuntimePlan(
            ir=build_ir(manifest, resolver, version="1"),
            resolver=resolver,
            workflow_version_id="manifest-version-1",
            webhook_sender=sender,
            egress_policy=_ALLOW_UNRESOLVABLE,
            governed_effects=GovernedEffectContext(
                session_factory=factory,
                workflow_run_id="RUN-ep",
                authorize=authorize,
                receipt_verifier=verify,
            ),
        )

        result = execute(plan, "hello", executor=FakeWorkflowExecutor())

        assert result.status == "completed"
        assert captured["headers"]["Idempotency-Key"]
        with factory() as session:
            proposals = list(session.execute(select(CaliberEffectProposal)).scalars())
            assert len(proposals) == 1
            assert proposals[0].connector == "http"
            assert proposals[0].state == "committed"


def test_manifest_http_node_refuses_raw_sender_without_governed_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def sender(_request: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"status_code": 201}

    resolver = InMemoryToolResolver({})
    manifest = parse_manifest(_http_manifest("webhook", "https://example.test/refused"))
    plan = RuntimePlan(
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        workflow_version_id="manifest-version-1",
        webhook_sender=sender,
        egress_policy=_ALLOW_UNRESOLVABLE,
    )

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "governed effect context" in (result.error or "")
    assert called is False


def test_legacy_http_plan_keeps_the_existing_guarded_effect_path() -> None:
    captured: dict[str, Any] = {}
    resolver = InMemoryToolResolver({})
    request = _direct_http_request()
    manifest = parse_manifest(_http_manifest("webhook", request["url"]))

    def sender(outgoing: dict[str, Any]) -> dict[str, Any]:
        captured.update(outgoing)
        return {"status_code": 200, "request_id": "legacy"}

    plan = RuntimePlan(
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        webhook_sender=sender,
        egress_policy=_ALLOW_UNRESOLVABLE,
    )
    result = _run_direct_http_effect(plan)
    assert result == {"status_code": 200, "request_id": "legacy"}
    assert captured["url"] == request["url"]


def test_governed_http_plan_rejects_incomplete_context() -> None:
    plan = _direct_http_plan(authorize="not-callable")
    with pytest.raises(ToolExecutionError, match="context is incomplete"):
        _run_direct_http_effect(plan)


def test_governed_http_plan_rejects_non_json_proposal_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_json_compatible", lambda _value: "not-a-mapping")
    with pytest.raises(ToolExecutionError, match="not JSON-compatible"):
        _run_direct_http_effect(_direct_http_plan())


def test_governed_http_plan_surfaces_proposal_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise EffectRefused("proposal_refused")

    monkeypatch.setattr(runtime_module, "submit_proposal", refuse)
    with pytest.raises(ToolExecutionError, match="proposal refused: proposal_refused"):
        _run_direct_http_effect(_direct_http_plan())


def test_governed_http_plan_surfaces_authorizer_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "submit_proposal", _fake_recorded_proposal)

    def authorize(_proposal: Any) -> None:
        raise ValueError("approval provider unavailable")

    with pytest.raises(ToolExecutionError, match="authorization failed: ValueError"):
        _run_direct_http_effect(_direct_http_plan(authorize=authorize))


def test_governed_http_plan_rejects_missing_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "submit_proposal", _fake_recorded_proposal)
    with pytest.raises(ToolExecutionError, match="authorization was not granted"):
        _run_direct_http_effect(_direct_http_plan(authorize=lambda _proposal: object()))


def test_governed_http_plan_surfaces_dispatch_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "submit_proposal", _fake_recorded_proposal)

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise EffectRefused("dispatch_refused")

    monkeypatch.setattr(runtime_module, "dispatch", refuse)
    with pytest.raises(ToolExecutionError, match="dispatch refused: dispatch_refused"):
        _run_direct_http_effect(_direct_http_plan())


def test_governed_http_plan_rejects_uncommitted_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "submit_proposal", _fake_recorded_proposal)
    monkeypatch.setattr(
        runtime_module,
        "dispatch",
        lambda *_args, **_kwargs: EffectOutcome(
            proposal_id="proposal-direct-http", state="unknown", reason="receipt_pending"
        ),
    )
    with pytest.raises(ToolExecutionError, match="did not commit: receipt_pending"):
        _run_direct_http_effect(_direct_http_plan())


def test_governed_http_plan_rejects_commit_without_transport_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "submit_proposal", _fake_recorded_proposal)
    monkeypatch.setattr(
        runtime_module,
        "dispatch",
        lambda *_args, **_kwargs: EffectOutcome(
            proposal_id="proposal-direct-http", state="committed"
        ),
    )
    with pytest.raises(ToolExecutionError, match="without a transport response"):
        _run_direct_http_effect(_direct_http_plan())


def test_governed_http_plan_refuses_a_transport_the_profile_does_not_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime caller gets its admission from the dispatcher: an unpinned ``http`` sends nothing (B12.3a, #738)."""
    import caliber.profile as profile_module

    monkeypatch.setattr(
        profile_module,
        "REFERENCE_CONNECTORS",
        tuple(t for t in profile_module.REFERENCE_CONNECTORS if t.backend_kind != "http"),
    )
    sent: list[Any] = []

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        sent.append(request)
        return {"status_code": 201}

    authorized: list[Any] = []

    def authorize(proposal: Any) -> None:
        authorized.append(proposal)

    with pytest.raises(ToolExecutionError, match="proposal refused: connector_not_admitted"):
        _run_direct_http_effect(_direct_http_plan(sender=sender, authorize=authorize))
    assert sent == [] and authorized == [], "nothing is proposed, authorized or sent"
