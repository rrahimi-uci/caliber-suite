"""#1051: the worker builds the governed contexts from the server's own state, and the runtime consumes them.

``caliber.effects.governed_context`` is the producer; ``RuntimePlan.governed_effects`` and ``governed_agent_steps`` are what the
interpreter reads. SQLite throughout (the exact-intent chain on PostgreSQL, killed processes included, is
``tests/egi_scenarios/test_b05_governed_worker_wiring.py``). The checks that matter most here are the negative ones: nothing a request,
a manifest or a model supplies becomes authority, a missing trusted input is a refusal and never an implicit allow, and a plan the
operator did not configure is exactly what it was.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
import caliber.orchestrator.workflow_run_worker as worker_module
import caliber.workflows.runtime as runtime_module
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects import governed_context
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import EffectOutcome, EffectRefused, TransportResult
from caliber.effects.governed_context import (
    GovernedContextUnavailable,
    RunAuthority,
    configured_tool_backends,
    effect_context,
    effect_evidence_hash,
    governed_agent_nodes,
    mapped_agent_nodes,
    run_authority,
    step_context,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.reconcile import UnknownEffectError
from caliber.effects.records import LEASE_LOST, Intent, ProposalError, record_proposal, transition
from caliber.effects.step_proposal import (
    EffectApprovalPending,
    PriorProposal,
    registry_tool_classifier,
)
from caliber.effects.tool_backends import AGENT_STEP_BACKENDS, parse_tool_backends
from caliber.ownership import Lease
from caliber.workflows.agent_step import StepCeilings
from caliber.workflows.approval_policy import ApprovalCommit
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import IRAgent
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    RuntimeResumeCheckpoint,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.effect_helpers import leased
from tests.test_governed_agent_step import (  # noqa: F401 - the module's fixtures and helpers
    PROTECTED,
    PROTECTED_ID,
    RUN,
    Canaries,
    _action,
    _inline_manifest,
    _manifest,
    _migrated_template,
    _parallel_manifest,
    _plan,
    _proposing,
    _rows,
    _run,
)
from tests.test_workflow_api_request_node import _manifest as api_manifest
from tests.test_workflow_api_request_node import _url_node
from tests.test_workflow_http_dispatch import _http_manifest
from tests.test_workflow_webhook_node import _ALLOW_UNRESOLVABLE

pytestmark = pytest.mark.usefixtures("fixed_step_clock")


@pytest.fixture
def world(_migrated_template: Path, tmp_path: Path) -> Iterator[sessionmaker[Session]]:  # noqa: F811
    """A migrated SQLite database with a run (``RUN``) and the tool registry seeded: a copy of the template, per test."""
    database = tmp_path / "governed.db"
    shutil.copyfile(_migrated_template, database)
    engine = create_engine(f"sqlite:///{database}")
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


DIGEST = "a" * 64
EVIDENCE = "sha256:" + "e" * 64
INITIATOR = "editor-1"
APPROVER = "approver-1"
SECOND_APPROVER = "approver-2"
CASE_RESOURCE = "cases/MORT-1/document-requests"


# ------------------------------------------------------------------------------------------- the settings


def test_the_tool_backend_map_is_keyed_by_registry_identity_and_names_only_a_backend_with_a_lookup() -> (
    None
):
    mapping = parse_tool_backends(
        '[{"tool": "request_missing_document", "version": "1.0", "backend": "document_request"}]'
    )
    assert dict(mapping) == {("request_missing_document", "1.0"): "document_request"}
    assert parse_tool_backends("[]") == {}
    assert {"document_request"} == AGENT_STEP_BACKENDS


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '[{"tool": "t", "version": "1.0"}]',
        '[{"tool": "t", "version": "1.0", "backend": "document_request", "extra": 1}]',
        '[{"tool": "t", "version": "1.0", "backend": "http"}]',
        '[{"tool": "t", "version": "1.0", "backend": "registered_tool"}]',
        '[{"tool": "t", "version": "1.0", "backend": "openapi_http"}]',
        '[{"tool": "bad name", "version": "1.0", "backend": "document_request"}]',
        '[{"tool": "t", "version": "", "backend": "document_request"}]',
        '[{"tool": 1, "version": "1.0", "backend": "document_request"}]',
        '["t"]',
        '[{"tool": "t", "version": "1.0", "backend": "document_request"},'
        ' {"tool": "t", "version": "1.0", "backend": "document_request"}]',
    ],
)
def test_a_tool_backend_map_that_is_not_exactly_the_documented_shape_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_tool_backends(raw)
    with pytest.raises(ValueError):
        CaliberConfig(agent_step_tool_backends=raw)


def test_the_configuration_reads_the_tool_backend_map_from_its_environment_variable() -> None:
    raw = '[{"tool": "t", "version": "2", "backend": "document_request"}]'
    config = CaliberConfig.load(environ={"CALIBER_AGENT_STEP_TOOL_BACKENDS": raw})
    assert dict(configured_tool_backends(config)) == {("t", "2"): "document_request"}
    assert dict(configured_tool_backends(CaliberConfig())) == {}


def test_a_development_process_that_maps_tools_says_it_ignores_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from caliber.server import _require_reference_profile  # noqa: PLC0415

    mapped = json.dumps([{"tool": "t", "version": "1", "backend": "document_request"}])
    with caplog.at_level("WARNING"):
        _require_reference_profile(CaliberConfig(agent_step_tool_backends=mapped))
    assert any(
        "CALIBER_AGENT_STEP_TOOL_BACKENDS maps tools" in record.getMessage()
        for record in caplog.records
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        _require_reference_profile(CaliberConfig())
    assert not any("CALIBER_AGENT_STEP_TOOL_BACKENDS" in r.getMessage() for r in caplog.records)


def test_a_configuration_whose_map_cannot_be_read_is_unavailable_not_empty() -> None:
    broken = SimpleNamespace(agent_step_tool_backends="not json")
    with pytest.raises(GovernedContextUnavailable) as caught:
        configured_tool_backends(broken)  # type: ignore[arg-type]
    assert caught.value.code == "configuration_invalid"


# ------------------------------------------------------------------------------------------- the authority


def _config(**changes: Any) -> CaliberConfig:
    fields: dict[str, Any] = {"approver_users": f"{APPROVER},{SECOND_APPROVER}"}
    fields.update(changes)
    return CaliberConfig(**fields)


def _run_row(factory: Callable[[], Any], *, created_by: Any = INITIATOR) -> CaliberWorkflowRun:
    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        run.created_by = created_by
        session.commit()
        session.refresh(run)
        session.expunge(run)
        return run


class Holder:
    """A lease provider whose answer a test changes."""

    def __init__(self, lease: Lease | None) -> None:
        self.lease = lease

    def __call__(self) -> Lease | None:
        return self.lease


def _authority(
    factory: Callable[[], Any],
    holder: Holder | None = None,
    *,
    config: CaliberConfig | None = None,
    digest: str = DIGEST,
    created_by: Any = INITIATOR,
) -> RunAuthority:
    if holder is None:
        with factory() as session:
            lease = leased(session, "owner-1")
            session.commit()
        holder = Holder(lease)
    return run_authority(
        session_factory=factory,
        run=_run_row(factory, created_by=created_by),
        config=config or _config(),
        lease_provider=holder,
        run_manifest_digest=digest,
    )


def _record(
    factory: Callable[[], Any],
    *,
    connector: str = "document-request",
    version: str | None = None,
    occurrence: str = "occ-1",
    node: str = "agent",
) -> Any:
    with factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id=RUN,
            node_id=node,
            occurrence_id=occurrence,
            intent=Intent(
                connector=connector,
                connector_version="1",
                effect_class="irreversible_write",
                resource=CASE_RESOURCE if connector == "document-request" else "https://x.test/a",
                args={"document_type": "pay_statement"},
                expected_resource_version=version,
            ),
        )
        session.commit()
        return recorded


def _decide(
    factory: Callable[[], Any],
    request_id: str,
    *,
    status: str = "approved",
    decided_by: str | None = APPROVER,
    also: tuple[str, ...] = (),
) -> None:
    with factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        row.status = status
        row.decided_by = decided_by
        row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if also:
            row.policy_snapshot = {**(row.policy_snapshot or {}), "approvals": [decided_by, *also]}
        session.commit()


def _requests(factory: Callable[[], Any]) -> list[CaliberRuntimeApprovalRequest]:
    with factory() as session:
        return list(session.execute(select(CaliberRuntimeApprovalRequest)).scalars())


def test_an_undecided_proposal_is_asked_about_once_and_the_node_is_told_to_wait(world: Any) -> None:
    authority = _authority(world)
    recorded = _record(world)

    for _ in range(2):
        with pytest.raises(EffectApprovalPending) as pending:
            authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
        assert pending.value.proposal_id == recorded.proposal_id
        assert pending.value.intent_hash == recorded.intent_hash

    (request,) = _requests(world)
    assert pending.value.approval_request_id == request.runtime_approval_id
    assert (request.status, request.proposal_id, request.requested_by) == (
        "pending",
        recorded.proposal_id,
        INITIATOR,
    ), "the requester is the run's initiator, read from the run row"
    assert request.policy_snapshot["required_role"] == "caliber.approver"
    assert request.policy_snapshot["allow_self_approval"] is False


def test_the_pending_signal_is_given_even_when_no_lease_is_held(world: Any) -> None:
    """The park needs no lease (nothing is dispatched); only an approved proposal does."""
    authority = _authority(world, Holder(None))
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    assert len(_requests(world)) == 1


def test_an_approved_proposal_yields_the_exact_authorization_and_nothing_a_request_supplied(
    world: Any,
) -> None:
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    holder = Holder(lease)
    authority = _authority(world, holder)
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash="sha256:" + "e" * 64)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id)

    authorization = authority.authorize(recorded.proposal_id, evidence_hash="sha256:" + "e" * 64)

    assert authorization.approval_id == request.runtime_approval_id
    assert authorization.lease is lease, (
        "the lease the execution owner holds now, read from the provider"
    )
    assert authorization.approver_scopes == {"caliber.approver", "caliber.viewer"}, (
        "derived now from the configured user lists for the user who decided"
    )
    assert authorization.evidence_hash == "sha256:" + "e" * 64
    assert authorization.current_resource_version is None

    holder.lease = None
    with pytest.raises(EffectRefused) as no_lease:
        authority.authorize(recorded.proposal_id, evidence_hash="sha256:" + "e" * 64)
    assert no_lease.value.code == "lease_unavailable"


def test_the_scopes_attested_are_those_every_approver_of_the_quorum_holds_now(world: Any) -> None:
    authority = _authority(world, config=_config(approver_users=APPROVER))
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id, decided_by=APPROVER, also=(SECOND_APPROVER,))

    authorization = authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)

    assert "caliber.approver" not in authorization.approver_scopes, (
        "the second approver holds no approver scope, so the quorum is only as strong as that approver"
    )


def test_an_approver_who_is_no_longer_configured_attests_no_scope(world: Any) -> None:
    authority = _authority(world, config=_config(approver_users=""))
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id)

    assert authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE).approver_scopes == {
        "caliber.viewer"
    }


def test_a_rejected_request_is_final(world: Any) -> None:
    authority = _authority(world)
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id, status="rejected")

    with pytest.raises(EffectRefused) as refused:
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    assert refused.value.code == "approval_rejected"
    assert len(_requests(world)) == 1, "nobody is asked a second time"


def test_an_approval_that_expired_before_dispatch_is_asked_again_not_used(world: Any) -> None:
    authority = replace(
        _authority(world), approval_ttl_seconds=1
    )  # the shortest window a request may have
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id)
    time.sleep(1.2)

    with pytest.raises(EffectApprovalPending) as again:
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)

    assert again.value.approval_request_id != request.runtime_approval_id
    assert sorted(item.status for item in _requests(world)) == ["pending", "superseded"]


def test_a_proposal_that_names_a_version_is_sent_only_where_the_downstream_enforces_it(
    world: Any,
) -> None:
    authority = _authority(world)
    versioned = _record(world, version="7")
    with pytest.raises(EffectApprovalPending):
        authority.authorize(versioned.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id)

    authorization = authority.authorize(versioned.proposal_id, evidence_hash=EVIDENCE)
    assert authorization.current_resource_version == "7", (
        "document-request: the downstream's precondition is atomic with the write, so a stale version is its 412"
    )


def test_a_versioned_proposal_for_a_connector_that_cannot_enforce_it_is_refused(world: Any) -> None:
    authority = _authority(world)
    versioned = _record(world, connector="http", version="7", node="hook")
    with pytest.raises(EffectApprovalPending):
        authority.authorize(versioned.proposal_id, evidence_hash=EVIDENCE)
    (request,) = _requests(world)
    _decide(world, request.runtime_approval_id)

    with pytest.raises(EffectRefused) as refused:
        authority.authorize(versioned.proposal_id, evidence_hash=EVIDENCE)
    assert refused.value.code == "version_unverifiable"


def test_the_evidence_an_approver_is_shown_binds_the_run_the_node_occurrence_and_the_intent(
    world: Any,
) -> None:
    recorded = _record(world)
    authority = _authority(world)
    with pytest.raises(EffectApprovalPending):
        authority.authorize_recorded(recorded)
    (request,) = _requests(world)
    expected = effect_evidence_hash(
        run_manifest_digest=DIGEST,
        node_id="agent",
        occurrence_id="occ-1",
        intent_hash=recorded.intent_hash,
    )
    assert request.evidence_hash == expected
    others = {
        effect_evidence_hash(
            run_manifest_digest="b" * 64,
            node_id="agent",
            occurrence_id="occ-1",
            intent_hash=recorded.intent_hash,
        ),
        effect_evidence_hash(
            run_manifest_digest=DIGEST,
            node_id="other",
            occurrence_id="occ-1",
            intent_hash=recorded.intent_hash,
        ),
        effect_evidence_hash(
            run_manifest_digest=DIGEST,
            node_id="agent",
            occurrence_id="occ-2",
            intent_hash=recorded.intent_hash,
        ),
        effect_evidence_hash(
            run_manifest_digest=DIGEST,
            node_id="agent",
            occurrence_id="occ-1",
            intent_hash="sha256:" + "0" * 64,
        ),
    }
    assert expected not in others and len(others) == 4


@pytest.mark.parametrize(
    "created_by",
    [None, "", " editor", "editor ", "x" * 257, "ed\u200bitor", "ed\x00itor"],
)
def test_a_run_without_a_usable_initiator_has_no_authority(world: Any, created_by: Any) -> None:
    with pytest.raises(GovernedContextUnavailable) as caught:
        _authority(world, created_by=created_by)
    assert caught.value.code == "requester_unavailable"


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64, "sha256:" + "a" * 64])
def test_a_run_whose_manifest_digest_is_not_a_sha256_hex_digest_has_no_authority(
    world: Any, digest: str
) -> None:
    with pytest.raises(GovernedContextUnavailable) as caught:
        _authority(world, digest=digest)
    assert caught.value.code == "run_manifest_unavailable"


def test_a_worker_not_wired_to_the_lease_has_no_authority(world: Any) -> None:
    with pytest.raises(GovernedContextUnavailable) as caught:
        run_authority(
            session_factory=world,
            run=_run_row(world),
            config=_config(),
            lease_provider=None,
            run_manifest_digest=DIGEST,
        )
    assert caught.value.code == "lease_provider_unbound"


@pytest.mark.parametrize(
    "broken",
    [
        {"effect_callback_connectors": "not json"},
        {"approval_ttl_seconds": 0},
    ],
)
def test_a_configuration_that_cannot_be_read_gives_no_authority(
    world: Any, broken: dict[str, Any]
) -> None:
    config = SimpleNamespace(
        **{
            "effect_callback_connectors": "[]",
            "approval_ttl_seconds": 3600,
            "approval_allow_self_approval": False,
            **broken,
        }
    )
    with pytest.raises(GovernedContextUnavailable) as caught:
        run_authority(
            session_factory=world,
            run=_run_row(world),
            config=config,  # type: ignore[arg-type]
            lease_provider=Holder(None),
            run_manifest_digest=DIGEST,
        )
    assert caught.value.code == "configuration_invalid"


# ------------------------------------------------------------------------------------- dispatch of a step


def _prior(recorded: Any) -> PriorProposal:
    return PriorProposal(recorded.proposal_id, "proposed", recorded.intent_hash)


def test_an_unapproved_step_proposal_is_not_dispatched(world: Any) -> None:
    authority = _authority(world, config=_config(effect_callback_connectors=_binding()))
    recorded = _record(world)
    with pytest.raises(EffectApprovalPending):  # the step asked when it proposed
        authority.authorize(recorded.proposal_id, evidence_hash=EVIDENCE)
    assert authority.dispatch_approved(_prior(recorded)) is None
    assert len(_requests(world)) == 1, "and a resume asks nothing new"


def _binding(connector: str = "document-request", project: str = "PRJ-ep") -> str:
    return json.dumps(
        [
            {
                "connector": connector,
                "project_id": project,
                "secret_source": "env://SECRET",
                "base_url": "http://127.0.0.1:9",
            }
        ]
    )


def test_a_step_proposal_whose_connector_is_not_bound_for_the_project_is_refused_before_any_claim(
    world: Any,
) -> None:
    authority = _authority(world)  # no binding at all
    recorded = _record(world)
    with pytest.raises(EffectRefused) as refused:
        authority.dispatch_approved(_prior(recorded))
    assert refused.value.code == "connector_unbound"
    assert _requests(world) == [], "not even asked about: it could never be sent"


def test_a_binding_for_another_project_is_not_a_binding_for_this_one(world: Any) -> None:
    authority = _authority(
        world, config=_config(effect_callback_connectors=_binding(project="PRJ-other"))
    )
    recorded = _record(world)
    with pytest.raises(EffectRefused) as refused:
        authority.dispatch_approved(_prior(recorded))
    assert refused.value.code == "connector_unbound"
    assert authority.project_id == "PRJ-ep"


def test_a_step_proposal_is_dispatched_only_through_a_backend_an_agent_step_may_reach(
    world: Any,
) -> None:
    authority = _authority(world, config=_config(effect_callback_connectors=_binding("http")))
    recorded = _record(world, connector="http", node="hook")
    with pytest.raises(EffectRefused) as refused:
        authority.dispatch_approved(_prior(recorded))
    assert refused.value.code == "unsupported_backend"


def test_a_step_proposal_that_already_moved_is_not_dispatched_again(world: Any) -> None:
    authority = _authority(world, config=_config(effect_callback_connectors=_binding()))
    recorded = _record(world)
    with world() as session:
        row = session.get(CaliberEffectProposal, recorded.proposal_id)
        assert row is not None
        transition(
            session,
            recorded.proposal_id,
            "failed",
            expected_version=row.version,
            reason="rejected",
        )
        session.commit()
    with pytest.raises(EffectRefused) as refused:
        authority.dispatch_approved(_prior(recorded))
    assert refused.value.code == "proposal_not_proposed"


# --------------------------------------------------------------------------- the effect context and nodes


def _verify(request: Any, response: Mapping[str, Any]) -> ReceiptFacts:
    return ReceiptFacts(
        attempt=request.view.attempt or 1,
        idempotency_key=request.view.logical_key,
        connector=request.view.connector,
        connector_version=request.view.connector_version,
        resource=request.view.resource,
        intent_hash=request.view.intent_hash,
        downstream_request_id=str(response.get("request_id", "r-1")),
        response_digest="sha256:" + "c" * 64,
    )


def _node_plan(
    factory: Callable[[], Any],
    context: GovernedEffectContext,
    *,
    sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> RuntimePlan:
    resolver = InMemoryToolResolver({})
    parsed = parse_manifest(manifest or _http_manifest("webhook", "https://example.test/governed"))
    return RuntimePlan(
        ir=build_ir(parsed, resolver, version="1"),
        resolver=resolver,
        workflow_version_id="governed-version",
        webhook_sender=sender or (lambda _request: {"status_code": 201, "request_id": "r-1"}),
        egress_policy=_ALLOW_UNRESOLVABLE,
        governed_effects=context,
    )


def test_a_workflow_node_is_refused_before_anything_is_recorded_when_its_backend_has_no_verifier(
    world: Any,
) -> None:
    """No generic connector has an authoritative receipt verifier (#1055): the node is refused, no approval is asked for."""
    sent: list[Any] = []
    context = effect_context(_authority(world))
    assert context.dispatchable_backends == frozenset()
    plan = _node_plan(world, context, sender=lambda request: sent.append(request) or {})

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "HTTP effect refused: no_authoritative_verifier" in (result.error or "")
    assert sent == []
    assert _rows(world) == ([], [])


def test_the_receipt_verifier_of_a_context_never_allows_an_unverified_connector(world: Any) -> None:
    context = effect_context(_authority(world), verifiers={"http": _verify})
    request = SimpleNamespace(view=SimpleNamespace(connector="registered_tool"))
    with pytest.raises(EffectRefused) as refused:
        context.receipt_verifier(request, {})  # type: ignore[arg-type]
    assert refused.value.code == "no_authoritative_verifier"


def _row_validator(
    session: Session, *, approval_id: str, proposal_id: str, **_: Any
) -> ApprovalCommit:
    """Stands in for the commit recheck: it binds the commit to the approval row exactly as the real validator does.

    The real recheck (every approver's project role, the requester, the initiator, the run's grant) needs a fully admitted run
    and is proven on PostgreSQL through the public path; what is under test here is the runtime and the producer.
    """
    approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
    proposal = session.get(CaliberEffectProposal, proposal_id)
    assert approval is not None and proposal is not None
    return ApprovalCommit(
        run_id=proposal.workflow_run_id,
        approval_id=approval_id,
        proposal_id=proposal_id,
        approver_id=str(approval.decided_by),
        intent_hash=proposal.intent_hash,
        evidence_hash=str(approval.evidence_hash),
        policy_digest=str(approval.policy_digest),
        expected_resource_version=proposal.expected_resource_version,
        grant_id="grant",
        decision_id="decision",
        run_identity=object(),
    )


@pytest.fixture
def real_validator_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _row_validator)
    monkeypatch.setattr(runtime_module, "approval_valid_at_commit", _row_validator)


