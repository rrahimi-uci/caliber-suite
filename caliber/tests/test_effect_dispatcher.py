"""B04.1: proposal submission and the approved-effect dispatcher boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import caliber.effects.dispatcher as dispatcher_module
from caliber.contracts.agent_step import ProposedAction
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberEventOutbox,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    EffectRefused,
    TransportResult,
    UnsupportedEffectPath,
    admit_backend,
    dispatch,
    submit_proposal,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import (
    DISPATCHED,
    EXACT_INTENT_APPROVAL,
    FAILED,
    Intent,
    ProposalError,
    ProposalState,
    ProposalView,
    proposal_state,
    transition,
)
from caliber.observability.span_attributes import governed_span
from caliber.workflows.approval_policy import ApprovalCommit, ApprovalCommitRefused
from tests.effect_helpers import admit_fake_backend, leased
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database

AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
EVIDENCE = "sha256:" + "e" * 64
POLICY = "sha256:" + "p" * 64
#: Settings the admitted document-request connector accepts; nothing is sent in these tests.
_DOC_CONFIG = {"base_url": "http://127.0.0.1:9"}


def _submit(session: Session, *, tool_name: str = "write_workdir_file") -> str:
    recorded = submit_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="agent",
        occurrence_id="occ-1",
        action={
            "connector": "document-request",
            "connector_version": "1",
            "effect_class": "read",
            "resource": "test/resource",
            "args": {"value": "safe"},
        },
        tool_name=tool_name,
        caliber_defined=True,
        admission=admit_backend("document_request"),
    )
    session.commit()
    return recorded.proposal_id


def _submit_http(session: Session) -> str:
    recorded = submit_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="hook",
        occurrence_id="occ-http",
        connector="http",
        connector_version="1",
        effect_class_name="reversible_write",
        resource="https://api.example.test/v1/resource",
        args={
            "url": "https://api.example.test/v1/resource",
            "method": "POST",
            "headers": {"X-Test": "1"},
            "timeout_seconds": 5,
            "body": {"value": "hello"},
        },
        admission=admit_backend("http"),
    )
    session.commit()
    return recorded.proposal_id


def _approval(session: Session, proposal_id: str) -> str:
    proposal = session.get(CaliberEffectProposal, proposal_id)
    assert proposal is not None
    approval_id = "rap-dispatch"
    session.add(
        CaliberRuntimeApprovalRequest(
            runtime_approval_id=approval_id,
            workflow_run_id=proposal.workflow_run_id,
            project_id="PRJ-ep",
            node_id=proposal.node_id,
            status="approved",
            requested_at=AT.replace(tzinfo=None),
            decided_at=AT.replace(tzinfo=None),
            decided_by="approver",
            policy_snapshot={"required_role": "caliber.approver", "approval_count": 1},
            proposal_id=proposal_id,
            intent_hash=proposal.intent_hash,
            evidence_hash=EVIDENCE,
            policy_digest=POLICY,
            expected_resource_version=proposal.expected_resource_version,
            expires_at=(AT + timedelta(hours=1)).replace(tzinfo=None),
            requested_by="editor",
        )
    )
    session.commit()
    return approval_id


def _validator(
    session: Session,
    *,
    approval_id: str,
    proposal_id: str,
    **_: Any,
) -> ApprovalCommit:
    proposal = session.get(CaliberEffectProposal, proposal_id)
    assert proposal is not None
    return ApprovalCommit(
        run_id=proposal.workflow_run_id,
        approval_id=approval_id,
        proposal_id=proposal_id,
        approver_id="approver",
        intent_hash=proposal.intent_hash,
        evidence_hash=EVIDENCE,
        policy_digest=POLICY,
        expected_resource_version=proposal.expected_resource_version,
        grant_id="grant",
        decision_id="decision",
        run_identity=object(),  # The real validator supplies the typed identity.
    )


@pytest.fixture(autouse=True)
def _trusted_test_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a deterministic transaction fixture behind the canonical-validator seam.

    Production dispatch accepts only the imported ``approval_valid_at_commit``
    function.  These SQLite unit fixtures do not seed the full run-authority
    graph, so they replace that canonical symbol explicitly; the EGI scenario
    exercises the real validator against the reference profile.
    """
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _validator)


