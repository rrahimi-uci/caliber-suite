"""Workflow runs must persist the MLflow *trace* id, not just the run id.

From the repository review (``ui-complete-report.md`` §3, "Trace-link
correctness defect"): :class:`WorkflowRunResult` carries both
``mlflow_run_id`` and ``mlflow_trace_id`` and the runtime populates both, but
neither the queued worker nor the synchronous run route ever assigned
``CaliberWorkflowRun.trace_id``. Two shipped surfaces read that column:

* ``GET /workflow-runs/{id}/trace`` — the in-app span viewer, which
  short-circuited to an empty tree for every run; and
* ``GET /workflow-runs/by-trace/{trace_id}`` — trace → run localization,
  which could never resolve a workflow run.

These tests pin the persistence for both execution paths and assert the two
dependent endpoints actually resolve.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.effects.dispatcher as dispatcher_module
from caliber.audit import configure_redactor
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    BACKENDS,
    BackendRegistration,
    EffectRefused,
    TransportResult,
    dispatch,
)
from caliber.effects.receipts import record_receipt
from caliber.effects.records import POLICY_DECISION, ProposalError, proposal_state
from caliber.observability.mlflow_tracing import Tracer, set_tracer
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.redaction import build_redactor
from caliber.workflows.approval_policy import ApprovalCommit, ApprovalCommitRefused
from caliber.workflows.runtime import NodeStep, WorkflowRunResult
from tests.effect_helpers import leased
from tests.test_effect_dispatcher import AT, _approval, _submit, _validator
from tests.test_effect_dispatcher import _Backend as _VerifiedBackend
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _facts, _proposal
from tests.test_effect_records import _decision
from tests.test_event_outbox import _database
from tests.test_mlflow_tracing import FakeMlflow, FakeSpan
from tests.workflow_helpers import PREFIX, create_and_publish, make_manifest

TRACE_ID = "tr-abc123"
MLFLOW_RUN_ID = "MR-abc123"


def _traced_result() -> WorkflowRunResult:
    return WorkflowRunResult(
        status="completed",
        output="done",
        steps=[NodeStep("agent", "agent", "ok", output="done")],
        mlflow_run_id=MLFLOW_RUN_ID,
        mlflow_trace_id=TRACE_ID,
    )


def _enable_queue(client: TestClient) -> None:
    """The async run queue is opt-in; the queued-path tests need it on."""
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


def _build_worker(client: TestClient) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def test_queued_worker_persists_the_trace_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_queue(client)
    workflow_id = "trace-linkage-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Trace Linkage",
        manifest=make_manifest(workflow_id),
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.mlflow_run_id == MLFLOW_RUN_ID
        # The defect: this was None, so the trace panel rendered empty.
        assert run.trace_id == TRACE_ID


def test_trace_endpoint_resolves_after_a_queued_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/workflow-runs/{id}/trace`` reports the id instead of a null tree."""
    _enable_queue(client)
    workflow_id = "trace-endpoint-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Trace Endpoint",
        manifest=make_manifest(workflow_id),
    )
    run_id = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    ).json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    resp = client.get(f"{PREFIX}/workflow-runs/{run_id}/trace")

    assert resp.status_code == 200, resp.text
    # MLflow is absent under test so the span list is empty, but the run is now
    # *linked* — previously trace_id was None and the route short-circuited
    # without ever attempting a lookup.
    assert resp.json()["data"]["trace_id"] == TRACE_ID


def test_by_trace_lookup_finds_the_queued_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/workflow-runs/by-trace/{trace_id}`` was unresolvable for every run."""
    _enable_queue(client)
    workflow_id = "by-trace-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="By Trace",
        manifest=make_manifest(workflow_id),
    )
    run_id = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    ).json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    resp = client.get(f"{PREFIX}/workflow-runs/by-trace/{TRACE_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["workflow_run_id"] == run_id


def test_synchronous_run_route_persists_the_trace_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-queued path had the same gap and must stay in step."""
    workflow_id = "sync-trace-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Sync Trace",
        manifest=make_manifest(workflow_id),
    )

    monkeypatch.setattr(
        "caliber.routes.workflow_versions.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    resp = client.post(f"{PREFIX}/workflow-versions/{version_id}/run", json={"input": "hello"})
    assert resp.status_code in (200, 201), resp.text

    with client.app.state.session_factory() as session:
        run = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_version_id == version_id)
            .one()
        )
        assert run.mlflow_run_id == MLFLOW_RUN_ID
        assert run.trace_id == TRACE_ID