@pytest.mark.usefixtures("real_validator_stub")
def test_a_workflow_node_parks_behind_its_bound_request_and_is_sent_once_after_the_decision(
    world: Any,
) -> None:
    sent: list[dict[str, Any]] = []

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        sent.append(request)
        return {"status_code": 201, "request_id": "r-1", "headers": {}}

    authority = _authority(world)
    plan = _node_plan(world, effect_context(authority, verifiers={"http": _verify}), sender=sender)

    first = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert first.status == "blocked" and first.error == "waiting_approval:hook"
    (step,) = [item for item in first.steps if item.node_id == "hook"]
    (proposal,), (request,) = _rows(world)
    state = step.checkpoint_state
    assert state is not None
    assert state["kind"] == "runtime_approval"
    assert state["proposal_id"] == proposal.proposal_id
    assert state["approval_request_id"] == request.runtime_approval_id
    assert state["intent_hash"] == proposal.intent_hash
    assert "example.test" not in str(state), "identifiers only, never the effect"
    assert isinstance(step.input_by_port, dict)
    assert sent == [] and proposal.state == "proposed"

    # A decision alone sends nothing; resuming the node (as the resume route does) finds its one logical effect.
    _decide(world, request.runtime_approval_id)
    checkpoint = RuntimeResumeCheckpoint(
        node_id="hook",
        checkpoint_kind="runtime_approval",
        input_by_port=dict(step.input_by_port),
        replay_output=False,
    )
    resumed = execute(plan, "hello", executor=FakeWorkflowExecutor(), resume_checkpoint=checkpoint)

    assert resumed.status == "completed", resumed.error
    assert len(sent) == 1 and sent[0]["headers"]["Idempotency-Key"]
    (done,), (spent,) = _rows(world)
    assert done.proposal_id == proposal.proposal_id and done.state == "committed"
    assert spent.consumed_at is not None