class _Backend:
    def __init__(self, factory: Any, proposal_id: str) -> None:
        self.factory = factory
        self.proposal_id = proposal_id
        self.called = False
        self.saw_committed_claim = False

    def perform(self, request: Any, _config: Any) -> TransportResult:
        self.called = True
        with self.factory() as session:
            state = proposal_state(session, self.proposal_id)
            self.saw_committed_claim = state.state == DISPATCHED
            assert (
                session.execute(
                    select(CaliberRuntimeApprovalRequest.consumed_at).where(
                        CaliberRuntimeApprovalRequest.runtime_approval_id == "rap-dispatch"
                    )
                ).scalar_one()
                is not None
            )
            assert session.execute(
                select(CaliberEventOutbox.event_id).where(
                    CaliberEventOutbox.aggregate_id == self.proposal_id
                )
            ).scalar_one()
        view = request.view
        facts = ReceiptFacts(
            attempt=view.attempt or 1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id="downstream-1",
            response_digest="sha256:" + "a" * 64,
        )
        return TransportResult(
            Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1),
            lambda: facts,
        )


class _Idle:
    """A transport that is only admitted, for tests that submit and never dispatch."""

    def perform(self, _request: Any, _config: Any) -> TransportResult:
        pytest.fail("an idle transport must not be called")


