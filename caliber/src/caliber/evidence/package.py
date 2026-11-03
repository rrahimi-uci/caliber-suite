"""Assemble the authorized v1 evidence package for one workflow run.

The assembler is deliberately a read-only producer.  It joins the immutable
control records already persisted for a run and emits identifiers, digests and
safe lifecycle metadata; it never copies a payload into the package.  The
package contract and its canonical digest are owned by
``caliber.contracts.registry`` so the later serializer and verifier can use
the same rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.audit import get_redactor
from caliber.auth import CaliberIdentity
from caliber.contracts import evidence_package_digest, validate_record
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberContextManifest,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberRunManifest,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.resource_access import authorize
from caliber.run_manifest import FIXED_REGION, FIXED_TENANT_ID


class EvidencePackageError(ValueError):
    """Base class for deterministic evidence-package refusals."""


class EvidencePackageNotFoundError(EvidencePackageError):
    """The requested run is not present or is not visible to the identity."""


class EvidencePackageAuthorizationError(EvidencePackageError):
    """The identity cannot read the requested run's evidence."""


@dataclass(frozen=True)
class EvidencePackage:
    """The validated package record and its canonical serialized bytes."""

    record: dict[str, Any]
    package_bytes: bytes
    package_sha256: str

    @property
    def package_id(self) -> str:
        return str(self.record["package_id"])

    @property
    def workflow_run_id(self) -> str:
        return str(self.record["workflow_run_id"])

    @property
    def manifest(self) -> dict[str, Any]:
        """Alias used by consumers that call the package body a manifest."""
        return self.record

    @property
    def digest(self) -> str:
        """Alias for the package's canonical digest."""
        return self.package_sha256


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bytes_digest(value: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(value)).hexdigest()


def archive_member_name(member_id: str) -> str:
    """Return the stable, non-disclosing path for one archive member."""

    return f"members/{hashlib.sha256(member_id.encode('utf-8')).hexdigest()}.json"


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _member(
    *,
    member_id: str,
    kind: str,
    payload_sha256: str,
    metadata: dict[str, Any],
    parent_ids: list[str],
) -> dict[str, Any]:
    return {
        "member_id": member_id,
        "kind": kind,
        "payload_sha256": payload_sha256,
        "metadata_sha256": _digest(metadata),
        "parent_ids": sorted(set(parent_ids)),
        "metadata": metadata,
    }


def _missing(
    link: str,
    status: str,
    code: str,
    expected_member_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"link": link, "status": status, "code": code}
    if expected_member_id is not None:
        result["expected_member_id"] = expected_member_id
    return result


def _authorization_error(run_id: str) -> EvidencePackageNotFoundError:
    # A single public refusal avoids leaking whether a foreign run exists.
    return EvidencePackageNotFoundError(f"workflow run {run_id!r} not found")


def _authorize_run(session: Session, run: CaliberWorkflowRun, identity: CaliberIdentity) -> None:
    """Apply the same project/visibility boundary as the run read routes."""
    if not isinstance(identity, CaliberIdentity):
        raise EvidencePackageAuthorizationError("identity must be a resolved CaliberIdentity")

    if run.project_id is not None:
        if identity.active_project_id != run.project_id:
            raise _authorization_error(run.workflow_run_id)
        decision = authorize(session, identity, "read", run.project_id)
        if not decision.allowed:
            raise _authorization_error(run.workflow_run_id)
        return

    workflow = session.execute(
        apply_visibility_filter(
            select(CaliberWorkflow).where(CaliberWorkflow.workflow_id == run.workflow_id),
            CaliberWorkflow,
            identity,
            identity.active_project_id,
        )
    ).scalar_one_or_none()
    if workflow is not None:
        return

    # Some legacy/imported runs predate a workflow row.  Do not widen access:
    # only the recorded initiator may assemble such an unscoped run.
    if run.created_by != identity.user_id:
        raise _authorization_error(run.workflow_run_id)


def _policy_payload(row: CaliberPolicyDecision) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "decision_id": row.decision_id,
        "grant_id": row.grant_id,
        "action": row.action,
        "resource": row.resource,
        "result": row.result,
        "reason_codes": list(row.reason_codes or []),
        "policy_version": row.policy_version,
        "input_hash": row.input_hash,
        "evaluated_at": _timestamp(row.evaluated_at),
    }