@pytest.mark.usefixtures("real_validator_stub")
def test_a_workflow_node_whose_lease_is_gone_fails_with_the_fixed_code_and_sends_nothing(
    world: Any,
) -> None:
    sent: list[Any] = []
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    holder = Holder(lease)
    plan = _node_plan(
        world,
        effect_context(_authority(world, holder), verifiers={"http": _verify}),
        sender=lambda request: sent.append(request) or {"status_code": 201},
    )
    first = execute(plan, "hello", executor=FakeWorkflowExecutor())
    (step,) = [item for item in first.steps if item.node_id == "hook"]
    _decide(world, _requests(world)[0].runtime_approval_id)
    holder.lease = None
    checkpoint = RuntimeResumeCheckpoint(
        node_id="hook",
        checkpoint_kind="runtime_approval",
        input_by_port=dict(step.input_by_port or {}),
        replay_output=False,
    )

    resumed = execute(plan, "hello", executor=FakeWorkflowExecutor(), resume_checkpoint=checkpoint)

    assert resumed.status == "error"
    assert "HTTP effect authorization refused: lease_unavailable" in (resumed.error or "")
    assert sent == []
    assert _rows(world)[0][0].state == "proposed"


def test_a_workflow_node_can_resume_from_an_approval_only_in_a_plan_with_a_governed_context(
    world: Any,
) -> None:
    from caliber.orchestrator.workflow_run_worker import _resume_checkpoint_preflight_error

    checkpoint = RuntimeResumeCheckpoint(
        node_id="hook",
        checkpoint_kind="runtime_approval",
        input_by_port={"input": "x"},
        replay_output=False,
    )
    governed = _node_plan(world, effect_context(_authority(world)))
    ungoverned = replace(governed, governed_effects=None)
    assert _resume_checkpoint_preflight_error(governed, checkpoint) is None
    assert _resume_checkpoint_preflight_error(ungoverned, checkpoint) is not None
    refused = execute(
        ungoverned, "hello", executor=FakeWorkflowExecutor(), resume_checkpoint=checkpoint
    )
    assert refused.status == "error" and "does not match current node 'hook'" in (
        refused.error or ""
    )