def test_submit_proposal_uses_inventory_effect_class(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
        with factory() as session:
            proposal = session.get(CaliberEffectProposal, proposal_id)
            assert proposal is not None
            assert proposal.effect_class == "reversible_write"
            assert proposal.state == "proposed"


@pytest.mark.parametrize(
    "case",
    ["missing_validator", "missing_lease", "unknown_backend", "no_approval", "wrong_state"],
)
def test_refuses_required_dispatch_preconditions(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()
        kwargs: dict[str, Any] = {
            "approval_id": approval_id,
            "approver_scopes": {"caliber.approver"},
            "lease": lease,
            "backend_kind": "document_request",
            "backend_config": _DOC_CONFIG,
            "approval_validator": _validator,
        }
        if case == "missing_validator":
            kwargs["approval_validator"] = None
        elif case == "missing_lease":
            kwargs["lease"] = None
        elif case == "unknown_backend":
            kwargs["backend_kind"] = "not-registered"
        elif case == "no_approval":
            kwargs["approval_id"] = None
        elif case == "wrong_state":
            with factory() as session:
                transition(
                    session,
                    proposal_id,
                    FAILED,
                    expected_version=proposal_state(session, proposal_id).version,
                    reason="test",
                )
                session.commit()
        with pytest.raises((EffectRefused, UnsupportedEffectPath)):
            dispatch(factory, proposal_id, **kwargs)
        with factory() as session:
            state = proposal_state(session, proposal_id)
            assert state.state in {"proposed", "failed"}


def test_valid_path_commits_with_receipt_and_calls_backend_after_claim_commit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()
        backend = _Backend(factory, proposal_id)
        admit_fake_backend(monkeypatch, "test", backend)
        result = dispatch(
            factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="test",
            approval_validator=_validator,
            now=AT,
        )
        assert result.state == "committed"
        assert result.outcome is TransportOutcome.COMMITTED_RECEIPT
        assert result.transport_outcome is result.outcome
        assert result.receipt_id is not None
        assert backend.called and backend.saw_committed_claim
        with factory() as session:
            state = proposal_state(session, proposal_id)
            approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
            assert state.state == "committed"
            assert state.approval_kind == EXACT_INTENT_APPROVAL
            assert approval is not None and approval.consumed_at == AT.replace(tzinfo=None)
            assert session.scalar(select(CaliberEffectReceipt.receipt_id)) == result.receipt_id


def test_unregistered_backend_kind_is_unsupported_without_claiming(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
        with pytest.raises(UnsupportedEffectPath) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="unknown",
                approval_validator=_validator,
            )
        assert refused.value.code == "unsupported_backend"
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"


def _document_request(
    *,
    connector: str = "document-request",
    connector_version: str = "1",
    resource: str = "cases/CASE-1/document-requests",
    expected_resource_version: str | None = "7",
    document_type: str = "pay_statement",
) -> dispatcher_module.EffectRequest:
    intent = Intent(
        connector=connector,
        connector_version=connector_version,
        effect_class="irreversible_write",
        resource=resource,
        args={"document_type": document_type},
        expected_resource_version=expected_resource_version,
    )
    return dispatcher_module.EffectRequest(
        view=ProposalView(
            proposal_id="prop-backend",
            logical_key="dispatch-key",
            connector=intent.connector,
            connector_version=intent.connector_version,
            effect_class=intent.effect_class,
            resource=intent.resource,
            expected_resource_version=intent.expected_resource_version,
            intent_hash=intent.digest,
            attempt=1,
            state=DISPATCHED,
        ),
        intent={"args": {"document_type": document_type}},
    )


def _http_request() -> dispatcher_module.EffectRequest:
    intent = Intent(
        connector="http",
        connector_version="1",
        effect_class="irreversible_write",
        resource="https://api.example.test/v1/resource",
        args={
            "url": "https://api.example.test/v1/resource",
            "method": "POST",
            "headers": {"X-Test": "1"},
            "timeout_seconds": 5,
            "body": {"value": "hello"},
        },
        expected_resource_version=None,
    )
    return dispatcher_module.EffectRequest(
        view=ProposalView(
            proposal_id="prop-http",
            logical_key="dispatch-http-key",
            connector=intent.connector,
            connector_version=intent.connector_version,
            effect_class=intent.effect_class,
            resource=intent.resource,
            expected_resource_version=None,
            intent_hash=intent.digest,
            attempt=1,
            state=DISPATCHED,
        ),
        intent={"args": intent.plain_args},
    )


def test_http_backend_requires_receipt_verifier_before_transport() -> None:
    called = False

    def sender(_request: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"status_code": 201}

    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._HttpBackend().perform(_http_request(), {"sender": sender})
    assert refused.value.code == "backend_config_missing"
    assert refused.value.never_sent is True
    assert called is False


def test_http_backend_rejects_connector_binding_before_sender() -> None:
    request = replace(
        _http_request(),
        view=replace(_http_request().view, connector="other-connector"),
    )
    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._HttpBackend().perform(
            request,
            {"sender": lambda _request: {"status_code": 201}, "receipt_verifier": lambda *_: None},
        )
    assert refused.value.code == "connector_binding_mismatch"
    assert refused.value.never_sent is True


def test_http_backend_rejects_intent_without_matching_url() -> None:
    request = replace(_http_request(), intent={"args": {"method": "POST"}})
    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._HttpBackend().perform(
            request,
            {"sender": lambda _request: {"status_code": 201}, "receipt_verifier": lambda *_: None},
        )
    assert refused.value.code == "connector_intent_unsupported"


def test_http_backend_defaults_headers_and_preserves_ambiguous_response() -> None:
    original = _http_request()
    args = dict(original.intent["args"])
    args["headers"] = None
    request = replace(original, intent={"args": args})
    captured: dict[str, Any] = {}

    def sender(outgoing: dict[str, Any]) -> dict[str, Any]:
        captured.update(outgoing)
        return {"status_code": 202, "text": "accepted"}

    result = dispatcher_module._HttpBackend().perform(
        request, {"sender": sender, "receipt_verifier": lambda *_: None}
    )
    assert captured["headers"] == {"Idempotency-Key": request.view.logical_key}
    assert result.outcome.outcome is TransportOutcome.AMBIGUOUS
    assert result.response == {"status_code": 202, "text": "accepted"}


def test_http_backend_rejects_non_mapping_headers() -> None:
    original = _http_request()
    args = dict(original.intent["args"])
    args["headers"] = "not-a-header-map"
    request = replace(original, intent={"args": args})
    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._HttpBackend().perform(
            request,
            {"sender": lambda _request: {"status_code": 201}, "receipt_verifier": lambda *_: None},
        )
    assert refused.value.code == "connector_intent_unsupported"


def test_http_backend_classifies_sender_exception_without_claiming_commit() -> None:
    def sender(_request: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("downstream unavailable")

    result = dispatcher_module._HttpBackend().perform(
        _http_request(), {"sender": sender, "receipt_verifier": lambda *_: None}
    )
    assert result.outcome.outcome is TransportOutcome.AMBIGUOUS
    assert result.outcome.code == "unclassified_error"


def test_http_backend_rejects_malformed_transport_result() -> None:
    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._HttpBackend().perform(
            _http_request(),
            {
                "sender": lambda _request: {"status_code": "201"},
                "receipt_verifier": lambda *_: None,
            },
        )
    assert refused.value.code == "transport_result_invalid"


def test_http_backend_rejects_untyped_receipt_verifier_result() -> None:
    result = dispatcher_module._HttpBackend().perform(
        _http_request(),
        {
            "sender": lambda _request: {"status_code": 201},
            "receipt_verifier": lambda *_: {"not": "receipt-facts"},
        },
    )
    assert result.verify is not None
    with pytest.raises(RuntimeError) as refused:
        result.verify()
    assert refused.value.code == "receipt_verifier_invalid"


def test_registered_http_backend_dispatches_private_transport_and_returns_response(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit_http(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "http-dispatcher")
            session.commit()

        captured: dict[str, Any] = {}

        def sender(request: dict[str, Any]) -> dict[str, Any]:
            captured.update(request)
            return {
                "status_code": 201,
                "request_id": "http-request-1",
                "json": {"accepted": True},
                "text": "accepted",
                "headers": {},
            }

        def verify(request: Any, response: dict[str, Any]) -> ReceiptFacts:
            assert response["request_id"] == "http-request-1"
            return ReceiptFacts(
                attempt=request.view.attempt or 1,
                idempotency_key=request.view.logical_key,
                connector=request.view.connector,
                connector_version=request.view.connector_version,
                resource=request.view.resource,
                intent_hash=request.view.intent_hash,
                downstream_request_id=response["request_id"],
                response_digest="sha256:" + "b" * 64,
            )

        result = dispatch(
            factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="http",
            backend_config={"sender": sender, "receipt_verifier": verify},
            approval_validator=_validator,
            now=AT,
        )

        assert result.state == "committed"
        assert result.response == {
            "status_code": 201,
            "request_id": "http-request-1",
            "json": {"accepted": True},
            "text": "accepted",
            "headers": {},
        }
        with factory() as session:
            proposal = session.get(CaliberEffectProposal, proposal_id)
            assert proposal is not None
            assert proposal_state(session, proposal_id).state == "committed"
            assert captured["headers"]["Idempotency-Key"] == proposal.logical_key


@pytest.mark.parametrize(
    ("effect_request", "config", "code"),
    [
        (_document_request(), {}, "backend_config_missing"),
        (
            _document_request(connector="other-connector"),
            {"base_url": "https://downstream.invalid"},
            "connector_binding_mismatch",
        ),
        (
            _document_request(connector_version="2"),
            {"base_url": "https://downstream.invalid"},
            "connector_binding_mismatch",
        ),
        (
            _document_request(resource="not-a-document-request"),
            {"base_url": "https://downstream.invalid"},
            "connector_intent_unsupported",
        ),
        (
            _document_request(expected_resource_version="v7"),
            {"base_url": "https://downstream.invalid"},
            "connector_version_invalid",
        ),
        (
            _document_request(),
            {"base_url": "https://downstream.invalid", "max_attempts": "many"},
            "backend_config_invalid",
        ),
    ],
)
def test_document_request_backend_refuses_invalid_config_before_transport(
    effect_request: dispatcher_module.EffectRequest,
    config: dict[str, Any],
    code: str,
) -> None:
    with pytest.raises(RuntimeError) as refused:
        dispatcher_module._Backend().perform(effect_request, config)
    assert refused.value.code == code
    assert refused.value.never_sent is True


def test_document_request_backend_classifies_transport_and_verifies_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {"status_code": 201, "json": {"request_id": "ignored"}},
        {
            "status_code": 200,
            "json": {
                "request_id": "downstream-1",
                "idempotency_key": "dispatch-key",
                "case_id": "CASE-1",
                "document_type": "pay_statement",
                "case_version": 8,
            },
        },
    ]
    calls: list[dict[str, Any]] = []

    def execute(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(
        dispatcher_module._raw_connector_transport,
        "execute_openapi_http_tool",
        execute,
    )
    result = dispatcher_module._Backend().perform(
        _document_request(),
        {"base_url": "https://downstream.invalid", "timeout_seconds": 2.5},
    )
    assert result.outcome.outcome is TransportOutcome.COMMITTED_RECEIPT
    assert result.verify is not None
    facts = result.verify()
    assert facts is not None and facts.downstream_request_id == "downstream-1"
    assert calls[0]["execution_config"]["method"] == "POST"
    assert calls[1]["execution_config"]["method"] == "GET"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status_code": 412, "json": {}}, TransportOutcome.DEFINITIVE_REJECTION),
        (RuntimeError("transport failed"), TransportOutcome.AMBIGUOUS),
    ],
)
def test_document_request_backend_never_upgrades_transport_evidence(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any] | RuntimeError,
    expected: TransportOutcome,
) -> None:
    def execute(**_: Any) -> dict[str, Any]:
        if isinstance(response, RuntimeError):
            raise response
        return response

    monkeypatch.setattr(
        dispatcher_module._raw_connector_transport,
        "execute_openapi_http_tool",
        execute,
    )
    result = dispatcher_module._Backend().perform(
        _document_request(), {"base_url": "https://downstream.invalid"}
    )
    assert result.outcome.outcome is expected


def test_document_request_backend_supports_unversioned_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher_module._raw_connector_transport,
        "execute_openapi_http_tool",
        lambda **_kwargs: {"status_code": 412, "json": {}},
    )
    result = dispatcher_module._Backend().perform(
        _document_request(expected_resource_version=None),
        {"base_url": "https://downstream.invalid"},
    )
    assert result.outcome.outcome is TransportOutcome.DEFINITIVE_REJECTION


