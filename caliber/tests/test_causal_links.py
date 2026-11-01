"""The persisted causal links of a run, read back as identifiers (B11.3b, #745).

Two layers. The service (``caliber.observability.causal_links``) runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set,
on PostgreSQL, against a database migrated by Alembic: the window functions and the keyset anchor are SQL, so the dialect
matters. The route (``GET /workflow-runs/{run_id}/causal-links``) runs on the application fixture and proves who may read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberContextManifest,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.receipts import ReceiptFacts, record_receipt
from caliber.effects.records import (
    APPROVED,
    COMMITTED,
    DISPATCHED,
    FAILED,
    PROPOSED,
    UNKNOWN,
    proposal_view,
)
from caliber.observability import causal_links as service
from caliber.observability.causal_links import (
    CausalLinksCursorError,
    decode_cursor,
    encode_cursor,
    read_causal_links,
)
from caliber.schemas import CausalLinksSchema
from caliber.workflows.approval_policy import ApprovalCommit
from tests.dialect_helpers import DIALECTS
from tests.test_effect_proposals import _record, _seed_run
from tests.test_effect_records import AT, NAIVE_AT, OWNER, _at, _decision, _move
from tests.test_event_outbox import _database

RUN = "RUN-ep"
OTHER_RUN = "RUN-other"
#: Strings that must never leave the database through this API: they sit in every column that carries a payload,
#: a resource, a person or prose on the rows read.
CANARY = "CANARY-5c1d7e"
EVIDENCE = "sha256:" + "e" * 64
POLICY = "sha256:" + "f" * 64


def _context(
    session: Session,
    *,
    node: str,
    occurrence: str,
    attempt: int,
    revision: int,
    step: str | None = None,
    run: str = RUN,
    created: int = 0,
) -> str:
    """A persisted context manifest of one node occurrence (the digest is the SHA-256 of the bytes, as the ORM requires)."""
    import hashlib

    from caliber.context.manifest import node_step_id_for

    step_request_id = step or f"step-{node}-{occurrence}-{attempt}"
    body = f"{CANARY}-context-{step_request_id}-{revision}".encode()
    manifest_id = f"cm-{step_request_id}-{revision}"
    session.add(
        CaliberContextManifest(
            context_manifest_id=manifest_id,
            workflow_run_id=run,
            step_request_id=step_request_id,
            node_step_id=node_step_id_for(run, node, occurrence),
            occurrence_id=occurrence,
            attempt=attempt,
            revision=revision,
            schema_version="v1",
            manifest_bytes=body,
            manifest_digest=hashlib.sha256(body).hexdigest(),
            created_at=NAIVE_AT + timedelta(seconds=created),
        )
    )
    session.flush()
    return manifest_id


def _approval_row(
    session: Session, proposal_id: str, *, status: str, approval_id: str, run: str = RUN
) -> str:
    proposal = session.get(CaliberEffectProposal, proposal_id)
    assert proposal is not None
    session.add(
        CaliberRuntimeApprovalRequest(
            runtime_approval_id=approval_id,
            workflow_run_id=run,
            project_id="PRJ-ep",
            node_id=proposal.node_id,
            status=status,
            requested_at=NAIVE_AT,
            decided_at=NAIVE_AT if status == "approved" else None,
            decided_by=f"{CANARY}-approver" if status == "approved" else None,
            decision_reason=f"{CANARY} reason in prose",
            policy_snapshot={"required_role": "caliber.approver", "note": CANARY},
            proposal_id=proposal_id,
            intent_hash=proposal.intent_hash,
            evidence_hash=EVIDENCE,
            policy_digest=POLICY,
            expected_resource_version=proposal.expected_resource_version,
            expires_at=NAIVE_AT + timedelta(hours=1),
            requested_by=f"{CANARY}-requester",
        )
    )
    session.flush()
    return approval_id


def _exact_intent_committed(
    session: Session, occurrence: str = "occ-exact", *, commit_decision_id: str | None = None
) -> str:
    """An irreversible write approved by an exact-intent request, dispatched and committed with a verified receipt.

    ``commit_decision_id`` is what the dispatcher's claim stores on the request in the UPDATE that consumes it (B11.3c,
    #1061); ``None`` is a request consumed before that column existed.

    Every payload-bearing column holds the canary: the intent's resource and arguments, who requested and decided, the
    decision's prose, the downstream's request id.
    """
    proposal_id = str(
        _record(
            session,
            occurrence,
            node="write",
            resource=f"cases/{CANARY}/resource",
            args={"note": CANARY, "ssn": "123-45-6789"},
            expected_resource_version=f"{CANARY}-v1",
        ).proposal_id
    )
    approval_id = f"rap-{occurrence}"
    _approval_row(session, proposal_id, status="approved", approval_id=approval_id)
    commit = ApprovalCommit(
        run_id=RUN,
        approval_id=approval_id,
        proposal_id=proposal_id,
        approver_id=f"{CANARY}-approver",
        intent_hash=proposal_view(session, proposal_id).intent_hash,
        evidence_hash=EVIDENCE,
        policy_digest=POLICY,
        expected_resource_version=f"{CANARY}-v1",
        grant_id="gr-1",
        decision_id=commit_decision_id or "pd_run_commit_time",
        run_identity=SimpleNamespace(),  # type: ignore[arg-type]
    )
    _move(session, proposal_id, APPROVED, approval=commit)
    _move(session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=1)
    view = proposal_view(session, proposal_id)
    receipt = record_receipt(
        session,
        proposal_id,
        ReceiptFacts(
            attempt=1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=f"{CANARY}-downstream-request-{occurrence}",
            response_digest="sha256:" + "a" * 64,
            resource_version=f"{CANARY}-v2",
        ),
        now=AT,
    )
    _move(session, proposal_id, COMMITTED, receipt_id=receipt.receipt_id, owner_id=OWNER, attempt=1)
    row = session.get(CaliberRuntimeApprovalRequest, approval_id)
    assert row is not None
    row.consumed_at = NAIVE_AT + timedelta(minutes=1)
    row.commit_decision_id = commit_decision_id
    session.flush()
    return proposal_id


@dataclass(frozen=True)
class World:
    """One migrated database holding a run with one effect of every shape, and a second run whose rows must never show."""

    factory: sessionmaker[Session]
    engine: Any
    #: name -> proposal id, for the effects of ``RUN``.
    effects: dict[str, str]
    other_effect: str
    other_decision: str


def _seed(factory: sessionmaker[Session]) -> tuple[dict[str, str], str, str]:
    _seed_run(factory)
    with factory() as session:
        session.add(
            CaliberWorkflowRun(
                workflow_run_id=OTHER_RUN,
                workflow_id="wf-ep",
                project_id="PRJ-ep",
                status="running",
            )
        )
        session.flush()
        effects: dict[str, str] = {}
        effects["complete"] = _at(session, COMMITTED, occurrence="occ-complete")
        # consumed before the decision id was stored: its decision stays unrecorded
        effects["exact"] = _exact_intent_committed(session)
        # consumed since: the claim stored the commit-time decision id beside ``consumed_at``
        effects["exact_recorded"] = _exact_intent_committed(
            session, "occ-exact-recorded", commit_decision_id="pd_commit_exact_recorded"
        )
        effects["bare"] = _at(session, PROPOSED, occurrence="occ-bare")
        effects["asking"] = _at(session, PROPOSED, occurrence="occ-asking")
        _approval_row(session, effects["asking"], status="pending", approval_id="rap-asking")
        effects["dispatched"] = _at(session, DISPATCHED, occurrence="occ-dispatched")
        effects["failed"] = _at(session, FAILED, occurrence="occ-failed")
        effects["unknown"] = _at(session, UNKNOWN, occurrence="occ-unknown")
        # The step that proposed ``complete`` ran twice; its second attempt was assembled twice.
        _context(session, node="ask", occurrence="occ-complete", attempt=1, revision=1, created=1)
        _context(session, node="ask", occurrence="occ-complete", attempt=2, revision=1, created=2)
        _context(
            session,
            node="ask",
            occurrence="occ-complete",
            attempt=2,
            revision=2,
            step="step-ask-occ-complete-2",
            created=3,
        )
        _context(session, node="elsewhere", occurrence="occ-none", attempt=1, revision=1, created=4)
        # A run-level decision of the kind the commit check records (not linked from any effect).
        session.add(
            CaliberPolicyDecision(
                decision_id="pd_run_commit_time",
                workflow_run_id=RUN,
                grant_id="gr-1",
                subject_user_id=f"{CANARY}-subject",
                workload_id="wl_" + "0" * 64,
                action="resource.execute",
                resource="workflow:wf-ep",
                purpose=f"{CANARY} purpose",
                result="allow",
                reason_codes=["grant_active", "role_ok"],
                policy_version="policy-v1",
                input_hash="1" * 64,
                evaluated_at=NAIVE_AT,
            )
        )
        session.add(
            CaliberPolicyDecision(
                decision_id="pd_commit_exact_recorded",
                workflow_run_id=RUN,
                grant_id="gr-1",
                subject_user_id=f"{CANARY}-subject",
                workload_id="wl_" + "0" * 64,
                action="resource.execute",
                resource="workflow:wf-ep",
                purpose=f"{CANARY} purpose",
                result="allow",
                reason_codes=["grant_active", "role_ok"],
                policy_version="policy-v1",
                input_hash="2" * 64,
                evaluated_at=NAIVE_AT,
            )
        )
        # The second run: an approved effect with its own decision.
        other = str(_record_other(session))
        session.commit()
    return effects, other, f"pd-{other}-proposed"


def _record_other(session: Session) -> str:
    from caliber.effects.records import POLICY_DECISION, Approval, record_proposal
    from tests.test_effect_proposals import _intent

    pid = str(
        record_proposal(
            session,
            workflow_run_id=OTHER_RUN,
            node_id="ask",
            occurrence_id="occ-complete",
            intent=_intent(effect_class="read", resource=f"cases/{CANARY}-other"),
        ).proposal_id
    )
    decision = f"pd-{pid}-proposed"
    _decision(session, pid, decision_id=decision, run=OTHER_RUN)
    _move(session, pid, APPROVED, approval=Approval(POLICY_DECISION, decision))
    return pid


@pytest.fixture(scope="module", params=DIALECTS)
def world(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    with (
        pytest.MonkeyPatch.context() as patch,
        _database(request.param, tmp_path_factory.mktemp("causal_links"), patch) as (
            engine,
            factory,
        ),
    ):
        effects, other, other_decision = _seed(factory)
        yield World(factory, engine, effects, other, other_decision)


def _page(world: World, run: str = RUN, **kwargs: Any) -> dict[str, Any]:
    with world.factory() as session:
        row = session.get(CaliberWorkflowRun, run)
        assert row is not None
        # the wire shape: what the route returns after its response schema has filled every field
        return CausalLinksSchema.model_validate(
            read_causal_links(session, row, **kwargs)
        ).model_dump(mode="json")


def _by_node(world: World, **kwargs: Any) -> dict[str, dict[str, Any]]:
    inverse = {value: key for key, value in world.effects.items()}
    page = _page(world, limit=service.MAX_LIMIT, **kwargs)
    assert not page["has_more"]
    return {inverse[effect["proposal_id"]]: effect for effect in page["effects"]}


# ---------------------------------------------------------------- the chain


def test_chain_complete(world: World) -> None:
    """A committed, policy-approved effect names every record of its chain, by the ids its spans carry."""
    effect = _by_node(world)["complete"]
    assert effect["proposal_id"] == world.effects["complete"]
    assert effect["state"] == "committed" and effect["missing_links"] == []
    assert effect["unrecorded_links"] == [], "a complete chain has neither kind"
    assert (effect["approval"]["status"], effect["approval"]["code"]) == (
        "not_required",
        "approved_by_policy_decision",
    )
    assert effect["approval"]["approval_id"] is None
    decision = effect["decision"]
    assert decision["status"] == "linked"
    assert decision["decision_id"] == f"pd-{world.effects['complete']}-proposed"
    assert (decision["action"], decision["result"]) == ("resource.execute", "allow")
    outcome = effect["effect"]
    assert outcome["status"] == "linked" and outcome["committed"] is True
    assert outcome["effect_id"] and outcome["attempt"] == 1
    assert outcome["receipt_digest"] and len(outcome["receipt_digest"]) == 64
    context = effect["context_manifest"]
    assert context["status"] == "linked"
    assert (context["attempt"], context["revision"]) == (2, 2), (
        "the latest attempt, then the highest revision"
    )
    assert context["context_manifest_id"] == "cm-step-ask-occ-complete-2-2"
    assert effect["node_step_id"] == f"{RUN}:ask:occ-complete"
    # the attributes the effect's spans carry, from the same rows (B11.3a)
    assert effect["span_attributes"] == {
        "caliber.proposal_id": world.effects["complete"],
        "caliber.decision_id": decision["decision_id"],
        "caliber.effect_id": outcome["effect_id"],
    }


def test_an_exact_intent_effect_links_its_approval_and_says_its_decision_is_recorded_per_run(
    world: World,
) -> None:
    effect = _by_node(world)["exact"]
    approval = effect["approval"]
    assert approval["status"] == "linked" and approval["approval_id"] == "rap-occ-exact"
    assert approval["request_status"] == "approved" and approval["consumed_at"] is not None
    assert approval["intent_hash"].startswith("sha256:") and approval["evidence_hash"] == EVIDENCE
    assert (effect["decision"]["status"], effect["decision"]["code"]) == (
        "unrecorded",
        "decision_recorded_per_run_only",
    ), (
        "consumed before migration 0134 stored the decision id, so the pairing is not guessed from the run's decisions"
    )
    assert effect["decision"]["decision_id"] is None
    assert effect["missing_links"] == [], "nothing here is broken"
    assert effect["unrecorded_links"] == ["context_manifest", "decision"]
    assert effect["span_attributes"] == {
        "caliber.proposal_id": world.effects["exact"],
        "caliber.approval_id": "rap-occ-exact",
        "caliber.effect_id": effect["effect"]["effect_id"],
    }


def test_an_exact_intent_effect_consumed_since_the_decision_id_was_stored_links_its_decision(
    world: World,
) -> None:
    """B11.3c, #1061: the chain of a governed write is complete, the decision named by the consumed approval itself."""
    effect = _by_node(world)["exact_recorded"]
    decision = effect["decision"]
    assert decision["status"] == "linked" and decision["code"] is None
    assert decision["decision_id"] == "pd_commit_exact_recorded"
    assert (decision["action"], decision["result"]) == ("resource.execute", "allow")
    assert decision["reason_codes"] == ["grant_active", "role_ok"]
    assert (decision["policy_version"], decision["input_hash"]) == ("policy-v1", "2" * 64)
    assert effect["approval"]["status"] == "linked"
    assert effect["approval"]["approval_id"] == "rap-occ-exact-recorded"
    assert effect["missing_links"] == []
    assert effect["unrecorded_links"] == ["context_manifest"], (
        "the decision is no longer unrecorded"
    )
    assert effect["span_attributes"] == {
        "caliber.proposal_id": world.effects["exact_recorded"],
        "caliber.decision_id": "pd_commit_exact_recorded",
        "caliber.approval_id": "rap-occ-exact-recorded",
        "caliber.effect_id": effect["effect"]["effect_id"],
    }
    # the id is the persisted decision row the run lists, not a value made up from a neighbour
    listed = {
        item["decision_id"]: item
        for item in _page(world, section="policy_decisions", limit=service.MAX_LIMIT)[
            "policy_decisions"
        ]
    }
    assert listed["pd_commit_exact_recorded"]["input_hash"] == decision["input_hash"]
    # the sibling consumed before the id was stored is untouched: no pairing is guessed from the run's other decision
    legacy = _by_node(world)["exact"]["decision"]
    assert (legacy["status"], legacy["decision_id"]) == ("unrecorded", None)