def _approval_payload(row: CaliberRuntimeApprovalRequest) -> dict[str, Any]:
    snapshot = row.policy_snapshot if isinstance(row.policy_snapshot, dict) else {}
    allowed = snapshot.get("allowed_responses", ("approve", "reject"))
    if not isinstance(allowed, list):
        allowed = list(allowed) if isinstance(allowed, tuple) else ["approve", "reject"]
    allowed = [item for item in allowed if isinstance(item, str)] or ["approve", "reject"]
    risk_class = snapshot.get("risk_class", "standard")
    if not isinstance(risk_class, str) or not risk_class:
        risk_class = "standard"
    return {
        "schema_version": "v1",
        "approval_request_id": row.runtime_approval_id,
        "proposal_id": row.proposal_id,
        "intent_hash": row.intent_hash,
        "evidence_hash": row.evidence_hash,
        "policy_digest": row.policy_digest,
        "expected_resource_version": row.expected_resource_version,
        "risk_class": risk_class,
        "requested_by": row.requested_by or "unknown",
        "expires_at": _timestamp(row.expires_at),
        "allowed_responses": sorted(set(allowed)),
    }


def _approval_decision_payload(row: CaliberRuntimeApprovalRequest) -> dict[str, Any]:
    decision = row.status
    if decision not in {"approve", "reject", "request_changes", "approved", "rejected"}:
        decision = "reject"
    if decision == "approved":
        decision = "approve"
    elif decision == "rejected":
        decision = "reject"
    return {
        "schema_version": "v1",
        "approval_request_id": row.runtime_approval_id,
        "decision": decision,
        "decided_by": row.decided_by or "unknown",
        "decided_at": _timestamp(row.decided_at),
        "reason": row.decision_reason or "",
    }


def _acceptance_provenance(rows: list[CaliberAcceptanceDecision]) -> list[dict[str, Any]]:
    if not rows:
        # The persistence model does not contain evaluator/provider or result
        # digests when no acceptance decision exists.  Do not manufacture
        # provenance: the missing_links acceptance marker is authoritative.
        return []

    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item.decision_id):
        criteria = row.criteria if isinstance(row.criteria, list) else []
        result_record = {
            "candidate_id": row.candidate_id,
            "outcome": row.outcome,
            "criteria": criteria,
            "reason_code": row.reason_code,
        }
        result.append(
            {
                "evaluator_id": row.criteria_version,
                "criteria_version": row.criteria_version,
                "provider": "caliber.acceptance",
                "simulated": False,
                "input_sha256": row.observation_digest,
                "result_sha256": _digest(result_record),
                "observation_sha256": row.observation_digest,
                "evaluated_at": _timestamp(row.decided_at),
            }
        )
    return result