def test_backend_call_accepts_callable_and_rejects_untyped_result() -> None:
    request = _document_request()
    classified = Classified(TransportOutcome.PROVEN_NOT_SENT, "not_sent", attempts=0)
    result = TransportResult(classified)
    assert (
        dispatcher_module._backend_call(lambda _request, _config: result, request, None) is result
    )
    with pytest.raises(TypeError, match="TransportResult"):
        dispatcher_module._backend_call(lambda _request, _config: object(), request, None)


def test_submit_proposal_accepts_each_intent_form_and_uses_fail_closed_class(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = admit_fake_backend(monkeypatch, "idle", _Idle(), connector="test-connector")
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            action = ProposedAction(
                connector="test-connector",
                connector_version="1",
                effect_class="read",
                resource="test/action",
                args={"value": "action"},
            )
            action_record = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-action",
                action=action,
                admission=admission,
            )
            intent_record = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-intent",
                intent=Intent(
                    connector="test-connector",
                    connector_version="1",
                    effect_class="read",
                    resource="test/intent",
                    args={"value": "intent"},
                ),
                admission=admission,
            )
            explicit_record = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-explicit",
                connector="test-connector",
                connector_version="1",
                effect_class_name="read",
                resource="test/explicit",
                args={"value": "explicit"},
                admission=admission,
            )
            session.commit()
        with factory() as session:
            assert {
                session.get(CaliberEffectProposal, proposal_id).effect_class
                for proposal_id in (
                    action_record.proposal_id,
                    intent_record.proposal_id,
                    explicit_record.proposal_id,
                )
            } == {"irreversible_write"}