@pytest.mark.parametrize(
    ("name", "approval", "decision", "effect"),
    [
        (
            "bare",
            ("pending", "approval_not_requested"),
            ("pending", "not_approved_yet"),
            ("pending", "not_dispatched"),
        ),
        (
            "asking",
            ("pending", "approval_not_consumed"),
            ("pending", "not_approved_yet"),
            ("pending", "not_dispatched"),
        ),
        (
            "dispatched",
            ("not_required", "approved_by_policy_decision"),
            ("linked", None),
            ("pending", "outcome_pending"),
        ),
        (
            "unknown",
            ("not_required", "approved_by_policy_decision"),
            ("linked", None),
            ("pending", "outcome_unknown"),
        ),
        (
            "failed",
            ("not_required", "failed_before_approval"),
            ("not_required", "failed_before_approval"),
            ("not_required", "failed_without_receipt"),
        ),
    ],
)
def test_missing_link_reported(
    world: World,
    name: str,
    approval: tuple[str, str],
    decision: tuple[str, str | None],
    effect: tuple[str, str],
) -> None:
    """An effect with no receipt yet has no ``effect_id``, with the reason; work in flight is ``pending`` and a
    settled effect that never needed the link is ``not_required``, so healthy work is not reported broken."""
    found = _by_node(world)[name]
    assert (found["approval"]["status"], found["approval"]["code"]) == approval
    assert (found["decision"]["status"], found["decision"]["code"]) == decision
    assert (found["effect"]["status"], found["effect"]["code"]) == effect
    assert (
        found["effect"]["effect_id"] is None and "caliber.effect_id" not in found["span_attributes"]
    )
    assert found["missing_links"] == [], "in-flight and failed-early work is not a broken chain"
    if name == "asking":
        assert found["approval"]["approval_id"] == "rap-asking", (
            "a pending request is named, not hidden"
        )
        assert (
            found["approval"]["request_status"] == "pending"
            and found["approval"]["consumed_at"] is None
        )
        assert "caliber.approval_id" not in found["span_attributes"], (
            "an unconsumed request is not on any span"
        )