# ---------------------------------------------------------------------------
# B11.3a (#737): the governed effect's spans name the persisted rows that authorised it
# ---------------------------------------------------------------------------

CAUSAL_KEYS = (
    "caliber.proposal_id",
    "caliber.decision_id",
    "caliber.approval_id",
    "caliber.effect_id",
)
SECRET = "ssn 123-45-6789 token=abc123 jane@example.com"


@pytest.fixture
def fake() -> Iterator[FakeMlflow]:
    recorder = FakeMlflow()
    set_tracer(Tracer(mlflow_module=recorder))
    yield recorder
    set_tracer(None)


@pytest.fixture(scope="module")
def _migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One database migrated to head, copied per test: the migration is the slow part, not the assertions."""
    directory = tmp_path_factory.mktemp("span_attributes_template")
    with pytest.MonkeyPatch.context() as patch, _database("sqlite", directory, patch):
        pass
    return directory / "event_outbox.db"


@pytest.fixture
def factory(_migrated_database: Path, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    copy = tmp_path / "span_attributes.db"
    shutil.copyfile(_migrated_database, copy)
    engine = create_engine(f"sqlite:///{copy}")
    try:
        session_factory = sessionmaker(engine, expire_on_commit=False)
        _seed_run(session_factory)
        yield session_factory
    finally:
        engine.dispose()


@dataclass(frozen=True)
class Armed:
    """One proposal with its approval request, a persisted policy-decision row and a captured lease."""

    proposal_id: str
    approval_id: str
    decision_id: str
    lease: Any


@pytest.fixture
def armed(factory: sessionmaker[Session]) -> Armed:
    with factory() as session:
        proposal_id = _submit(session)
        approval_id = _approval(session, proposal_id)
        decision_id = _decision(session, proposal_id, decision_id=f"pd_{proposal_id[-12:]}")
        lease = leased(session, "dispatcher")
        session.commit()
    return Armed(proposal_id, approval_id, decision_id, lease)


def _only(fake: FakeMlflow, name: str) -> FakeSpan:
    matching = [span for span in fake.spans if span.name == name]
    assert len(matching) == 1, [span.name for span in fake.spans]
    return matching[0]


def _named(fake: FakeMlflow, name: str) -> list[FakeSpan]:
    return [span for span in fake.spans if span.name == name]


def _causal(span: FakeSpan) -> dict[str, Any]:
    return {key: span.attributes[key] for key in CAUSAL_KEYS if key in span.attributes}


def _trust_row_decision(monkeypatch: pytest.MonkeyPatch, decision_id: str) -> Any:
    """The B06.3 validator seam, answering with the id of a *persisted* policy-decision row."""

    def validator(session: Session, *, approval_id: str, proposal_id: str, **kwargs: Any) -> Any:
        commit = _validator(session, approval_id=approval_id, proposal_id=proposal_id, **kwargs)
        return replace(commit, decision_id=decision_id)

    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", validator)
    return validator


def _register(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setitem(BACKENDS, "test", BackendRegistration(backend, "document-request", "1"))


def _dispatch(factory: Any, case: Armed, validator: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "approval_id": case.approval_id,
        "approver_scopes": {"caliber.approver"},
        "lease": case.lease,
        "backend_kind": "test",
        "approval_validator": validator,
        "now": AT,
    }
    arguments.update(overrides)
    return dispatch(factory, case.proposal_id, **arguments)


def test_governed_step_spans_carry_decision_ids(
    factory: sessionmaker[Session],
    armed: Armed,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMlflow,
) -> None:
    """The acceptance oracle: a dispatched proposal's spans carry all four ids, read back from the rows."""
    validator = _trust_row_decision(monkeypatch, armed.decision_id)
    _register(monkeypatch, _VerifiedBackend(factory, armed.proposal_id))

    outcome = _dispatch(factory, armed, validator)

    assert outcome.state == "committed"
    with factory() as session:
        proposal = session.scalars(select(CaliberEffectProposal)).one()
        approval = session.scalars(select(CaliberRuntimeApprovalRequest)).one()
        decision = session.scalars(select(CaliberPolicyDecision)).one()
        receipt = session.scalars(select(CaliberEffectReceipt)).one()
        assert approval.consumed_at is not None
        assert proposal.receipt_id == receipt.receipt_id
        rows = {
            "caliber.proposal_id": proposal.proposal_id,
            "caliber.decision_id": decision.decision_id,
            "caliber.approval_id": approval.runtime_approval_id,
            "caliber.effect_id": receipt.receipt_id,
        }

    dispatched = _only(fake, "effect.dispatch")
    assert _causal(dispatched) == rows
    assert dispatched.attributes["caliber.status"] == "completed"
    assert dispatched.span_type == "TOOL"

    written = _only(fake, "effect.receipt.write")
    # The receipt write knows what the proposal row records: its exact-intent approval and its own receipt.  The
    # decision id is not stored on the proposal, so this span does not have it (a missing link is not invented).
    assert _causal(written) == {
        key: rows[key]
        for key in ("caliber.proposal_id", "caliber.approval_id", "caliber.effect_id")
    }
    # Only the allow-listed ids and the tracer's own status: no payload, URL, argument or message.
    allowed = {*CAUSAL_KEYS, "caliber.status"}
    assert set(dispatched.attributes) <= allowed
    assert set(written.attributes) <= allowed


