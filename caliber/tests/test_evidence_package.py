"""B14.2 authorized evidence-package assembly."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.contracts import (
    ContractValidationError,
    evidence_package_digest,
    validate_record,
)
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberContextManifest,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberProject,
    CaliberRunManifest,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.evidence import (
    EvidencePackage,
    EvidencePackageAuthorizationError,
    EvidencePackageError,
    EvidencePackageNotFoundError,
    assemble,
)
from caliber.evidence.package import (
    _acceptance_provenance,
    _approval_decision_payload,
    _approval_payload,
    _authorize_run,
    _bytes_digest,
    _canonical_bytes,
    _missing,
    _policy_payload,
    _timestamp,
)

RUN_ID = "run-evidence-1"
PROJECT_ID = "project-evidence-1"
OWNER = "@evidence-owner"
NOW = datetime(2026, 11, 3, 9, 0, 0)
IDENTITY = CaliberIdentity(
    user_id=OWNER,
    scopes=frozenset({"caliber.operator", "caliber.approver"}),
    active_project_id=PROJECT_ID,
)


def _setup_run(
    session: Session,
    *,
    with_proposal: bool = True,
    with_manifest: bool = True,
    run_id: str = RUN_ID,
    project_id: str = PROJECT_ID,
    created_by: str = OWNER,
    project_owner: str = OWNER,
    projectless: bool = False,
    legacy: bool = False,
    approval_kind: str | None = None,
    approval_ref: str | None = None,
) -> None:
    session.add(
        CaliberProject(
            project_id=project_id,
            name=f"Evidence project {project_id}",
            owner=project_owner,
        )
    )
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id="workflow-evidence-1",
            project_id=None if projectless else project_id,
            tenant_id="tenant-evidence",
            created_by=created_by,
            manifest_legacy=legacy,
            status="completed",
        )
    )
    manifest_bytes = b'{"manifest":"run-evidence-1"}'
    if with_manifest:
        session.add(
            CaliberRunManifest(
                manifest_id=f"manifest-{run_id}",
                workflow_run_id=run_id,
                schema_version="v1",
                manifest_bytes=manifest_bytes,
                manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
                created_at=NOW,
            )
        )
    if with_proposal:
        intent_bytes = b'{"connector":"document-request","resource":"case-1"}'
        session.add(
            CaliberEffectProposal(
                proposal_id=f"proposal-{run_id}",
                tenant_id="tenant-evidence",
                workflow_run_id=run_id,
                node_id="node-1",
                occurrence_id="occurrence-1",
                logical_key="a" * 64,
                connector="document-request",
                connector_version="v1",
                effect_class="reversible_write",
                resource="case-1",
                args={},
                expected_resource_version="1",
                intent_algorithm="sha256",
                intent_bytes=intent_bytes,
                intent_hash="sha256:" + hashlib.sha256(intent_bytes).hexdigest(),
                state="committed",
                version=1,
                created_at=NOW,
                settled_at=NOW,
                approval_kind=approval_kind,
                approval_ref=approval_ref,
            )
        )
    session.commit()


def test_assemble_complete_membership_and_server_scope(db_session: Session) -> None:
    _setup_run(db_session)

    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)

    assert package.workflow_run_id == RUN_ID
    assert package.record["tenant_id"] == "tenant-evidence"
    assert package.record["region"] == "local"
    assert package.record["project_id"] == PROJECT_ID
    assert package.record["view"]["authorization"] == "authorized"
    assert [member["member_id"] for member in package.record["members"]] == sorted(
        member["member_id"] for member in package.record["members"]
    )
    proposal = next(member for member in package.record["members"] if member["kind"] == "proposal")
    assert proposal["parent_ids"] == ["run_manifest:manifest-run-evidence-1"]
    assert any(
        link["link"] == "receipt" and link["status"] == "missing"
        for link in package.record["missing_links"]
    )
    validate_record("evidence_package", package.record)
    assert package.package_sha256 == evidence_package_digest(package.record)
    assert package.package_bytes == _canonical_bytes(package.record)


def test_missing_receipt_is_explicit_and_payload_bytes_are_not_copied(db_session: Session) -> None:
    _setup_run(db_session)

    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    proposal = next(member for member in package.record["members"] if member["kind"] == "proposal")

    assert "intent" not in proposal["metadata"]
    assert all("case-1" not in str(member["metadata"]) for member in package.record["members"])
    receipt_link = next(
        link for link in package.record["missing_links"] if link["link"] == "receipt"
    )
    assert receipt_link == {
        "link": "receipt",
        "status": "missing",
        "code": "receipt_row_missing",
        "expected_member_id": "receipt:proposal-run-evidence-1",
    }


def test_member_id_metadata_and_view_tampering_changes_digest(db_session: Session) -> None:
    _setup_run(db_session)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)

    tampered = dict(package.record)
    tampered["members"] = [dict(member) for member in package.record["members"]]
    tampered["members"][0] = dict(tampered["members"][0])
    tampered["members"][0]["metadata"] = dict(tampered["members"][0]["metadata"])
    tampered["members"][0]["metadata"]["state"] = "tampered"
    with pytest.raises(ContractValidationError, match="metadata_sha256|package_sha256"):
        validate_record("evidence_package", tampered)

    wrong_view = dict(package.record)
    wrong_view["view"] = {"authorization": "authorized", "redaction": "redacted"}
    with pytest.raises(ContractValidationError, match="package_sha256"):
        validate_record("evidence_package", wrong_view)


def test_foreign_project_is_refused_before_assembly(db_session: Session) -> None:
    _setup_run(db_session)
    foreign = CaliberIdentity(
        user_id="@other",
        scopes=frozenset({"caliber.operator", "caliber.approver"}),
        active_project_id="project-other",
    )

    with pytest.raises(EvidencePackageNotFoundError, match="not found"):
        assemble(db_session, run_id=RUN_ID, identity=foreign)


def test_unknown_run_is_refused_without_leaking_existence(db_session: Session) -> None:
    with pytest.raises(EvidencePackageNotFoundError, match="not found"):
        assemble(db_session, run_id="foreign-run", identity=IDENTITY)


def test_canonical_helpers_and_public_package_properties() -> None:
    package = EvidencePackage(
        record={"package_id": "ep-1", "workflow_run_id": RUN_ID},
        package_bytes=b"{}",
        package_sha256="d" * 64,
    )

    assert package.package_id == "ep-1"
    assert package.workflow_run_id == RUN_ID
    assert package.manifest is package.record
    assert package.digest == "d" * 64
    assert _timestamp(None) is None
    assert _timestamp(NOW) == "2026-11-03T09:00:00.000000Z"
    assert _timestamp(NOW.replace(tzinfo=timezone.utc)) == ("2026-11-03T09:00:00.000000Z")
    assert _canonical_bytes({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'
    assert _bytes_digest(bytearray(b"bytes")) == hashlib.sha256(b"bytes").hexdigest()
    assert _missing("receipt", "pending", "outcome_pending") == {
        "link": "receipt",
        "status": "pending",
        "code": "outcome_pending",
    }


def test_contract_payload_helpers_cover_safe_defaults() -> None:
    policy = CaliberPolicyDecision(
        decision_id="decision-helper",
        workflow_run_id=RUN_ID,
        grant_id="grant-helper",
        subject_user_id=OWNER,
        workload_id="workload-helper",
        action="resource.read",
        resource="resource-helper",
        purpose="test",
        result="allow",
        reason_codes=None,
        policy_version="policy-v1",
        input_hash="a" * 64,
        evaluated_at=NOW,
    )
    assert _policy_payload(policy)["reason_codes"] == []

    request = CaliberRuntimeApprovalRequest(
        runtime_approval_id="approval-helper",
        workflow_run_id=RUN_ID,
        node_id="node-1",
        status="pending",
        requested_at=NOW,
        policy_snapshot={"allowed_responses": ("reject", 7, "approve"), "risk_class": "high"},
        expires_at=NOW + timedelta(hours=1),
        requested_by=None,
    )
    payload = _approval_payload(request)
    assert payload["allowed_responses"] == ["approve", "reject"]
    assert payload["risk_class"] == "high"
    assert payload["requested_by"] == "unknown"

    request.policy_snapshot = {"allowed_responses": [], "risk_class": ""}
    assert _approval_payload(request)["allowed_responses"] == ["approve", "reject"]
    assert _approval_payload(request)["risk_class"] == "standard"
    request.policy_snapshot = None
    assert _approval_payload(request)["allowed_responses"] == ["approve", "reject"]

    for status, expected in (
        ("approved", "approve"),
        ("rejected", "reject"),
        ("request_changes", "request_changes"),
        ("unexpected", "reject"),
    ):
        request.status = status
        request.decided_at = NOW
        request.decided_by = None
        request.decision_reason = None
        decision = _approval_decision_payload(request)
        assert decision["decision"] == expected
        assert decision["decided_by"] == "unknown"
        assert decision["reason"] == ""


def test_acceptance_provenance_is_deterministic_and_empty_when_unrecorded() -> None:
    assert _acceptance_provenance([]) == []
    first = CaliberAcceptanceDecision(
        decision_id="decision-b",
        workflow_run_id=RUN_ID,
        candidate_id="proposal-evidence-1",
        criteria_version="criteria-v1",
        observation_digest="b" * 64,
        outcome="accepted",
        criteria=[{"predicate_id": "p", "passed": True}],
        criteria_total=1,
        criteria_failed=0,
        reason_code=None,
        reason_detail=None,
        decided_at=NOW,
    )
    second = CaliberAcceptanceDecision(
        decision_id="decision-a",
        workflow_run_id=RUN_ID,
        candidate_id="proposal-evidence-2",
        criteria_version="criteria-v1",
        observation_digest="c" * 64,
        outcome="rejected",
        criteria=None,
        criteria_total=1,
        criteria_failed=1,
        reason_code="criterion_failed",
        reason_detail="safe detail",
        decided_at=NOW,
    )
    provenance = _acceptance_provenance([first, second])
    assert [entry["input_sha256"] for entry in provenance] == ["c" * 64, "b" * 64]
    assert all(entry["provider"] == "caliber.acceptance" for entry in provenance)
    assert all(len(entry["result_sha256"]) == 64 for entry in provenance)


def test_authorization_refuses_invalid_and_foreign_identities(db_session: Session) -> None:
    _setup_run(db_session)
    run = db_session.get(CaliberWorkflowRun, RUN_ID)
    assert run is not None

    with pytest.raises(EvidencePackageAuthorizationError, match="CaliberIdentity"):
        _authorize_run(db_session, run, object())  # type: ignore[arg-type]

    foreign = CaliberIdentity(
        user_id="@foreign",
        scopes=frozenset({"caliber.operator", "caliber.approver"}),
        active_project_id=PROJECT_ID,
    )
    with pytest.raises(EvidencePackageNotFoundError, match="not found"):
        _authorize_run(db_session, run, foreign)


def test_projectless_authorization_is_narrow_and_assembly_refuses_it(db_session: Session) -> None:
    _setup_run(
        db_session,
        with_manifest=False,
        run_id="run-projectless",
        projectless=True,
        created_by=OWNER,
    )
    run = db_session.get(CaliberWorkflowRun, "run-projectless")
    assert run is not None
    _authorize_run(db_session, run, IDENTITY)
    db_session.add(
        CaliberWorkflow(
            workflow_id="workflow-evidence-1",
            project_id=PROJECT_ID,
            visibility="project",
            name="Projectless visible workflow",
            owner=OWNER,
        )
    )
    db_session.commit()
    _authorize_run(db_session, run, IDENTITY)
    with pytest.raises(EvidencePackageError, match="project-scoped"):
        assemble(db_session, run_id="run-projectless", identity=IDENTITY)

    other = CaliberIdentity(
        user_id="@foreign",
        scopes=frozenset({"caliber.operator", "caliber.approver"}),
        active_project_id=PROJECT_ID,
    )
    with pytest.raises(EvidencePackageNotFoundError, match="not found"):
        _authorize_run(db_session, run, other)


def test_legacy_run_without_manifest_is_explicitly_not_required(db_session: Session) -> None:
    _setup_run(db_session, with_manifest=False, legacy=True)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    assert {
        "link": "run_manifest",
        "status": "not_required",
        "code": "legacy_run_has_no_manifest",
        "expected_member_id": "run_manifest:run-evidence-1",
    } in package.record["missing_links"]
    validate_record("evidence_package", package.record)


def test_empty_legacy_run_and_invalid_run_id_are_deterministic_refusals(
    db_session: Session,
) -> None:
    _setup_run(db_session, with_manifest=False, with_proposal=False, legacy=True)
    with pytest.raises(EvidencePackageError, match="no registered evidence"):
        assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    with pytest.raises(EvidencePackageError, match="non-empty"):
        assemble(db_session, run_id="", identity=IDENTITY)


def test_missing_receipt_states_and_authorization_links_are_explicit(
    db_session: Session,
) -> None:
    _setup_run(
        db_session,
        approval_kind="exact_intent",
        approval_ref="approval-missing",
    )
    rows = (
        ("proposal-failed", "failed", None, None),
        ("proposal-unknown", "unknown", None, None),
        ("proposal-proposed", "proposed", None, None),
        ("proposal-policy-missing", "failed", "policy_decision", "policy-missing"),
    )
    for index, (proposal_id, state, approval_kind, approval_ref) in enumerate(rows, start=2):
        intent_bytes = json.dumps({"proposal": proposal_id}).encode()
        db_session.add(
            CaliberEffectProposal(
                proposal_id=proposal_id,
                tenant_id="tenant-evidence",
                workflow_run_id=RUN_ID,
                node_id=f"node-{index}",
                occurrence_id=f"occurrence-{index}",
                logical_key=hex(index)[2:].rjust(64, "0"),
                connector="document-request",
                connector_version="v1",
                effect_class="reversible_write",
                resource="case-1",
                args={},
                expected_resource_version="1",
                intent_algorithm="sha256",
                intent_bytes=intent_bytes,
                intent_hash="sha256:" + hashlib.sha256(intent_bytes).hexdigest(),
                state=state,
                version=1,
                created_at=NOW,
                settled_at=NOW if state in {"failed", "unknown"} else None,
                approval_kind=approval_kind,
                approval_ref=approval_ref,
            )
        )
    db_session.commit()

    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    links = {(link["code"], link["status"]) for link in package.record["missing_links"]}
    assert ("failed_without_receipt", "not_required") in links
    assert ("outcome_unknown", "pending") in links
    assert ("outcome_pending", "pending") in links
    assert ("approval_row_missing", "missing") in links
    assert ("decision_row_missing", "missing") in links
    validate_record("evidence_package", package.record)


def test_canonical_order_parent_links_and_digest_recomputation(db_session: Session) -> None:
    _setup_run(db_session)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    reordered = json.loads(json.dumps(package.record))
    reordered["members"].reverse()
    reordered["members"][0]["parent_ids"].reverse()
    validate_record("evidence_package", reordered)
    assert evidence_package_digest(reordered) == package.package_sha256

    tampered = json.loads(json.dumps(package.record))
    tampered["members"][0]["parent_ids"] = ["foreign-member"]
    with pytest.raises(ContractValidationError, match="parent_ids|package_sha256"):
        validate_record("evidence_package", tampered)

    digest_record = {
        key: value
        for key, value in package.record.items()
        if key not in {"package_sha256", "signatures"}
    }
    assert evidence_package_digest(digest_record) == package.package_sha256


def test_rich_package_contains_context_receipt_policy_approval_and_acceptance(
    db_session: Session,
) -> None:
    _setup_run(
        db_session,
        approval_kind="policy_decision",
        approval_ref="policy-evidence-1",
    )
    proposal = db_session.get(CaliberEffectProposal, "proposal-run-evidence-1")
    assert proposal is not None
    context_bytes = b'{"context":"safe"}'
    db_session.add(
        CaliberContextManifest(
            context_manifest_id="context-evidence-1",
            workflow_run_id=RUN_ID,
            step_request_id="step-evidence-1",
            node_step_id="node-1",
            occurrence_id="occurrence-1",
            attempt=1,
            revision=1,
            schema_version="v1",
            manifest_bytes=context_bytes,
            manifest_digest=hashlib.sha256(context_bytes).hexdigest(),
            created_at=NOW,
        )
    )
    receipt_bytes = b'{"receipt":"safe"}'
    db_session.add(
        CaliberEffectReceipt(
            receipt_id="receipt-evidence-1",
            proposal_id=proposal.proposal_id,
            attempt=1,
            logical_key=proposal.logical_key,
            intent_hash=proposal.intent_hash,
            connector=proposal.connector,
            connector_version=proposal.connector_version,
            resource=proposal.resource,
            downstream_request_id="downstream-evidence-1",
            resource_version="2",
            response_digest="sha256:" + "b" * 64,
            verified_by="query",
            verified_at=NOW,
            receipt_bytes=receipt_bytes,
            receipt_digest=hashlib.sha256(receipt_bytes).hexdigest(),
        )
    )
    db_session.add(
        CaliberPolicyDecision(
            decision_id="policy-evidence-1",
            workflow_run_id=RUN_ID,
            grant_id="grant-evidence-1",
            subject_user_id=OWNER,
            workload_id="workload-evidence-1",
            action="resource.execute",
            resource="case-1",
            purpose="run",
            result="allow",
            reason_codes=["grant_active"],
            policy_version="policy-v1",
            input_hash="c" * 64,
            evaluated_at=NOW,
        )
    )
    db_session.add(
        CaliberRuntimeApprovalRequest(
            runtime_approval_id="approval-evidence-1",
            workflow_run_id=RUN_ID,
            project_id=PROJECT_ID,
            node_id=proposal.node_id,
            status="approved",
            requested_at=NOW,
            decided_at=NOW,
            decided_by=OWNER,
            decision_reason="approved for evidence test",
            policy_snapshot={"allowed_responses": ["approve", "reject"], "risk_class": "standard"},
            proposal_id=proposal.proposal_id,
            intent_hash=proposal.intent_hash,
            evidence_hash="sha256:" + "e" * 64,
            policy_digest="sha256:" + "f" * 64,
            expected_resource_version=proposal.expected_resource_version,
            expires_at=NOW + timedelta(hours=1),
            requested_by="@requester",
        )
    )
    db_session.add(
        CaliberAcceptanceDecision(
            decision_id="acceptance-evidence-1",
            workflow_run_id=RUN_ID,
            candidate_id=proposal.proposal_id,
            criteria_version="criteria-v1",
            observation_digest="d" * 64,
            outcome="accepted",
            criteria=[{"predicate_id": "p", "passed": True}],
            criteria_total=1,
            criteria_failed=0,
            reason_code=None,
            reason_detail=None,
            decided_at=NOW,
        )
    )
    db_session.commit()

    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    kinds = {member["kind"] for member in package.record["members"]}
    assert {
        "run_manifest",
        "context_manifest",
        "proposal",
        "receipt",
        "policy_decision",
    } <= kinds
    assert not any(link["link"] == "receipt" for link in package.record["missing_links"])
    assert package.record["evaluator_provenance"]
    validate_record("evidence_package", package.record)