def test_a_step_with_no_context_manifest_says_so_without_calling_the_chain_broken(
    world: World,
) -> None:
    for name in ("bare", "exact"):
        found = _by_node(world)[name]
        context = found["context_manifest"]
        assert (context["status"], context["code"]) == (
            "unrecorded",
            "no_context_manifest_for_node_step",
        )
        assert context["context_manifest_id"] is None
        assert "context_manifest" in found["unrecorded_links"]
        assert "context_manifest" not in found["missing_links"]


def test_acceptance_and_evidence_are_stated_absent_not_synthesized(world: World) -> None:
    page = _page(world)
    assert page["acceptance"] == {
        "status": "not_available",
        "code": "acceptance_record_not_available",
    }
    assert page["evidence_package"] == {
        "status": "not_available",
        "code": "evidence_package_not_available",
    }
    model = CausalLinksSchema.model_validate(page)
    assert (
        model.acceptance.status == "not_available"
        and model.evidence_package.status == "not_available"
    )


def test_a_legacy_run_has_no_manifest_and_says_so(world: World) -> None:
    manifest = _page(world)["run_manifest"]
    assert (manifest["status"], manifest["code"]) == ("legacy", "legacy_run_has_no_manifest")
    assert manifest["manifest_id"] is None and manifest["manifest_digest"] is None


def test_an_admitted_run_without_its_manifest_row_is_missing_and_one_with_it_is_linked(
    world: World,
) -> None:
    import hashlib

    from caliber.db.models import CaliberRunManifest

    with world.factory() as session:
        run = CaliberWorkflowRun(
            workflow_run_id="RUN-admitted",
            workflow_id="wf-ep",
            project_id="PRJ-ep",
            status="running",
            manifest_legacy=False,
        )
        session.add(run)
        session.flush()
        missing = read_causal_links(session, run)["run_manifest"]
        body = f"{CANARY}-manifest-body".encode()
        session.add(
            CaliberRunManifest(
                manifest_id="mf-admitted",
                workflow_run_id="RUN-admitted",
                schema_version="v1",
                manifest_bytes=body,
                manifest_digest=hashlib.sha256(body).hexdigest(),
            )
        )
        session.flush()
        linked = read_causal_links(session, run)["run_manifest"]
        session.rollback()
    assert missing == {"status": "missing", "code": "manifest_row_missing"}
    assert linked["status"] == "linked" and linked["manifest_id"] == "mf-admitted"
    assert linked["manifest_digest"] == hashlib.sha256(body).hexdigest()
    assert CANARY not in json.dumps(linked, default=str), "the manifest body is never read"