def test_a_refused_dispatch_names_only_what_was_read_and_never_a_message(
    factory: sessionmaker[Session],
    armed: Armed,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMlflow,
) -> None:
    def refusing(session: Session, **_: Any) -> ApprovalCommit:
        raise ApprovalCommitRefused("stale_version", SECRET)

    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", refusing)
    _register(monkeypatch, _VerifiedBackend(factory, armed.proposal_id))

    with pytest.raises(EffectRefused) as refused:
        _dispatch(factory, armed, refusing, approval_id="RAP-forged00")

    assert refused.value.code == "stale_version"
    span = _only(fake, "effect.dispatch")
    # The proposal came from a row; the caller's approval id was never validated, so it is not a link.
    assert _causal(span) == {"caliber.proposal_id": armed.proposal_id}
    assert span.attributes["caliber.refusal_code"] == "stale_version"
    assert span.attributes["caliber.status"] == "failed"
    for leaked in ("RAP-forged00", "123-45-6789", "abc123", "jane@example.com"):
        assert leaked not in repr(span.attributes)
    # No exception reached MLflow, so its exception event cannot carry the message either.
    assert fake.span_exits == [("effect.dispatch", None)]
    with factory() as session:
        assert proposal_state(session, armed.proposal_id).state == "proposed"
        assert session.scalars(select(CaliberEffectReceipt)).all() == []
        assert session.scalars(select(CaliberRuntimeApprovalRequest)).one().consumed_at is None


@pytest.mark.parametrize(
    ("outcome", "verify", "expected_state"),
    [
        (TransportOutcome.AMBIGUOUS, None, "unknown"),
        (TransportOutcome.COMMITTED_RECEIPT, lambda: None, "unknown"),
        (TransportOutcome.DEFINITIVE_REJECTION, None, "failed"),
    ],
    ids=["ambiguous", "unverifiable-2xx", "rejected"],
)
def test_without_a_verified_receipt_there_is_no_effect_id(
    factory: sessionmaker[Session],
    armed: Armed,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMlflow,
    outcome: TransportOutcome,
    verify: Any,
    expected_state: str,
) -> None:
    validator = _trust_row_decision(monkeypatch, armed.decision_id)

    class Backend:
        def perform(self, _request: Any, _config: Any) -> TransportResult:
            return TransportResult(Classified(outcome, "test", 502, 1), verify)

    _register(monkeypatch, Backend())
    result = _dispatch(factory, armed, validator)

    assert result.state == expected_state and result.receipt_id is None
    span = _only(fake, "effect.dispatch")
    assert _causal(span) == {
        "caliber.proposal_id": armed.proposal_id,
        "caliber.approval_id": armed.approval_id,
        "caliber.decision_id": armed.decision_id,
    }
    assert not _named(fake, "effect.receipt.write")
    with factory() as session:
        assert session.scalars(select(CaliberEffectReceipt)).all() == []