@pytest.mark.usefixtures("real_validator_stub")
def test_an_api_request_node_parks_the_same_way(world: Any) -> None:
    plan = _node_plan(
        world,
        effect_context(_authority(world), verifiers={"http": _verify}),
        manifest=api_manifest(_url_node(url="https://example.test/api")),
    )
    result = execute(plan, "hello", executor=FakeWorkflowExecutor())
    assert result.status == "blocked" and (result.error or "").startswith("waiting_approval:")
    assert len(_rows(world)[1]) == 1


@pytest.mark.parametrize("kind", ["for_each", "loop", "error_boundary"])
def test_a_workflow_effect_inside_a_fan_out_loop_or_boundary_cannot_park_the_run_and_fails_it(
    world: Any, kind: str
) -> None:
    """A target runs inside its parent: there is no node of the graph to resume, so it is an error, never a park, and a
    boundary may not absorb the signal as a handled error."""
    manifest = _inline_manifest(kind)
    hook = _http_manifest("webhook", "https://example.test/governed")["nodes"]["hook"]
    manifest["nodes"]["hook"] = hook
    del manifest["nodes"]["agent"]
    manifest["nodes"]["parent"]["target_node_id"] = "hook"
    plan = _node_plan(
        world, effect_context(_authority(world), verifiers={"http": _verify}), manifest=manifest
    )

    result = execute(plan, '["a"]', executor=FakeWorkflowExecutor())

    assert result.status == "error", result.status
    assert "cannot run inline in a fan-out, loop or boundary: node 'hook'" in (result.error or "")
    assert len(_rows(world)[1]) == 1, (
        "the approval was asked for once and nobody can answer it: #1074's to settle"
    )