# ---------------------------------------------------------------- other runs' rows never appear


def test_a_run_reads_only_its_own_rows(world: World) -> None:
    mine = _page(world, limit=service.MAX_LIMIT)
    theirs = _page(world, OTHER_RUN, limit=service.MAX_LIMIT)
    assert world.other_effect not in {effect["proposal_id"] for effect in mine["effects"]}
    assert [effect["proposal_id"] for effect in theirs["effects"]] == [world.other_effect]
    other_decision = _page(world, section="policy_decisions", limit=service.MAX_LIMIT)
    assert world.other_decision not in {
        item["decision_id"] for item in other_decision["policy_decisions"]
    }
    theirs_decisions = _page(world, OTHER_RUN, section="policy_decisions", limit=service.MAX_LIMIT)
    assert [item["decision_id"] for item in theirs_decisions["policy_decisions"]] == [
        world.other_decision
    ]
    assert not _page(world, OTHER_RUN, section="context_manifests")["context_manifests"]


# ---------------------------------------------------------------- sections and pagination


def _all(world: World, section: str, key: str, id_key: str, limit: int) -> list[str]:
    seen: list[str] = []
    cursor = None
    for _ in range(50):
        page = _page(world, section=section, limit=limit, cursor=cursor)
        assert page["section"] == section and len(page[key]) <= limit
        seen += [item[id_key] for item in page[key]]
        for other in {"effects", "context_manifests", "policy_decisions"} - {section}:
            assert page[other] == [], "only the requested section is filled"
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return seen
        assert page["next_cursor"] and len(page[key]) == limit
        cursor = page["next_cursor"]
    raise AssertionError("pagination did not end")


@pytest.mark.parametrize("limit", [1, 2, 3, 100])
@pytest.mark.parametrize(
    ("section", "key", "id_key"),
    [
        ("effects", "effects", "proposal_id"),
        ("context_manifests", "context_manifests", "context_manifest_id"),
        ("policy_decisions", "policy_decisions", "decision_id"),
    ],
)
def test_keyset_pages_cover_every_row_once_in_a_stable_order(
    world: World, section: str, key: str, id_key: str, limit: int
) -> None:
    """Rows share timestamps (a server stamp has second resolution, every decision here is at one instant): the id breaks the tie."""
    whole = _all(world, section, key, id_key, service.MAX_LIMIT)
    paged = _all(world, section, key, id_key, limit)
    assert paged == whole and len(set(paged)) == len(paged) > 0
    expected = {"effects": 8, "context_manifests": 4, "policy_decisions": 5}[section]
    assert len(whole) == expected, "the second run's rows are not among them"


def test_the_context_manifest_section_lists_every_revision_with_digests_only(world: World) -> None:
    items = _page(world, section="context_manifests", limit=service.MAX_LIMIT)["context_manifests"]
    assert {item["context_manifest_id"] for item in items} == {
        "cm-step-ask-occ-complete-1-1",
        "cm-step-ask-occ-complete-2-1",
        "cm-step-ask-occ-complete-2-2",
        "cm-step-elsewhere-occ-none-1-1",
    }
    assert [item["created_at"] for item in items] == sorted(item["created_at"] for item in items)
    assert all(len(item["manifest_digest"]) == 64 for item in items)
    assert CANARY not in json.dumps(items, default=str), "no manifest body"


def test_the_decision_section_lists_the_run_decisions_including_the_commit_time_one(
    world: World,
) -> None:
    items = {
        item["decision_id"]: item
        for item in _page(world, section="policy_decisions", limit=service.MAX_LIMIT)[
            "policy_decisions"
        ]
    }
    run_level = items["pd_run_commit_time"]
    assert run_level["reason_codes"] == ["grant_active", "role_ok"]
    assert (run_level["action"], run_level["result"], run_level["resource"]) == (
        "resource.execute",
        "allow",
        "workflow:wf-ep",
    )
    assert run_level["input_hash"] == "1" * 64 and run_level["grant_id"] == "gr-1"
    # the decision an exact-intent approval read at commit time is on the run, and on no effect
    assert "caliber.decision_id" not in _by_node(world)["exact"]["span_attributes"]
    assert all(
        link["decision"]["decision_id"] != "pd_run_commit_time" for link in _by_node(world).values()
    )


def test_the_response_holds_identifiers_only(world: World) -> None:
    """Every column that can carry a payload, a resource, a person or prose holds the canary; none of it comes back."""
    texts = []
    for section in ("effects", "context_manifests", "policy_decisions"):
        page = _page(world, section=section, limit=service.MAX_LIMIT)
        texts.append(CausalLinksSchema.model_validate(page).model_dump_json())
    blob = "\n".join(texts)
    for secret in (CANARY, "123-45-6789", "reason in prose", "purpose", "cases/"):
        assert secret not in blob, secret
    assert "approver" not in blob and "requester" not in blob and "subject" not in blob


# ---------------------------------------------------------------- bounded and read-only


def _statements(world: World, **kwargs: Any) -> list[str]:
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(world.engine, "before_cursor_execute", record)
    try:
        _page(world, **kwargs)
    finally:
        event.remove(world.engine, "before_cursor_execute", record)
    return statements


def test_the_number_of_queries_does_not_depend_on_the_page_size(world: World) -> None:
    one = _statements(world, limit=1)
    many = _statements(world, limit=service.MAX_LIMIT)
    # the run manifest, the page, and one lookup each for contexts, approvals (two kinds), decisions and receipts
    assert len(many) <= 8, many
    assert len(many) - len(one) <= 3, "only lookups that have nothing to look up are skipped"
    assert set(one) | set(many) <= {"SELECT"}, "read-only: no insert, update, delete or flush"


def test_a_read_writes_nothing(world: World) -> None:
    with world.factory() as session:
        before = {
            model: session.query(model).count()
            for model in (
                CaliberEffectProposal,
                CaliberEffectReceipt,
                CaliberRuntimeApprovalRequest,
                CaliberPolicyDecision,
                CaliberContextManifest,
            )
        }
    for section in ("effects", "context_manifests", "policy_decisions"):
        _page(world, section=section, limit=2)
    with world.factory() as session:
        after = {model: session.query(model).count() for model in before}
    assert before == after


@pytest.mark.parametrize("limit", [0, -1, service.MAX_LIMIT + 1, True, "5", 2.0, None])
def test_a_limit_outside_the_bound_is_refused(world: World, limit: Any) -> None:
    with pytest.raises(ValueError, match="limit must be between 1 and 200"):
        _page(world, limit=limit)