def assemble(  # noqa: PLR0912, PLR0915
    session: Session,
    *,
    run_id: str,
    identity: CaliberIdentity,
) -> EvidencePackage:
    """Assemble the authorized v1 package for ``run_id``.

    The project, tenant and region come from the persisted run and deployment
    configuration.  The identity can authorize the selected run but cannot
    choose a different project, visibility tier or payload set.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise EvidencePackageError("run_id must be a non-empty string")

    run = session.get(CaliberWorkflowRun, run_id)
    if run is None:
        raise _authorization_error(run_id)
    try:
        _authorize_run(session, run, identity)
    except EvidencePackageNotFoundError:
        raise
    except EvidencePackageError:
        raise
    except Exception as exc:  # pragma: no cover - defensive boundary for auth adapters
        raise EvidencePackageAuthorizationError("evidence package authorization failed") from exc

    if run.project_id is None:
        raise EvidencePackageError(
            "evidence package requires a project-scoped workflow run; projectless runs are not representable in v1"
        )

    manifest = session.execute(
        select(CaliberRunManifest).where(CaliberRunManifest.workflow_run_id == run_id)
    ).scalar_one_or_none()
    contexts = list(
        session.execute(
            select(CaliberContextManifest)
            .where(CaliberContextManifest.workflow_run_id == run_id)
            .order_by(CaliberContextManifest.context_manifest_id)
        ).scalars()
    )
    proposals = list(
        session.execute(
            select(CaliberEffectProposal)
            .where(CaliberEffectProposal.workflow_run_id == run_id)
            .order_by(CaliberEffectProposal.proposal_id)
        ).scalars()
    )
    receipts = list(
        session.execute(
            select(CaliberEffectReceipt)
            .join(
                CaliberEffectProposal,
                CaliberEffectProposal.proposal_id == CaliberEffectReceipt.proposal_id,
            )
            .where(CaliberEffectProposal.workflow_run_id == run_id)
            .order_by(CaliberEffectReceipt.receipt_id)
        ).scalars()
    )
    approvals = list(
        session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(
                CaliberRuntimeApprovalRequest.workflow_run_id == run_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_not(None),
            )
            .order_by(CaliberRuntimeApprovalRequest.runtime_approval_id)
        ).scalars()
    )
    decisions = list(
        session.execute(
            select(CaliberPolicyDecision)
            .where(CaliberPolicyDecision.workflow_run_id == run_id)
            .order_by(CaliberPolicyDecision.decision_id)
        ).scalars()
    )
    acceptance = list(
        session.execute(
            select(CaliberAcceptanceDecision)
            .where(CaliberAcceptanceDecision.workflow_run_id == run_id)
            .order_by(CaliberAcceptanceDecision.decision_id)
        ).scalars()
    )

    manifest_id = f"run_manifest:{manifest.manifest_id}" if manifest else f"run_manifest:{run_id}"
    missing_links: list[dict[str, Any]] = []
    if manifest is None:
        missing_links.append(
            _missing(
                "run_manifest",
                "not_required" if run.manifest_legacy else "missing",
                "legacy_run_has_no_manifest" if run.manifest_legacy else "manifest_row_missing",
                manifest_id,
            )
        )

    members: list[dict[str, Any]] = []
    if manifest is not None:
        members.append(
            _member(
                member_id=manifest_id,
                kind="run_manifest",
                payload_sha256=_bytes_digest(manifest.manifest_bytes),
                metadata={
                    "schema_version": manifest.schema_version,
                    "created_at": _timestamp(manifest.created_at),
                },
                parent_ids=[],
            )
        )

    context_by_step: dict[tuple[str | None, str | None], str] = {
        (row.node_step_id, row.occurrence_id): f"context_manifest:{row.context_manifest_id}"
        for row in contexts
        if row.node_step_id and row.occurrence_id
    }
    for context in contexts:
        members.append(
            _member(
                member_id=f"context_manifest:{context.context_manifest_id}",
                kind="context_manifest",
                payload_sha256=_bytes_digest(context.manifest_bytes),
                metadata={
                    "schema_version": context.schema_version,
                    "step_request_id": context.step_request_id,
                    "revision": context.revision,
                    "node_step_id": context.node_step_id,
                    "occurrence_id": context.occurrence_id,
                    "attempt": context.attempt,
                    "created_at": _timestamp(context.created_at),
                },
                parent_ids=[manifest_id],
            )
        )

    receipt_by_proposal: dict[str, list[CaliberEffectReceipt]] = {}
    for receipt in receipts:
        receipt_by_proposal.setdefault(receipt.proposal_id, []).append(receipt)
    approval_by_proposal: dict[str, list[CaliberRuntimeApprovalRequest]] = {}
    for approval in approvals:
        if approval.proposal_id:
            approval_by_proposal.setdefault(approval.proposal_id, []).append(approval)
    decision_by_id = {decision.decision_id: decision for decision in decisions}

    for proposal in proposals:
        parent_ids = [manifest_id]
        context_id = context_by_step.get((proposal.node_id, proposal.occurrence_id))
        if context_id:
            parent_ids.append(context_id)
        members.append(
            _member(
                member_id=f"proposal:{proposal.proposal_id}",
                kind="proposal",
                payload_sha256=_bytes_digest(proposal.intent_bytes),
                metadata={
                    "schema_version": "v1",
                    "state": proposal.state,
                    "node_id": proposal.node_id,
                    "occurrence_id": proposal.occurrence_id,
                    "connector": proposal.connector,
                    "connector_version": proposal.connector_version,
                    "effect_class": proposal.effect_class,
                    "approval_kind": proposal.approval_kind,
                    "approval_ref": proposal.approval_ref,
                    "receipt_id": proposal.receipt_id,
                    "created_at": _timestamp(proposal.created_at),
                    "settled_at": _timestamp(proposal.settled_at),
                },
                parent_ids=parent_ids,
            )
        )
        if not receipt_by_proposal.get(proposal.proposal_id):
            expected = f"receipt:{proposal.proposal_id}"
            if proposal.state == "committed":
                status, code = "missing", "receipt_row_missing"
            elif proposal.state in {"failed"}:
                status, code = "not_required", "failed_without_receipt"
            elif proposal.state in {"unknown", "reconciled"}:
                status, code = "pending", "outcome_unknown"
            else:
                status, code = "pending", "outcome_pending"
            missing_links.append(
                _missing("receipt", status, code, expected if status == "missing" else None)
            )
        if (
            proposal.approval_kind
            and proposal.approval_ref
            and not approval_by_proposal.get(proposal.proposal_id)
        ):
            missing_links.append(
                _missing(
                    "approval",
                    "missing",
                    "approval_row_missing",
                    f"approval_request:{proposal.approval_ref}",
                )
            )
        if (
            proposal.approval_kind == "policy_decision"
            and proposal.approval_ref not in decision_by_id
        ):
            missing_links.append(
                _missing(
                    "decision",
                    "missing",
                    "decision_row_missing",
                    f"policy_decision:{proposal.approval_ref}",
                )
            )

    for receipt in receipts:
        members.append(
            _member(
                member_id=f"receipt:{receipt.receipt_id}",
                kind="receipt",
                payload_sha256=_bytes_digest(receipt.receipt_bytes),
                metadata={
                    "schema_version": "v1",
                    "proposal_id": receipt.proposal_id,
                    "attempt": receipt.attempt,
                    "verified_by": receipt.verified_by,
                    "verified_at": _timestamp(receipt.verified_at),
                },
                parent_ids=[f"proposal:{receipt.proposal_id}"],
            )
        )

    for decision in decisions:
        parent_ids = [manifest_id]
        parent_ids.extend(
            f"proposal:{proposal.proposal_id}"
            for proposal in proposals
            if proposal.approval_kind == "policy_decision"
            and proposal.approval_ref == decision.decision_id
        )
        payload = _policy_payload(decision)
        members.append(
            _member(
                member_id=f"policy_decision:{decision.decision_id}",
                kind="policy_decision",
                payload_sha256=_digest(payload),
                metadata={
                    "schema_version": "v1",
                    "result": decision.result,
                    "evaluated_at": _timestamp(decision.evaluated_at),
                },
                parent_ids=parent_ids,
            )
        )

    for approval in approvals:
        approval_id = f"approval_request:{approval.runtime_approval_id}"
        proposal_id = f"proposal:{approval.proposal_id}"
        payload = _approval_payload(approval)
        members.append(
            _member(
                member_id=approval_id,
                kind="approval_request",
                payload_sha256=_digest(payload),
                metadata={
                    "schema_version": "v1",
                    "status": approval.status,
                    "proposal_id": approval.proposal_id,
                    "requested_at": _timestamp(approval.requested_at),
                    "decided_at": _timestamp(approval.decided_at),
                    "consumed_at": _timestamp(approval.consumed_at),
                },
                parent_ids=[proposal_id],
            )
        )
        if approval.decided_at is not None and approval.decided_by:
            decision_id = f"approval_decision:{approval.runtime_approval_id}"
            decision_payload = _approval_decision_payload(approval)
            members.append(
                _member(
                    member_id=decision_id,
                    kind="approval_decision",
                    payload_sha256=_digest(decision_payload),
                    metadata={
                        "schema_version": "v1",
                        "decision": decision_payload["decision"],
                        "decided_at": _timestamp(approval.decided_at),
                    },
                    parent_ids=[approval_id],
                )
            )

    if not acceptance:
        missing_links.append(
            _missing("acceptance", "unrecorded", "acceptance_record_not_available")
        )

    redaction = "redacted" if get_redactor().patterns else "none"
    if not members:
        raise EvidencePackageError("workflow run has no registered evidence records")
    members.sort(key=lambda member: member["member_id"])
    missing_links.sort(key=lambda link: (link["link"], link["status"], link["code"]))
    package: dict[str, Any] = {
        "schema_version": "v1",
        # The package is a view of the immutable run records, so its identity
        # must remain stable across repeated assembly/export requests.  A
        # random id or wall-clock timestamp would defeat conditional object
        # creation and make the archive cache impossible to use safely.
        "package_id": f"ep-{run_id}",
        "workflow_run_id": run_id,
        "tenant_id": run.tenant_id or FIXED_TENANT_ID,
        "region": FIXED_REGION,
        "project_id": run.project_id,
        "view": {"authorization": "authorized", "redaction": redaction},
        "members": members,
        "missing_links": missing_links,
        "evaluator_provenance": _acceptance_provenance(acceptance),
        "package_sha256": "0" * 64,
        "created_at": _timestamp(run.completed_at or run.started_at or run.queued_at)
        or "1970-01-01T00:00:00Z",
    }
    package["package_sha256"] = evidence_package_digest(package)
    validate_record("evidence_package", package)
    package_bytes = _canonical_bytes(package)
    return EvidencePackage(
        record=package,
        package_bytes=package_bytes,
        package_sha256=package["package_sha256"],
    )


def payload_bytes_for_package(session: Session, package: EvidencePackage) -> dict[str, bytes]:
    """Load only the selected run's payload bytes for an export.

    ``assemble`` intentionally returns a digest-only projection.  The archive
    writer is the one consumer allowed to materialize those bytes, and it
    re-queries by the package's run id rather than trusting caller-provided
    references.  Every returned byte string is checked against the already
    assembled member digest before a caller can serialize it.
    """

    run = session.get(CaliberWorkflowRun, package.workflow_run_id)
    if run is None:
        raise EvidencePackageError("workflow run disappeared while exporting evidence")

    manifest = session.execute(
        select(CaliberRunManifest).where(
            CaliberRunManifest.workflow_run_id == package.workflow_run_id
        )
    ).scalar_one_or_none()
    contexts = list(
        session.execute(
            select(CaliberContextManifest).where(
                CaliberContextManifest.workflow_run_id == package.workflow_run_id
            )
        ).scalars()
    )
    proposals = list(
        session.execute(
            select(CaliberEffectProposal).where(
                CaliberEffectProposal.workflow_run_id == package.workflow_run_id
            )
        ).scalars()
    )
    receipts = list(
        session.execute(
            select(CaliberEffectReceipt)
            .join(
                CaliberEffectProposal,
                CaliberEffectProposal.proposal_id == CaliberEffectReceipt.proposal_id,
            )
            .where(CaliberEffectProposal.workflow_run_id == package.workflow_run_id)
        ).scalars()
    )
    approvals = list(
        session.execute(
            select(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.workflow_run_id == package.workflow_run_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_not(None),
            )
        ).scalars()
    )
    decisions = list(
        session.execute(
            select(CaliberPolicyDecision).where(
                CaliberPolicyDecision.workflow_run_id == package.workflow_run_id
            )
        ).scalars()
    )

    payloads: dict[str, bytes] = {}
    if manifest is not None:
        payloads[f"run_manifest:{manifest.manifest_id}"] = bytes(manifest.manifest_bytes)
    payloads.update(
        {
            f"context_manifest:{row.context_manifest_id}": bytes(row.manifest_bytes)
            for row in contexts
        }
    )
    payloads.update({f"proposal:{row.proposal_id}": bytes(row.intent_bytes) for row in proposals})
    payloads.update({f"receipt:{row.receipt_id}": bytes(row.receipt_bytes) for row in receipts})
    payloads.update(
        {
            f"policy_decision:{row.decision_id}": _canonical_bytes(_policy_payload(row))
            for row in decisions
        }
    )
    for row in approvals:
        request_id = f"approval_request:{row.runtime_approval_id}"
        payloads[request_id] = _canonical_bytes(_approval_payload(row))
        if row.decided_at is not None and row.decided_by:
            payloads[f"approval_decision:{row.runtime_approval_id}"] = _canonical_bytes(
                _approval_decision_payload(row)
            )

    expected = {
        str(member["member_id"]): str(member["payload_sha256"])
        for member in package.record["members"]
    }
    selected: dict[str, bytes] = {}
    for member_id, digest in expected.items():
        payload = payloads.get(member_id)
        if payload is None:
            raise EvidencePackageError(
                f"evidence member {member_id!r} has no payload in its selected run"
            )
        actual = _bytes_digest(payload)
        if actual != digest:
            raise EvidencePackageError(f"evidence member {member_id!r} changed after assembly")
        selected[member_id] = payload
    return selected


__all__ = [
    "EvidencePackage",
    "EvidencePackageAuthorizationError",
    "EvidencePackageError",
    "EvidencePackageNotFoundError",
    "archive_member_name",
    "assemble",
    "payload_bytes_for_package",
]
