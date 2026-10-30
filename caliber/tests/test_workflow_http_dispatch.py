"""B04.2 runtime integration for governed webhook and API-request nodes."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

import caliber.effects.dispatcher as dispatcher_module
import caliber.workflows.runtime as runtime_module
from caliber.db.models import CaliberEffectProposal
from caliber.effects.receipts import ReceiptFacts
from caliber.egress import EgressPolicy
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.effect_helpers import leased
from tests.test_effect_dispatcher import EVIDENCE, _approval, _validator
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database
from tests.test_workflow_api_request_node import _manifest as api_manifest
from tests.test_workflow_api_request_node import _url_node
from tests.test_workflow_webhook_node import _manifest as webhook_manifest
from tests.test_workflow_webhook_node import _ALLOW_UNRESOLVABLE


def _http_manifest(kind: str, url: str) -> dict[str, Any]:
    if kind == "webhook":
        return webhook_manifest(url)
    return api_manifest(_url_node(url=url))


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
        manifest = parse_manifest(
            _http_manifest(kind, "https://example.test/governed-http")
        )
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