def test_an_unknown_section_is_refused(world: World) -> None:
    with pytest.raises(ValueError, match="section must be one of"):
        _page(world, section="payloads")


# ---------------------------------------------------------------- cursors


def test_a_cursor_round_trips_and_only_a_canonical_one_decodes() -> None:
    cursor = encode_cursor("EP-abc123")
    assert decode_cursor(cursor) == "EP-abc123" and decode_cursor(None) is None
    for bad in (
        "",
        "!!!",
        cursor + "=",
        cursor[:-1] + "*",
        "a" * 300,
        "/w==",
        "gA",
        encode_cursor("x") + "x",
    ):
        with pytest.raises(CausalLinksCursorError, match="cursor is invalid"):
            decode_cursor(bad)
    # invalid UTF-8 inside valid base64, and a cursor that decodes to nothing
    with pytest.raises(CausalLinksCursorError):
        decode_cursor("_w")


def test_a_cursor_for_another_run_or_section_is_refused(world: World) -> None:
    with pytest.raises(CausalLinksCursorError):
        _page(world, cursor=encode_cursor(world.other_effect))
    with pytest.raises(CausalLinksCursorError):
        _page(world, cursor=encode_cursor("EP-never-existed"))
    first = _page(world, limit=1)
    with pytest.raises(CausalLinksCursorError):
        _page(world, section="policy_decisions", cursor=first["next_cursor"])
    # and one run's cursor never opens a page of the other
    with pytest.raises(CausalLinksCursorError):
        _page(world, OTHER_RUN, cursor=first["next_cursor"])


# ================================================================ the route

PREFIX = "/ajax-api/2.0/mlflow/caliber"
SECTIONS = ("effects", "context_manifests", "policy_decisions")
COUNTED = (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberRuntimeApprovalRequest,
    CaliberPolicyDecision,
    CaliberContextManifest,
    CaliberWorkflowRun,
)


def _url(run_id: str = RUN) -> str:
    return f"{PREFIX}/workflow-runs/{run_id}/causal-links"


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> World:
    """The same run, on the application fixture's database (no migration triggers: rows can be bent for the negative cases)."""
    effects, other, other_decision = _seed(session_factory)
    return World(session_factory, None, effects, other, other_decision)


def _member(factory: sessionmaker[Session], user: str, role: str, status: str = "active") -> None:
    """``user`` joins the project, whose workflow is visible to its members (and so are its runs)."""
    from caliber.db.models import CaliberProjectMember, CaliberWorkflow

    with factory() as session:
        workflow = session.get(CaliberWorkflow, "wf-ep")
        assert workflow is not None
        workflow.visibility = "project"
        session.add(
            CaliberProjectMember(
                member_id=f"M-{user}-{role}",
                project_id="PRJ-ep",
                user_id=user,
                role=role,
                status=status,
                created_by="@test",
            )
        )
        session.commit()


def _as(user: str) -> dict[str, str]:
    """A caller acting in the run's project."""
    return {"X-CALIBER-User": user, "X-CALIBER-Project": "PRJ-ep"}


def _counts(factory: sessionmaker[Session]) -> list[int]:
    with factory() as session:
        return [session.query(model).count() for model in COUNTED]


def test_the_route_returns_the_typed_page_for_the_runs_project_owner(
    client: Any, seeded: World
) -> None:
    response = client.get(_url())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == set(CausalLinksSchema.model_fields), "exactly the declared fields"
    assert data["workflow_run_id"] == RUN and data["project_id"] == "PRJ-ep"
    assert data["section"] == "effects" and data["limit"] == service.DEFAULT_LIMIT
    assert data["has_more"] is False and data["next_cursor"] is None
    assert {effect["proposal_id"] for effect in data["effects"]} == set(seeded.effects.values())
    complete = next(e for e in data["effects"] if e["proposal_id"] == seeded.effects["complete"])
    assert complete["effect"]["status"] == "linked" and complete["missing_links"] == []
    for key in ("context_manifests", "policy_decisions"):
        assert data[key] == []


def test_foreign_run_404(client: Any, seeded: World) -> None:
    """Another project's caller, an outsider who can see the workflow, and an unknown id all get one and the same 404."""
    from caliber.db.models import CaliberWorkflow

    stranger_headers = {"X-CALIBER-User": "@mallory"}
    missing = client.get(_url("WR-none"), headers=stranger_headers)
    assert missing.status_code == 404
    stranger = client.get(_url(), headers=stranger_headers)
    claiming = client.get(_url(), headers={**stranger_headers, "X-CALIBER-Project": "PRJ-ep"})
    with seeded.factory() as session:
        workflow = session.get(CaliberWorkflow, "wf-ep")
        assert workflow is not None
        workflow.visibility = "public"
        session.commit()
    seen = client.get(f"{PREFIX}/workflow-runs/{RUN}", headers=stranger_headers)
    assert seen.status_code == 200, "the run itself is visible through the public workflow"
    outsider = client.get(_url(), headers=stranger_headers)
    shapes = {
        response.json()["detail"].replace(run, "X")
        for response, run in (
            (missing, "WR-none"),
            (stranger, RUN),
            (claiming, RUN),
            (outsider, RUN),
        )
    }
    assert {r.status_code for r in (missing, stranger, claiming, outsider)} == {404}
    assert len(shapes) == 1, shapes
    # nothing the caller supplies widens it: not a project in the query, not the run in another spelling
    sneaky = client.get(
        f"{_url()}?project_id=PRJ-ep&workflow_run_id={RUN}", headers=stranger_headers
    )
    assert sneaky.status_code == 404
    # and a foreign caller gets the 404 before any complaint about the parameters
    bad = client.get(f"{_url()}?limit=0&section=x&cursor=%", headers=stranger_headers)
    assert bad.status_code == 404