def test_submit_proposal_refuses_conflicts_inventory_and_bad_action(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = admit_fake_backend(monkeypatch, "idle", _Idle(), connector="test-connector")
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        action = {
            "connector": "test-connector",
            "connector_version": "1",
            "effect_class": "read",
            "resource": "test/resource",
            "args": {},
        }
        with factory() as session:
            with pytest.raises(EffectRefused, match="conflicting_proposed_actions"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-conflict",
                    action=action,
                    proposed_action=action,
                    admission=admission,
                )
            with pytest.raises(EffectRefused, match="conflicting_intent_inputs"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-conflict-intent",
                    action=action,
                    intent=Intent(
                        connector="test-connector",
                        connector_version="1",
                        effect_class="read",
                        resource="test/other",
                        args={},
                    ),
                    admission=admission,
                )
            with pytest.raises(EffectRefused, match="conflicting_intent_inputs"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-conflicting-explicit",
                    action=action,
                    resource="test/different",
                    admission=admission,
                )
            with pytest.raises(EffectRefused, match="invalid_proposed_action"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-invalid",
                    action=object(),  # type: ignore[arg-type]
                    admission=admission,
                )
            with pytest.raises(UnsupportedEffectPath, match="unsupported_effect_path"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-refused",
                    action=action,
                    tool_name="sandbox_python",
                    caliber_defined=True,
                    admission=admission,
                )
            with pytest.raises(EffectRefused, match="invalid_effect_class"):
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-class",
                    action={**action, "effect_class": "not-a-class"},
                    admission=admission,
                )