def test_a_lost_lease_leaves_no_effect_id_and_no_receipt(
    factory: sessionmaker[Session],
    armed: Armed,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMlflow,
) -> None:
    validator = _trust_row_decision(monkeypatch, armed.decision_id)
    verified = _VerifiedBackend(factory, armed.proposal_id)

    class TakenOver:
        def perform(self, request: Any, config: Any) -> TransportResult:
            result = verified.perform(request, config)
            with factory() as session:
                leased(session, "another-owner")  # a takeover: this worker's lease is now stale
                session.commit()
            return result

    _register(monkeypatch, TakenOver())
    with pytest.raises(EffectRefused) as refused:
        _dispatch(factory, armed, validator)

    assert refused.value.code == "lease_lost"
    span = _only(fake, "effect.dispatch")
    assert _causal(span) == {
        "caliber.proposal_id": armed.proposal_id,
        "caliber.approval_id": armed.approval_id,
        "caliber.decision_id": armed.decision_id,
    }
    assert span.attributes["caliber.refusal_code"] == "lease_lost"
    assert span.attributes["caliber.status"] == "failed"
    assert not _named(fake, "effect.receipt.write")
    with factory() as session:
        assert session.scalars(select(CaliberEffectReceipt)).all() == []
        assert proposal_state(session, armed.proposal_id).state == "dispatched"


def test_receipt_write_span_names_the_policy_decision_a_proposal_was_approved_by(
    factory: sessionmaker[Session], fake: FakeMlflow
) -> None:
    with factory() as session:
        proposal_id = _proposal(session)
        state = proposal_state(session, proposal_id)
        assert state.approval_kind == POLICY_DECISION
        facts = _facts(session, proposal_id)

        first = record_receipt(session, proposal_id, facts, now=AT)
        again = record_receipt(session, proposal_id, facts, now=AT)

        assert first.created and not again.created and first.receipt_id == again.receipt_id
        receipt_id = session.scalars(select(CaliberEffectReceipt.receipt_id)).one()
    assert receipt_id == first.receipt_id
    spans = _named(fake, "effect.receipt.write")
    assert len(spans) == 2
    for span in spans:
        assert _causal(span) == {
            "caliber.proposal_id": proposal_id,
            "caliber.decision_id": state.approval_ref,
            "caliber.effect_id": receipt_id,
        }


def test_receipt_write_refusals_are_coded_and_unknown_proposals_are_not_linked(
    factory: sessionmaker[Session], fake: FakeMlflow
) -> None:
    with factory() as session:
        proposal_id = _proposal(session)
        facts = _facts(session, proposal_id)
        with pytest.raises(KeyError):
            record_receipt(session, "prop-" + SECRET.replace(" ", "-"), facts, now=AT)
        with pytest.raises(ProposalError) as bad_facts:
            record_receipt(session, proposal_id, object(), now=AT)  # type: ignore[arg-type]
        assert bad_facts.value.code == "bad_field"
        session.rollback()
    unknown, malformed = _named(fake, "effect.receipt.write")
    assert _causal(unknown) == {}
    assert unknown.attributes["caliber.error_type"] == "KeyError"
    assert "caliber.refusal_code" not in unknown.attributes
    assert "123-45-6789" not in repr(unknown.attributes)
    assert _causal(malformed) == {}
    assert malformed.attributes["caliber.refusal_code"] == "bad_field"


class _RaisingRedactor:
    def redact_value(self, _value: object) -> object:
        raise RuntimeError("redactor down")


@pytest.fixture
def failing_redactor() -> Iterator[None]:
    configure_redactor(_RaisingRedactor())  # type: ignore[arg-type]
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))


def test_a_failing_redactor_never_fails_the_effect_it_traces(
    factory: sessionmaker[Session],
    armed: Armed,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMlflow,
    failing_redactor: None,
) -> None:
    """The bind after the settlement commit is the last thing dispatch does: it cannot undo a committed effect."""
    validator = _trust_row_decision(monkeypatch, armed.decision_id)
    _register(monkeypatch, _VerifiedBackend(factory, armed.proposal_id))

    outcome = _dispatch(factory, armed, validator)

    assert outcome.state == "committed" and outcome.receipt_id is not None
    with factory() as session:
        assert session.scalars(select(CaliberEffectReceipt.receipt_id)).one() == outcome.receipt_id
        assert proposal_state(session, armed.proposal_id).state == "committed"
    for span in fake.spans:
        assert not [key for key in span.attributes if key.startswith("caliber.")], span.name
    assert {span.name for span in fake.spans} == {"effect.dispatch", "effect.receipt.write"}


def test_a_failing_redactor_does_not_fail_a_receipt_write(
    factory: sessionmaker[Session], fake: FakeMlflow, failing_redactor: None
) -> None:
    with factory() as session:
        proposal_id = _proposal(session)
        recorded = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
        assert recorded.created
    (span,) = _named(fake, "effect.receipt.write")
    assert _causal(span) == {}