@pytest.mark.parametrize("role", ["viewer", "reviewer", "editor"])
def test_every_project_role_reads_the_ids(client: Any, seeded: World, role: str) -> None:
    _member(seeded.factory, f"@{role}", role)
    response = client.get(_url(), headers=_as(f"@{role}"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["effects"]


def test_the_callers_current_membership_decides_not_the_membership_they_had(
    client: Any, seeded: World
) -> None:
    from caliber.db.models import CaliberProjectMember

    _member(seeded.factory, "@temp", "viewer")
    headers = _as("@temp")
    assert client.get(_url(), headers=headers).status_code == 200
    with seeded.factory() as session:
        member = session.query(CaliberProjectMember).filter_by(user_id="@temp").one()
        member.status = "inactive"
        session.commit()
    gone = client.get(_url(), headers=headers)
    assert gone.status_code == 404
    assert gone.json()["detail"] == f"workflow run {RUN!r} not found"


def test_a_platform_admin_with_no_role_in_the_project_still_gets_the_same_404(
    client: Any, seeded: World
) -> None:
    """``caliber.admin`` sees every workflow and its runs, but it is not a project role: the ids are project evidence."""
    admin = client.get(f"{PREFIX}/workflow-runs/{RUN}", headers={"X-CALIBER-User": "@admin"})
    assert admin.status_code == 200, "visibility alone would let the admin through"
    denied = client.get(_url(), headers={"X-CALIBER-User": "@admin"})
    assert denied.status_code == 404
    assert denied.json()["detail"] == f"workflow run {RUN!r} not found"


def test_a_run_with_no_project_is_gated_by_its_workflows_visibility(
    client: Any, session_factory: sessionmaker[Session]
) -> None:
    from caliber.db.models import CaliberWorkflow

    with session_factory() as session:
        session.add(CaliberWorkflow(workflow_id="wf-solo", name="Solo", owner="@test"))
        session.add(
            CaliberWorkflowRun(
                workflow_run_id="RUN-solo", workflow_id="wf-solo", status="completed"
            )
        )
        session.commit()
    mine = client.get(_url("RUN-solo"))
    assert mine.status_code == 200 and mine.json()["data"]["project_id"] is None
    assert mine.json()["data"]["effects"] == []
    other = client.get(_url("RUN-solo"), headers={"X-CALIBER-User": "@mallory"})
    assert other.status_code == 404


def test_the_route_is_read_only(client: Any, seeded: World) -> None:
    before = _counts(seeded.factory)
    for section in SECTIONS:
        assert client.get(_url(), params={"section": section}).status_code == 200
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(_url()).status_code == 405
    assert _counts(seeded.factory) == before


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("section=payloads", "section must be one of effects, context_manifests, policy_decisions"),
        ("limit=0", "limit must be between 1 and 200"),
        ("limit=201", "limit must be between 1 and 200"),
        ("limit=many", "limit must be an integer"),
        ("cursor=%25%25", "cursor is invalid"),
        ("cursor=" + "A" * 400, "cursor is invalid"),
        ("cursor=" + encode_cursor("EP-unknown"), "cursor is invalid"),
    ],
)
def test_a_bad_parameter_is_a_400_that_names_the_rule(
    client: Any, seeded: World, query: str, message: str
) -> None:
    response = client.get(f"{_url()}?{query}")
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == message


def test_the_route_pages_with_the_cursor_it_returns(client: Any, seeded: World) -> None:
    seen: list[str] = []
    url = f"{_url()}?limit=3"
    for _ in range(5):
        page = client.get(url).json()["data"]
        seen += [effect["proposal_id"] for effect in page["effects"]]
        if not page["has_more"]:
            break
        url = f"{_url()}?limit=3&cursor={page['next_cursor']}"
    assert len(set(seen)) == len(seen) == 8
    assert sorted(seen) == sorted(seeded.effects.values())


def test_the_route_holds_identifiers_only(client: Any, seeded: World) -> None:
    secrets = (
        CANARY,
        "123-45-6789",
        "reason in prose",
        "cases/",
        "approver",
        "requester",
        "subject",
    )
    for section in SECTIONS:
        text = client.get(_url(), params={"section": section, "limit": 200}).text
        for secret in secrets:
            assert secret not in text, (section, secret)


def test_an_id_stored_on_one_run_never_pulls_in_another_runs_row(
    client: Any, seeded: World
) -> None:
    """Even if a proposal named a decision or an approval of a different run, that row is not read for it."""
    with seeded.factory() as session:
        proposals = (
            CaliberEffectProposal.__table__
        )  # Core: the ORM refuses to move an immutable row
        session.execute(
            update(proposals)
            .where(proposals.c.proposal_id == seeded.effects["complete"])
            .values(approval_ref=seeded.other_decision)
        )
        _approval_row(
            session,
            seeded.other_effect,
            status="approved",
            approval_id="rap-other-run",
            run=OTHER_RUN,
        )
        session.execute(
            update(proposals)
            .where(proposals.c.proposal_id == seeded.effects["exact"])
            .values(approval_ref="rap-other-run")
        )
        session.commit()
    response = client.get(_url(), params={"limit": 200})
    by_id = {effect["proposal_id"]: effect for effect in response.json()["data"]["effects"]}
    decision = by_id[seeded.effects["complete"]]["decision"]
    assert (decision["status"], decision["code"], decision["decision_id"]) == (
        "missing",
        "decision_row_missing",
        None,
    )
    approval = by_id[seeded.effects["exact"]]["approval"]
    assert (approval["status"], approval["code"], approval["approval_id"]) == (
        "missing",
        "approval_row_missing",
        None,
    )
    assert "rap-other-run" not in response.text and seeded.other_decision not in response.text


def _effect_and_body(client: Any, proposal_id: str) -> tuple[dict[str, Any], str]:
    response = client.get(_url(), params={"limit": 200})
    effects = {e["proposal_id"]: e for e in response.json()["data"]["effects"]}
    return effects[proposal_id], response.text


def _set_approval(factory: sessionmaker[Session], approval_id: str, **values: Any) -> None:
    """Bend a row of the unmigrated application database (no triggers there), through Core."""
    with factory() as session:
        session.execute(
            update(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == approval_id)
            .values(**values)
        )
        session.commit()


def test_a_stored_commit_decision_id_that_names_another_runs_decision_is_not_followed(
    client: Any, seeded: World
) -> None:
    """The decision lookup is filtered by the run like the approval lookup: a foreign id is a broken link, never a row."""
    _set_approval(
        seeded.factory, "rap-occ-exact-recorded", commit_decision_id=seeded.other_decision
    )
    effect, body = _effect_and_body(client, seeded.effects["exact_recorded"])
    decision = effect["decision"]
    assert (decision["status"], decision["code"], decision["decision_id"]) == (
        "missing",
        "decision_row_missing",
        None,
    )
    assert effect["missing_links"] == ["decision"]
    assert "caliber.decision_id" not in effect["span_attributes"]
    assert seeded.other_decision not in body


def test_a_stored_commit_decision_id_that_names_no_decision_is_a_broken_link(
    client: Any, seeded: World
) -> None:
    _set_approval(seeded.factory, "rap-occ-exact-recorded", commit_decision_id="pd-vanished")
    effect, body = _effect_and_body(client, seeded.effects["exact_recorded"])
    assert effect["decision"]["status"] == "missing"
    assert effect["missing_links"] == ["decision"]
    assert effect["unrecorded_links"] == ["context_manifest"]
    assert "pd-vanished" not in body


def test_the_commit_decision_of_an_approval_bound_to_another_proposal_is_not_followed(
    client: Any, seeded: World
) -> None:
    """The decision id counts only through the effect's own approval request, as the approval link does."""
    _set_approval(seeded.factory, "rap-occ-exact-recorded", proposal_id=seeded.effects["bare"])
    effect, _ = _effect_and_body(client, seeded.effects["exact_recorded"])
    assert effect["approval"]["status"] == "missing"
    assert (effect["decision"]["status"], effect["decision"]["code"]) == (
        "unrecorded",
        "decision_recorded_per_run_only",
    )
    assert "caliber.decision_id" not in effect["span_attributes"]