def test_dispatch_rejects_untrusted_validator_without_claim(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        def validator(_session: Session, **_kwargs: Any) -> object:
            raise AssertionError("an untrusted validator must never be called")

        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=validator,
            )
        assert refused.value.code == "approval_validator_untrusted"
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("approval", "expired"),
        ("proposal", "proposal_corrupt"),
        ("generic", "approval_validator_failed"),
        ("invalid", "approval_validator_invalid"),
    ],
)
def test_dispatch_maps_validator_failures_to_stable_refusals(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        def validator(session: Session, **kwargs: Any) -> Any:
            if failure == "approval":
                raise ApprovalCommitRefused("expired")
            if failure == "proposal":
                raise ProposalError("proposal_corrupt", "proposal is corrupt")
            if failure == "generic":
                raise RuntimeError("validator crashed")
            return object()

        monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", validator)
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=validator,
            )
        assert refused.value.code == expected
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"


def test_dispatch_refuses_a_backend_registration_that_does_not_match_intent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()
        admit_fake_backend(
            monkeypatch,
            "mismatch",
            lambda _request, _config: pytest.fail("transport called"),
            connector="other",
        )
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="mismatch",
                approval_validator=_validator,
            )
        assert refused.value.code == "backend_intent_mismatch"
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"


def test_dispatch_refuses_a_consumed_approval_after_validation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        original_transition = dispatcher_module.transition

        def consume_after_approval(
            session: Session, proposal_id: str, to_state: str, **kwargs: Any
        ) -> Any:
            result = original_transition(session, proposal_id, to_state, **kwargs)
            if to_state == "approved":
                approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
                assert approval is not None
                approval.consumed_at = AT.replace(tzinfo=None)
            return result

        monkeypatch.setattr(dispatcher_module, "transition", consume_after_approval)
        monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _validator)
        with pytest.raises(EffectRefused, match="approval_already_consumed"):
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=_validator,
            )


def test_dispatch_refuses_when_the_proposal_run_is_missing_after_claim(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        original_reader = dispatcher_module.read_proposal

        def missing_run(session: Session, proposal_id: str) -> dict[str, Any]:
            record = dict(original_reader(session, proposal_id))
            record["workflow_run_id"] = "run-that-does-not-exist"
            return record

        monkeypatch.setattr(dispatcher_module, "read_proposal", missing_run)
        with pytest.raises(EffectRefused, match="run_not_found"):
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=_validator,
            )


def test_dispatch_maps_settlement_proposal_error_to_public_refusal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        class Backend:
            def perform(self, _request: Any, _config: Any) -> TransportResult:
                return TransportResult(
                    Classified(TransportOutcome.PROVEN_NOT_SENT, "not-sent", 0, 0)
                )

        admit_fake_backend(monkeypatch, "settle-error", Backend())

        def refuse_settlement(*_args: Any, **_kwargs: Any) -> Any:
            raise ProposalError("settlement_corrupt", "settlement is corrupt")

        monkeypatch.setattr(dispatcher_module, "settle_dispatch", refuse_settlement)
        with pytest.raises(EffectRefused, match="settlement_corrupt"):
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="settle-error",
                approval_validator=_validator,
            )