# --------------------------------------------------------------------------------- governed agent nodes


def test_only_the_agents_that_can_reach_a_mapped_tool_are_governed(world: Any) -> None:
    ir = _plan(world).ir
    agents = [node for node in ir.nodes.values() if isinstance(node, IRAgent)]
    classify = registry_tool_classifier(world)
    assert governed_agent_nodes(
        agents, classify=classify, tool_backends={PROTECTED_ID: "document_request"}
    ) == {"agent"}
    assert governed_agent_nodes(agents, classify=classify, tool_backends={}) == frozenset()
    assert (
        governed_agent_nodes(
            agents, classify=classify, tool_backends={("another_tool", "1.0"): "document_request"}
        )
        == frozenset()
    ), "the map is keyed by registry identity: a tool that is not in it governs nothing"
    assert (
        governed_agent_nodes(
            agents, classify=classify, tool_backends={(PROTECTED, "9.9"): "document_request"}
        )
        == frozenset()
    ), "and by version"


def test_the_step_context_is_the_operators_ceilings_budget_and_map(world: Any) -> None:
    config = _config(
        agent_step_max_turns=3,
        agent_step_max_tool_calls=2,
        agent_step_max_tokens=5000,
        agent_step_max_seconds=90.0,
        agent_run_max_tokens=123456,
        approval_ttl_seconds=600,
    )
    authority = _authority(world, config=config)
    context = step_context(
        authority,
        grant_id="grant-9",
        governed_nodes=frozenset({"agent"}),
        tool_backends={PROTECTED_ID: "document_request"},
    )

    assert context.grant_id == "grant-9" and context.run_manifest_digest == DIGEST
    assert context.requested_by == INITIATOR and context.approval_ttl_seconds == 600
    assert context.ceilings == StepCeilings.from_config(config)
    assert context.run_token_budget == 123456
    assert dict(context.tool_backends) == {PROTECTED_ID: "document_request"}
    assert context.governed_nodes == frozenset({"agent"})
    assert context.dispatch_approved == authority.dispatch_approved
    agent = _plan(world).ir.nodes["agent"]
    assert isinstance(agent, IRAgent)
    limits = context.limits(agent)
    assert (limits.max_turns, limits.max_tool_calls, limits.max_tokens) == (3, 2, 5000)
    assert (
        datetime.now(timezone.utc)
        < limits.deadline_at
        <= datetime.now(timezone.utc) + timedelta(seconds=91)
    )


def test_a_context_names_only_the_agent_nodes_it_governs(world: Any) -> None:
    only = _plan(world, governed_nodes=frozenset({"agent"})).governed_agent_steps
    nothing = _plan(world, governed_nodes=frozenset()).governed_agent_steps
    everything = _plan(world).governed_agent_steps
    assert only is not None and nothing is not None and everything is not None
    assert (only.governs("agent"), only.governs("agent_two")) == (True, False)
    assert nothing.governs("agent") is False
    assert everything.governs("anything"), (
        "a context that names none governs every agent, as before the worker"
    )