def test_a_committed_effect_whose_receipt_row_is_gone_is_reported_missing(
    client: Any, seeded: World
) -> None:
    """Nothing is filled in from the proposal's state: a committed proposal with no receipt row has no ``effect_id``."""
    receipts = CaliberEffectReceipt.__table__
    with seeded.factory() as session:
        session.execute(
            receipts.delete().where(receipts.c.proposal_id == seeded.effects["complete"])
        )
        session.commit()
    effects = {e["proposal_id"]: e for e in client.get(_url()).json()["data"]["effects"]}
    outcome = effects[seeded.effects["complete"]]["effect"]
    assert (outcome["status"], outcome["code"]) == ("missing", "receipt_row_missing")
    assert outcome["effect_id"] is None and outcome["committed"] is False
    assert "caliber.effect_id" not in effects[seeded.effects["complete"]]["span_attributes"]
    assert "effect" in effects[seeded.effects["complete"]]["missing_links"]


def test_the_receipt_that_settled_a_proposal_is_the_one_named_even_when_a_later_attempt_exists(
    client: Any, seeded: World
) -> None:
    import hashlib

    with seeded.factory() as session:
        proposal = session.get(CaliberEffectProposal, seeded.effects["exact"])
        assert proposal is not None
        settled = proposal.receipt_id
        body = b"a later attempt that did not settle the proposal"
        session.add(
            CaliberEffectReceipt(
                receipt_id="rcpt-later-attempt",
                proposal_id=proposal.proposal_id,
                attempt=2,
                logical_key=proposal.logical_key,
                intent_hash=proposal.intent_hash,
                connector=proposal.connector,
                connector_version=proposal.connector_version,
                resource=proposal.resource,
                downstream_request_id="DR-later-attempt",
                response_digest="sha256:" + "c" * 64,
                verified_by="query",
                verified_at=NAIVE_AT,
                receipt_bytes=body,
                receipt_digest=hashlib.sha256(body).hexdigest(),
            )
        )
        session.commit()
    effects = {e["proposal_id"]: e for e in client.get(_url()).json()["data"]["effects"]}
    outcome = effects[seeded.effects["exact"]]["effect"]
    assert outcome["effect_id"] == settled and outcome["attempt"] == 1
    assert outcome["committed"] is True
    assert effects[seeded.effects["exact"]]["span_attributes"]["caliber.effect_id"] == settled


def test_reason_codes_are_strings_and_bounded() -> None:
    assert service._codes(["grant_active", 7, None, "role_ok"]) == ["grant_active", "role_ok"]
    assert service._codes({"not": "a list"}) == []
    assert service._codes(None) == []
    many = [f"code_{number}" for number in range(service.MAX_REASON_CODES + 10)]
    assert service._codes(many) == many[: service.MAX_REASON_CODES]


def test_a_receipt_id_stored_on_one_proposal_never_attaches_another_proposals_receipt(
    client: Any, seeded: World
) -> None:
    """``complete`` is made to name the receipt that settled ``exact``: it keeps its own latest receipt, uncommitted."""
    with seeded.factory() as session:
        proposals = CaliberEffectProposal.__table__
        theirs = session.execute(
            select(proposals.c.receipt_id).where(proposals.c.proposal_id == seeded.effects["exact"])
        ).scalar_one()
        session.execute(
            update(proposals)
            .where(proposals.c.proposal_id == seeded.effects["complete"])
            .values(receipt_id=theirs)
        )
        session.commit()
    effects = {e["proposal_id"]: e for e in client.get(_url()).json()["data"]["effects"]}
    own = effects[seeded.effects["complete"]]["effect"]
    assert own["status"] == "linked" and own["effect_id"] != theirs
    assert own["committed"] is False, "the proposal's recorded receipt is not the one found for it"
    assert effects[seeded.effects["exact"]]["effect"]["effect_id"] == theirs
    assert effects[seeded.effects["exact"]]["effect"]["committed"] is True


@pytest.mark.parametrize(
    ("section", "key", "total"),
    [
        ("effects", "effects", 8),
        ("context_manifests", "context_manifests", 4),
        ("policy_decisions", "policy_decisions", 5),
    ],
)
def test_has_more_is_exact_at_the_page_boundary(
    world: World, section: str, key: str, total: int
) -> None:
    """A page that ends exactly at the last row says so; one row short says there is more."""
    exact = _page(world, section=section, limit=total)
    assert len(exact[key]) == total and exact["has_more"] is False and exact["next_cursor"] is None
    short = _page(world, section=section, limit=total - 1)
    assert len(short[key]) == total - 1 and short["has_more"] is True and short["next_cursor"]
    rest = _page(world, section=section, limit=total, cursor=short["next_cursor"])
    assert len(rest[key]) == 1 and rest["has_more"] is False and rest["next_cursor"] is None


def test_an_approval_of_this_proposal_that_belongs_to_another_run_is_not_followed(
    client: Any, seeded: World
) -> None:
    """The row is bound to the right proposal but sits in another run: the run filter, not the proposal check, refuses it."""
    proposals = CaliberEffectProposal.__table__
    with seeded.factory() as session:
        _approval_row(
            session,
            seeded.effects["exact"],
            status="approved",
            approval_id="rap-wrong-run",
            run=OTHER_RUN,
        )
        session.execute(
            update(proposals)
            .where(proposals.c.proposal_id == seeded.effects["exact"])
            .values(approval_ref="rap-wrong-run")
        )
        session.commit()
    response = client.get(_url(), params={"limit": 200})
    effects = {e["proposal_id"]: e for e in response.json()["data"]["effects"]}
    approval = effects[seeded.effects["exact"]]["approval"]
    assert (approval["status"], approval["code"], approval["approval_id"]) == (
        "missing",
        "approval_row_missing",
        None,
    )
    assert "rap-wrong-run" not in response.text