def test_dispatch_rejects_consumed_approval_without_claim(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
            assert approval is not None
            approval.consumed_at = AT.replace(tzinfo=None)
            session.commit()
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=_validator,
            )
        assert refused.value.code == "approval_refused"


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        (TransportOutcome.PROVEN_NOT_SENT, "failed"),
        (TransportOutcome.DEFINITIVE_REJECTION, "failed"),
        (TransportOutcome.AMBIGUOUS, "unknown"),
    ],
)
def test_dispatch_settles_noncommitted_transport_evidence(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: TransportOutcome,
    expected_state: str,
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        class Backend:
            def perform(self, _request: Any, _config: Any) -> TransportResult:
                return TransportResult(Classified(outcome, "test", 412, 1))

        admit_fake_backend(monkeypatch, "outcome", Backend())
        result = dispatch(
            factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="outcome",
            approval_validator=_validator,
            now=AT,
        )
        assert result.state == expected_state


def test_claim_returns_existing_in_flight_proposal_without_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProposalState(
        proposal_id="prop-in-flight",
        state=DISPATCHED,
        version=2,
        attempt=1,
        owner_id="dispatcher",
        dispatch_token=1,
        reason=None,
        approval_kind=EXACT_INTENT_APPROVAL,
        approval_ref="approval",
        receipt_id=None,
        dispatched_at=AT.replace(tzinfo=None),
        unknown_at=None,
        settled_at=None,
    )

    class Scope:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: Any) -> bool:
            return False

    monkeypatch.setattr(dispatcher_module, "_session_scope", lambda _source: Scope())
    monkeypatch.setattr(dispatcher_module, "proposal_state", lambda _session, _id: state)
    with governed_span("effect.dispatch") as span:
        result = dispatcher_module._claim_dispatch(
            object(),
            state.proposal_id,
            approval_id="approval",
            approver_scopes=set(),
            lease=object(),  # type: ignore[arg-type]
            admitted=admit_backend("document_request"),
            current_resource_version=None,
            evidence_hash=None,
            approval_validator=dispatcher_module.approval_valid_at_commit,
            now=AT,
            span=span,
        )
    assert isinstance(result, dispatcher_module.EffectOutcome)
    assert result.applied is False and result.reason == "already_dispatched"
    # The proposal is named (the state read found it); the caller's unvalidated approval id is not.
    assert span.attributes["caliber.proposal_id"] == "prop-in-flight"
    assert "caliber.approval_id" not in span.attributes


def test_dispatch_returns_existing_claim_without_calling_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = dispatcher_module.EffectOutcome(
        "prop-in-flight", DISPATCHED, reason="already_dispatched", applied=False
    )
    monkeypatch.setattr(dispatcher_module, "_claim_dispatch", lambda *_args, **_kwargs: expected)
    result = dispatch(
        object(),
        expected.proposal_id,
        approval_id="approval",
        approver_scopes=set(),
        lease=object(),  # type: ignore[arg-type]
        backend_kind="document_request",
        backend_config=_DOC_CONFIG,
        approval_validator=dispatcher_module.approval_valid_at_commit,
    )
    assert result is expected


def test_session_scope_and_naive_utc_cover_direct_session_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            with dispatcher_module._session_scope(session) as entered:
                assert entered is session
            with pytest.raises(RuntimeError, match="rollback"):
                with dispatcher_module._session_scope(session):
                    raise RuntimeError("rollback")
            assert dispatcher_module._naive_utc(datetime(2026, 3, 4, 5, 6, 7)) == datetime(
                2026, 3, 4, 5, 6, 7
            )


def test_submit_proposal_maps_recording_error_to_public_refusal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = admit_fake_backend(monkeypatch, "idle", _Idle(), connector="test-connector")
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            with pytest.raises(EffectRefused) as refused:
                submit_proposal(
                    session,
                    workflow_run_id="missing-run",
                    node_id="agent",
                    occurrence_id="occ-missing-run",
                    action={
                        "connector": "test-connector",
                        "connector_version": "1",
                        "effect_class": "read",
                        "resource": "test/resource",
                        "args": {},
                    },
                    admission=admission,
                )
            assert refused.value.code == "run_not_found"


def test_claim_rejects_untrusted_validator_before_transaction(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        def validator(_session: Session, **_kwargs: Any) -> ApprovalCommit:
            raise AssertionError("an untrusted validator must never be called")

        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="document_request",
                backend_config=_DOC_CONFIG,
                approval_validator=validator,
            )
        assert refused.value.code == "approval_validator_untrusted"


def test_dispatch_classifies_backend_exception_as_ambiguous(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _submit(session)
            approval_id = _approval(session, proposal_id)
            lease = leased(session, "dispatcher")
            session.commit()

        class Backend:
            def perform(self, _request: Any, _config: Any) -> TransportResult:
                raise RuntimeError("transport failed")

        admit_fake_backend(monkeypatch, "raises", Backend())
        result = dispatch(
            factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="raises",
            approval_validator=_validator,
            now=AT,
        )
        assert result.state == "unknown"