def test_an_agent_the_context_does_not_name_runs_as_before(world: Any) -> None:
    executor = FakeWorkflowExecutor()
    result = execute(
        _plan(world, governed_nodes=frozenset()),
        "hello",
        executor=executor,
        extra_tools=Canaries().tools(),
    )
    assert result.status == "completed", result.error
    assert executor.step_requests == [], "a legacy agent turn, not a governed step"
    assert _rows(world) == ([], [])


def test_the_governed_agent_of_two_is_refused_in_a_parallel_branch_and_the_text_agent_is_not(
    world: Any,
) -> None:
    governed_one = _plan(world, manifest=_parallel_manifest(), governed_nodes=frozenset({"agent"}))
    refused = execute(
        governed_one,
        "go",
        executor=FakeWorkflowExecutor(step_script=[_proposing()]),
        extra_tools=Canaries().tools(),
    )
    assert refused.status == "error"
    assert "governed agent steps cannot run in parallel branches: agent 'agent'" in (
        refused.error or ""
    )

    none = _plan(world, manifest=_parallel_manifest(), governed_nodes=frozenset())
    ran = execute(none, "go", executor=FakeWorkflowExecutor(), extra_tools=Canaries().tools())
    assert ran.status == "completed", ran.error


def test_an_agent_that_is_not_governed_may_not_hand_off_to_one_that_is(world: Any) -> None:
    manifest = _manifest()
    manifest["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    manifest["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "tools": [PROTECTED],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    ir = _plan(world, manifest=manifest).ir
    refusal = runtime_module._governed_plan_refusal(ir, lambda node_id: node_id == "billing")
    assert refusal == (
        "governed agent steps cannot run inline: agent 'billing' is a handoff target of agent 'agent'"
    )
    assert runtime_module._governed_plan_refusal(ir, lambda node_id: False) is None


def test_mapped_agents_are_found_in_the_reference_profile_only(world: Any) -> None:
    ir = _plan(world).ir
    reference = _mapped()
    assert mapped_agent_nodes(ir.nodes.values(), reference, world) == {"agent"}
    development = reference.model_copy(update={"profile": "development"})
    assert mapped_agent_nodes(ir.nodes.values(), development, world) == frozenset()
    unmapped = reference.model_copy(update={"agent_step_tool_backends": "[]"})
    assert mapped_agent_nodes(ir.nodes.values(), unmapped, world) == frozenset()
    with pytest.raises(GovernedContextUnavailable):
        mapped_agent_nodes(
            ir.nodes.values(), reference.model_copy(update={"agent_step_tool_backends": "x"}), world
        )


def _requiring(world: Any, **changes: Any) -> RuntimePlan:
    plan = _plan(world, governed=False)
    plan.agents_requiring_governance = frozenset({"agent"})
    for name, value in changes.items():
        setattr(plan, name, value)
    return plan


def test_a_plan_with_no_governed_step_refuses_an_agent_that_can_reach_a_mapped_tool(
    world: Any,
) -> None:
    """A sub-workflow or a synchronous run has no step context: the agent is refused, not run as a legacy agent."""
    executor = FakeWorkflowExecutor()
    canaries = Canaries()
    result = execute(_requiring(world), "hello", executor=executor, extra_tools=canaries.tools())
    assert result.status == "error"
    assert "agent 'agent' can reach a tool the operator mapped to a governed backend" in (
        result.error or ""
    )
    assert canaries.mutations == [] and canaries.reads == [] and _rows(world) == ([], [])


def test_the_refusal_does_not_touch_a_preview_or_an_agent_the_plan_governs(world: Any) -> None:
    previewed = execute(
        _requiring(world),
        "hello",
        executor=FakeWorkflowExecutor(),
        preview=True,
        extra_tools=Canaries().tools(),
    )
    assert previewed.status == "completed", previewed.error
    governed = _plan(world)
    governed.agents_requiring_governance = frozenset({"agent"})
    parked = execute(
        governed,
        "hello",
        executor=FakeWorkflowExecutor(step_script=[_proposing()]),
        extra_tools=Canaries().tools(),
    )
    assert parked.status == "blocked" and parked.error == "waiting_approval:agent"


def test_a_plan_with_no_governed_step_refuses_an_agent_that_hands_off_to_a_mapped_one(
    world: Any,
) -> None:
    manifest = _manifest()
    manifest["nodes"]["agent"]["tools"] = []
    manifest["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    manifest["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "tools": [PROTECTED],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    plan = _plan(world, governed=False, manifest=manifest)
    plan.agents_requiring_governance = frozenset({"billing"})
    result = execute(plan, "hello", executor=FakeWorkflowExecutor(), extra_tools=Canaries().tools())
    assert result.status == "error"
    assert "agent 'billing' can reach a tool the operator mapped" in (result.error or "")


def test_a_governed_agent_as_the_target_of_a_fan_out_is_refused_only_when_it_is_governed(
    world: Any,
) -> None:
    ir = _plan(world, manifest=_inline_manifest("for_each")).ir
    assert runtime_module._governed_plan_refusal(ir, lambda node_id: node_id == "agent") is not None
    assert runtime_module._governed_plan_refusal(ir, lambda node_id: False) is None


# ---------------------------------------------------------------------- dispatching an approved step


def _parked(world: Any, **changes: Any) -> Any:
    """Run the agent step to its park, then return a function that runs it again under a context with ``changes``."""
    first, _, _ = _run(world, [_proposing()])
    assert first.status == "blocked"

    def again(**more: Any) -> tuple[Any, FakeWorkflowExecutor, Canaries]:
        return _run(world, [], plan=_plan(world, **changes, **more))

    return again


def test_an_approved_step_is_dispatched_by_the_producer_and_completes_when_it_commits(
    world: Any,
) -> None:
    seen: list[PriorProposal] = []

    def dispatch_approved(prior: PriorProposal) -> EffectOutcome:
        seen.append(prior)
        return EffectOutcome(prior.proposal_id, "committed", receipt_id="rcpt-1")

    result, executor, canaries = _parked(world)(dispatch_approved=dispatch_approved)

    assert result.status == "completed", result.error
    assert [item.state for item in seen] == ["proposed"]
    assert executor.step_requests == [] and canaries.mutations == []
    (step,) = [item for item in result.steps if item.node_id == "agent"]
    assert step.output == f"effect {seen[0].proposal_id} committed"


def test_a_step_without_an_approval_decision_keeps_waiting_behind_its_request(world: Any) -> None:
    result, executor, _ = _parked(world)(dispatch_approved=lambda _prior: None)
    assert result.status == "blocked" and result.error == "waiting_approval:agent"
    assert executor.step_requests == []
    assert len(_rows(world)[1]) == 1


def test_a_context_that_cannot_dispatch_leaves_an_approved_step_parked_as_before(
    world: Any,
) -> None:
    result, _, _ = _parked(world)()
    assert result.status == "blocked" and result.error == "waiting_approval:agent"


def test_a_refused_dispatch_fails_the_run_with_only_its_fixed_code(world: Any) -> None:
    def refuse(_prior: PriorProposal) -> EffectOutcome:
        raise EffectRefused("lease_unavailable", "secret detail that must not travel")

    result, _, _ = _parked(world)(dispatch_approved=refuse)
    assert result.status == "error"
    assert (
        result.error
        == "ToolExecutionError: governed agent step dispatch refused: lease_unavailable"
    )
    assert "secret" not in (result.error or "")


@pytest.mark.parametrize("state", ["unknown", "dispatched", "reconciled", "approved"])
def test_a_dispatch_that_ends_short_of_committed_parks_the_run_for_the_reconciler(
    world: Any, state: str
) -> None:
    result, _, _ = _parked(world)(
        dispatch_approved=lambda prior: EffectOutcome(prior.proposal_id, state)
    )
    assert result.status == "blocked" and result.error == "waiting_effect:agent"


def test_a_dispatch_the_downstream_rejected_fails_the_node(world: Any) -> None:
    result, _, _ = _parked(world)(
        dispatch_approved=lambda prior: EffectOutcome(prior.proposal_id, "failed")
    )
    assert result.status == "error" and "its proposed effect failed" in (result.error or "")


def test_a_dispatch_that_raised_after_it_may_have_sent_parks_when_the_effect_is_held(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_prior: PriorProposal) -> EffectOutcome:
        raise RuntimeError("the model-chosen argument value in a statement parameter")

    again = _parked(world)
    held = PriorProposal(_rows(world)[0][0].proposal_id, "unknown", "sha256:" + "1" * 64)
    real = runtime_module._step_proposal.prior_proposal
    calls: list[str] = []

    def prior(*args: Any, **kwargs: Any) -> Any:
        calls.append("read")
        # the first read finds the proposal as the step left it; the one after the failure finds it held
        return real(*args, **kwargs) if len(calls) == 1 else held

    monkeypatch.setattr(runtime_module._step_proposal, "prior_proposal", prior)
    result, _, _ = again(dispatch_approved=boom)
    assert result.status == "blocked" and result.error == "waiting_effect:agent"


def test_a_refusal_after_the_send_parks_the_run_instead_of_failing_it(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the REAL dispatcher: the downstream was called, then settling was refused (the lease moved on).

    ``dispatch`` raises ``EffectRefused(lease_lost)`` with the proposal left ``dispatched``; failing the run there would hide an
    effect that was sent, so the node parks for the reconciler and nothing is sent a second time.
    """
    sent: list[str] = []

    def transport(request: Any, _config: Any) -> TransportResult:
        sent.append(request.view.logical_key)
        return TransportResult(Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1))

    def settle(*_args: Any, **_kwargs: Any) -> Any:
        raise ProposalError(LEASE_LOST, "the lease moved on")

    for module in (dispatcher_module, governed_context):
        monkeypatch.setattr(module, "approval_valid_at_commit", _row_validator)
    monkeypatch.setattr(
        dispatcher_module, "_backend_call", lambda _b, request, config: transport(request, config)
    )
    monkeypatch.setattr(dispatcher_module, "settle_dispatch", settle)
    authority = _authority(world, config=_config(effect_callback_connectors=_binding()))
    first, _, _ = _run(
        world, [_proposing()], plan=_plan(world, dispatch_approved=authority.dispatch_approved)
    )
    assert first.status == "blocked"
    _decide(world, _requests(world)[0].runtime_approval_id)

    result, executor, _ = _run(
        world, [], plan=_plan(world, dispatch_approved=authority.dispatch_approved)
    )

    assert result.status == "blocked" and result.error == "waiting_effect:agent", result.error
    assert len(sent) == 1, "the downstream was called exactly once"
    assert _rows(world)[0][0].state == "dispatched", (
        "the claim stands; the reconciler asks the downstream by key"
    )
    assert executor.step_requests == []


def test_a_refusal_whose_proposal_turns_out_committed_completes_the_node(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_prior: PriorProposal) -> EffectOutcome:
        raise EffectRefused("lease_lost")

    again = _parked(world)
    real = runtime_module._step_proposal.prior_proposal
    calls: list[int] = []

    def prior(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        found = real(*args, **kwargs)
        return found if len(calls) == 1 else replace(found, state="committed")

    monkeypatch.setattr(runtime_module._step_proposal, "prior_proposal", prior)
    result, _, _ = again(dispatch_approved=refuse)
    assert result.status == "completed", result.error


def test_a_dispatch_that_raised_before_anything_moved_is_a_store_failure_without_its_text(
    world: Any,
) -> None:
    def boom(_prior: PriorProposal) -> EffectOutcome:
        raise RuntimeError("the model-chosen argument value in a statement parameter")

    result, _, _ = _parked(world)(dispatch_approved=boom)
    assert result.status == "error"
    assert result.error == "ToolExecutionError: governed agent step dispatch failed: store_failed"
    assert "model-chosen" not in (result.error or "")


def test_a_dispatch_that_raised_and_whose_state_cannot_be_read_is_still_only_a_fixed_error(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_prior: PriorProposal) -> EffectOutcome:
        raise RuntimeError("secret")

    again = _parked(world)
    real = runtime_module._step_proposal.prior_proposal
    calls: list[int] = []

    def prior(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return real(*args, **kwargs)
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(runtime_module._step_proposal, "prior_proposal", prior)
    result, _, _ = again(dispatch_approved=boom)
    assert result.error == "ToolExecutionError: governed agent step dispatch failed: store_failed"


# ---------------------------------------------------------------------------------------- the worker


class _Plan:
    """Just what ``_bind_governed_contexts`` reads and sets of a plan."""

    def __init__(self, ir: Any) -> None:
        self.ir = ir
        self.governed_effects: Any = None
        self.governed_agent_steps: Any = None
        self.context_recorder: Any = None


def _worker(
    factory: Callable[[], Any], config: CaliberConfig, provider: Callable[[], Lease | None] | None
) -> Any:
    return worker_module.WorkflowRunWorker(factory, config=config, lease_provider=provider)


def _bind(
    world: Any,
    config: CaliberConfig,
    *,
    provider: Callable[[], Lease | None] | None,
    admitted: bool = True,
    manifest: dict[str, Any] | None = None,
    pinned_digest: str = DIGEST,
) -> tuple[str | None, _Plan]:
    plan = _Plan(_plan(world, manifest=manifest).ir)
    run = _run_row(world)
    pinned = SimpleNamespace(digest=pinned_digest, record={"workflow_run_id": RUN})
    identity = SimpleNamespace(grant_id="grant-1") if admitted else None
    failure = _worker(world, config, provider)._bind_governed_contexts(
        run, plan, pinned if admitted else None, identity, None
    )
    return failure, plan


def _mapped() -> CaliberConfig:
    return _config(
        profile="reference",
        agent_step_tool_backends=json.dumps(
            [{"tool": PROTECTED, "version": "1.0", "backend": "document_request"}]
        ),
    )


def test_the_development_profile_binds_nothing(world: Any) -> None:
    failure, plan = _bind(
        world,
        _config(agent_step_tool_backends=_mapped().agent_step_tool_backends),
        provider=Holder(None),
    )
    assert failure is None
    assert plan.governed_effects is None and plan.governed_agent_steps is None


def test_a_reference_run_with_a_lease_gets_the_effect_context_and_governs_only_the_mapped_agent(
    world: Any,
) -> None:
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    failure, plan = _bind(world, _mapped(), provider=Holder(lease))

    assert failure is None
    assert (
        plan.governed_effects is not None
        and plan.governed_effects.dispatchable_backends == frozenset()
    )
    assert plan.governed_agent_steps is not None
    assert plan.governed_agent_steps.governed_nodes == frozenset({"agent"})
    assert plan.context_recorder is not None, "a governed step needs a recorded context"
    assert plan.governed_agent_steps.grant_id == "grant-1"
    assert plan.governed_agent_steps.run_manifest_digest == DIGEST


def test_a_reference_run_with_no_mapped_tool_is_left_to_run_as_before(world: Any) -> None:
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    failure, plan = _bind(world, _config(profile="reference"), provider=Holder(lease))
    assert failure is None
    assert plan.governed_effects is not None, (
        "protected workflow nodes are governed (and refused for want of a verifier)"
    )
    assert plan.governed_agent_steps is None and plan.context_recorder is None


def test_a_reference_run_without_a_lease_binding_leaves_effect_nodes_refused_and_the_agent_failed(
    world: Any,
) -> None:
    # nothing governed: the effect context is simply not bound (a protected node is refused for want of one)
    failure, plan = _bind(world, _config(profile="reference"), provider=None)
    assert failure is None and plan.governed_effects is None
    # a mapped agent: the run must not proceed as a legacy agent
    failure, plan = _bind(world, _mapped(), provider=None)
    assert failure == "lease_provider_unbound"
    assert plan.governed_agent_steps is None


def test_a_reference_run_that_was_not_admitted_cannot_reach_a_mapped_tool(world: Any) -> None:
    failure, _ = _bind(world, _mapped(), provider=Holder(None), admitted=False)
    assert failure == "run_not_admitted"
    failure, plan = _bind(
        world, _config(profile="reference"), provider=Holder(None), admitted=False
    )
    assert failure is None and plan.governed_effects is None


def test_a_reference_run_with_a_broken_mapping_or_manifest_digest_is_refused(world: Any) -> None:
    failure, _ = _bind(
        world, _mapped().model_copy(update={"agent_step_tool_backends": "x"}), provider=Holder(None)
    )
    assert failure == "configuration_invalid"
    failure, _ = _bind(world, _mapped(), provider=Holder(None), pinned_digest="nope")
    assert failure == "run_manifest_unavailable"


def test_the_worker_reads_the_lease_it_is_bound_to_when_it_dispatches(world: Any) -> None:
    worker = _worker(world, _config(), None)
    assert worker._lease_provider is None
    holder = Holder(None)
    worker.bind_lease_provider(holder)
    assert worker._lease_provider is holder
    worker.bind_lease_provider(None)
    assert worker._lease_provider is None


def test_a_checkpoint_of_a_governed_workflow_effect_names_its_proposal_and_request() -> None:
    from caliber.orchestrator.workflow_run_worker import _waiting_approval_checkpoint_payload

    state = {
        "kind": "runtime_approval",
        "node_id": "hook",
        "proposal_id": "p-1",
        "approval_request_id": "rap-1",
        "intent_hash": "sha256:" + "1" * 64,
    }
    step = runtime_module.NodeStep(
        "hook",
        "webhook",
        "blocked",
        detail="waiting_approval:hook",
        checkpoint_state=state,
        input_by_port={"input": "x"},
    )
    result = runtime_module.WorkflowRunResult(
        status="blocked", output="", error="waiting_approval:hook", steps=[step]
    )
    payload = _waiting_approval_checkpoint_payload(result)
    assert payload is not None and payload[0] == "hook"
    assert payload[1]["proposal_id"] == "p-1" and payload[1]["input_by_port"] == {"input": "x"}
    # a webhook node that carries no bound request is not a resumable approval
    bare = replace(step, checkpoint_state=None)
    assert _waiting_approval_checkpoint_payload(replace(result, steps=[bare])) is None
    # nor one whose request ids are missing
    partial = replace(step, checkpoint_state={k: v for k, v in state.items() if k != "proposal_id"})
    assert _waiting_approval_checkpoint_payload(replace(result, steps=[partial])) is None


def test_an_unknown_effect_still_parks_with_its_own_signal(world: Any) -> None:
    """The two park signals are not the same thing: an open effect is ``waiting_effect``, a pending approval ``waiting_approval``."""
    assert not issubclass(EffectApprovalPending, UnknownEffectError)
    assert not issubclass(UnknownEffectError, EffectApprovalPending)


def test_the_module_exports_what_the_worker_binds() -> None:
    assert {"effect_context", "step_context", "run_authority", "governed_agent_nodes"} <= set(
        governed_context.__all__
    )