def test_the_live_join_from_a_stored_context_manifest_to_its_proposal(
    client: Any, seeded: World
) -> None:
    """A manifest written by the real store and a proposal written by the real recorder join on ``node_step_id``.

    Nothing here seeds ``node_step_id`` through ``node_step_id_for``: the store derives it from the record's ``step``, so a
    change in either side's format would break the join and fail this test. The latest attempt of the occurrence is linked.
    """
    from datetime import datetime, timezone

    from caliber.context.manifest import (
        ContextSegment,
        build_context_manifest,
        store_context_manifest,
    )
    from caliber.run_manifest import DeploymentScope

    with seeded.factory() as session:
        proposal_id = _at(session, PROPOSED, occurrence="occ-live-join")
        ids = {}
        for attempt in (1, 2):
            built = build_context_manifest(
                context_manifest_id=f"cm-live-{attempt}",
                workflow_run_id=RUN,
                step_request_id=f"sr-live-{attempt}",
                revision=1,
                scope=DeploymentScope.fixed("PRJ-ep"),
                segments=[
                    ContextSegment(
                        tier="instruction",
                        kind="workflow_instruction",
                        source={"kind": "prompt", "reference": "support@4"},
                        content="You answer support questions.",
                        tokens=10,
                    )
                ],
                token_budget=100,
                created_at=datetime(2026, 10, 27, 12, 0, attempt, tzinfo=timezone.utc),
                step={"node_id": "ask", "occurrence_id": "occ-live-join", "attempt": attempt},
            )
            row = store_context_manifest(session, built)
            ids[attempt] = (row.context_manifest_id, row.manifest_digest)
        session.commit()
    effects = {
        e["proposal_id"]: e
        for e in client.get(_url(), params={"limit": 200}).json()["data"]["effects"]
    }
    context = effects[proposal_id]["context_manifest"]
    assert context["status"] == "linked" and context["attempt"] == 2
    assert (context["context_manifest_id"], context["manifest_digest"]) == ids[2]
    assert effects[proposal_id]["node_step_id"] == f"{RUN}:ask:occ-live-join"
    listed = client.get(_url(), params={"section": "context_manifests", "limit": 200}).json()[
        "data"
    ]
    stored = {m["context_manifest_id"]: m for m in listed["context_manifests"]}
    assert stored["cm-live-1"]["node_step_id"] == effects[proposal_id]["node_step_id"]


def test_an_approval_that_names_another_proposal_is_not_followed(
    client: Any, seeded: World
) -> None:
    with seeded.factory() as session:
        session.execute(
            update(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == "rap-occ-exact")
            .values(proposal_id=seeded.effects["bare"])
        )
        session.commit()
    effects = {e["proposal_id"]: e for e in client.get(_url()).json()["data"]["effects"]}
    assert effects[seeded.effects["exact"]]["approval"]["status"] == "missing"


def test_the_operation_is_typed_in_the_openapi_document(client: Any) -> None:
    from caliber.routes.openapi import PREFIX as API_PREFIX
    from caliber.routes.openapi import build_openapi_document

    document = build_openapi_document(client.app)
    operation = document["paths"][f"{API_PREFIX}/workflow-runs/{{run_id}}/causal-links"]["get"]
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    data = ok["properties"]["data"]
    assert data["title"] == "CausalLinksSchema" and data["additionalProperties"] is False
    assert set(data["properties"]) == set(CausalLinksSchema.model_fields)
    assert data["properties"]["effects"]["items"] == {
        "$ref": "#/components/schemas/CausalEffectSchema"
    }
    effect = document["components"]["schemas"]["CausalEffectSchema"]
    assert effect["additionalProperties"] is False and "span_attributes" in effect["properties"]
    assert {"400", "401", "404"} <= set(operation["responses"])


# ---------------------------------------------------------------- the SDK against this server


def test_the_sdk_models_name_every_field_the_server_sends() -> None:
    """A field added to the response without its SDK model (or the other way round) is drift, caught here."""
    import dataclasses

    sdk = pytest.importorskip("caliber_sdk.models.workflows", reason="caliber-sdk is not installed")
    from caliber import schemas

    pairs = {
        schemas.CausalLinksSchema: sdk.CausalLinks,
        schemas.CausalRunManifestLinkSchema: sdk.CausalRunManifestLink,
        schemas.CausalAbsentRecordSchema: sdk.CausalAbsentRecord,
        schemas.CausalContextManifestSchema: sdk.CausalContextManifest,
        schemas.CausalPolicyDecisionSchema: sdk.CausalPolicyDecision,
        schemas.CausalContextLinkSchema: sdk.CausalContextLink,
        schemas.CausalApprovalLinkSchema: sdk.CausalApprovalLink,
        schemas.CausalDecisionLinkSchema: sdk.CausalDecisionLink,
        schemas.CausalEffectOutcomeLinkSchema: sdk.CausalEffectOutcomeLink,
        schemas.CausalEffectSchema: sdk.CausalEffect,
    }
    for schema, model in pairs.items():
        fields = {field.name for field in dataclasses.fields(model)} - {"extra"}
        assert fields == set(schema.model_fields), schema.__name__


def test_the_sdk_reads_the_real_server_page_by_page(client: Any, seeded: World) -> None:
    caliber_sdk = pytest.importorskip("caliber_sdk", reason="caliber-sdk is not installed")
    sdk = caliber_sdk.CaliberClient("http://testserver", http_client=client)

    seen: list[Any] = []
    cursor = None
    for _ in range(10):
        page = sdk.workflows.runs.causal_links(RUN, limit=3, cursor=cursor)
        seen += page.effects
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert sorted(effect.proposal_id for effect in seen) == sorted(seeded.effects.values())
    complete = next(e for e in seen if e.proposal_id == seeded.effects["complete"])
    assert complete.effect.status == "linked" and complete.effect.committed is True
    assert complete.decision.status == "linked" and complete.missing_links == []
    exact = next(e for e in seen if e.proposal_id == seeded.effects["exact"])
    assert (exact.approval.status, exact.decision.status) == ("linked", "unrecorded")
    assert exact.span_attributes["caliber.approval_id"] == "rap-occ-exact"
    bare = next(e for e in seen if e.proposal_id == seeded.effects["bare"])
    assert bare.effect.effect_id is None and "caliber.effect_id" not in bare.span_attributes
    assert page.acceptance.status == "not_available"

    decisions = sdk.workflows.runs.causal_links(RUN, section="policy_decisions", limit=200)
    assert "pd_run_commit_time" in {item.decision_id for item in decisions.policy_decisions}
    contexts = sdk.workflows.runs.causal_links(RUN, section="context_manifests")
    assert len(contexts.context_manifests) == 4


def test_the_sdk_gets_a_typed_not_found_for_a_run_it_may_not_read(
    client: Any, seeded: World
) -> None:
    caliber_sdk = pytest.importorskip("caliber_sdk", reason="caliber-sdk is not installed")
    with TestClient(client.app, headers={"X-CALIBER-User": "@mallory"}) as stranger:
        sdk = caliber_sdk.CaliberClient("http://testserver", http_client=stranger)
        with pytest.raises(caliber_sdk.CaliberNotFoundError) as foreign:
            sdk.workflows.runs.causal_links(RUN)
        with pytest.raises(caliber_sdk.CaliberNotFoundError) as unknown:
            sdk.workflows.runs.causal_links("WFR-none")
    import re

    def shape(error: BaseException, run: str) -> str:
        return re.sub(r"request_id=\w+", "", str(error)).replace(run, "X")

    assert shape(foreign.value, RUN) == shape(unknown.value, "WFR-none")
