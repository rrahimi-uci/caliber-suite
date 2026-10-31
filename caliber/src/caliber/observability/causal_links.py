"""The persisted causal links of one workflow run, read back as identifiers (B11.3b, #745).

B11.3a (#737) puts four identifiers on the spans of a governed effect.  This module reads the *rows* those
identifiers name and returns the chain a reviewer follows: run manifest, the step's context manifest, the effect
proposal, the approval request, the policy decision and the verified receipt (whose id is the span's ``effect_id``).

* **Identifiers only.**  Every value is an id, a digest, a state, a code or a timestamp that is read from a persisted
  row.  No query selects an argument, intent body, resource, resource version, downstream request id, manifest body or
  any column that names a person (``requested_by``, ``decided_by``, ``subject_user_id``) or carries prose
  (``decision_reason``, ``purpose``).  The response schema forbids extra fields, so a column added later cannot reach a
  caller by accident.
* **Missing is stated, never synthesized.**  A link with no persisted row is reported as ``missing`` with a stable
  code, and a link the platform does not record against the effect at all is ``unrecorded`` (the commit-time run
  decision of an exact-intent approval: it is persisted per run, see the ``policy_decisions`` section).  Nothing is
  inferred from timestamps or neighbours.
* **Scoped to the run.**  Every query is filtered by the run the caller was authorized for, including the by-id lookups
  of approvals and decisions, so an id stored on one run's proposal can never pull in another run's row.
* **Bounded.**  A page is at most :data:`MAX_LIMIT` rows of one *section* and a request costs a fixed handful of
  queries whatever the page size (no query per effect).  Pages use keyset pagination on ``(timestamp, id)``.

This module does not authorize.  The caller (the route, or a later evidence assembler) must already have established
that the principal may read ``run`` in its project: it takes the authorized run row, never an id from a request.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Sequence
from typing import Any, Final, Literal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from caliber.context.manifest import node_step_id_for
from caliber.db.models import (
    CaliberContextManifest,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberRunManifest,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import EXACT_INTENT_APPROVAL, POLICY_DECISION
from caliber.observability.span_attributes import causal_attributes

Section = Literal["effects", "context_manifests", "policy_decisions"]
SECTIONS: Final[tuple[str, ...]] = ("effects", "context_manifests", "policy_decisions")
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
MAX_CURSOR_LENGTH: Final = 256
#: Reason codes of a decision kept per row; a decision never carries more than a handful.
MAX_REASON_CODES: Final = 32

# Link statuses (see ``caliber.schemas.CausalLinkStatus``).
LINKED: Final = "linked"
NOT_REQUIRED: Final = "not_required"
PENDING: Final = "pending"
MISSING: Final = "missing"
UNRECORDED: Final = "unrecorded"
#: The statuses that mean a reader cannot follow the link to a persisted row.
UNRESOLVED: Final = frozenset({MISSING, UNRECORDED})

# Codes: stable, lower snake case, never a message.
MANIFEST_LEGACY: Final = "legacy_run_has_no_manifest"
MANIFEST_ROW_MISSING: Final = "manifest_row_missing"
CONTEXT_NOT_FOUND: Final = "no_context_manifest_for_node_step"
APPROVAL_ROW_MISSING: Final = "approval_row_missing"
APPROVAL_NOT_REQUESTED: Final = "approval_not_requested"
APPROVAL_NOT_CONSUMED: Final = "approval_not_consumed"
APPROVED_BY_POLICY: Final = "approved_by_policy_decision"
DECISION_ROW_MISSING: Final = "decision_row_missing"
DECISION_NOT_APPROVED_YET: Final = "not_approved_yet"
DECISION_PER_RUN_ONLY: Final = "decision_recorded_per_run_only"
EFFECT_RECEIPT_MISSING: Final = "receipt_row_missing"
ACCEPTANCE_NOT_AVAILABLE: Final = "acceptance_record_not_available"
EVIDENCE_PACKAGE_NOT_AVAILABLE: Final = "evidence_package_not_available"
#: What a proposal with no receipt is waiting on, by its state.
_NO_RECEIPT_CODE: Final = {
    "proposed": "not_dispatched",
    "approved": "not_dispatched",
    "dispatched": "outcome_pending",
    "unknown": "outcome_unknown",
    "reconciled": "reconciled_without_receipt",
    "failed": "failed_without_receipt",
    "committed": EFFECT_RECEIPT_MISSING,
}


class CausalLinksCursorError(ValueError):
    """The ``cursor`` is not one this API issued for this run and section."""


def encode_cursor(row_id: str) -> str:
    """An opaque cursor naming the last row of a page."""
    return base64.urlsafe_b64encode(row_id.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    """The row id a cursor names, or :class:`CausalLinksCursorError` for anything this API did not issue."""
    if cursor is None:
        return None
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise CausalLinksCursorError("cursor is invalid")
    try:
        padded = cursor.replace("-", "+").replace("_", "/") + "=" * (-len(cursor) % 4)
        row_id = base64.b64decode(padded.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        raise CausalLinksCursorError("cursor is invalid") from None
    if not row_id or encode_cursor(row_id) != cursor:
        raise CausalLinksCursorError("cursor is invalid")
    return row_id


def _page(
    session: Session,
    statement: Select[Any],
    *,
    run_id: str,
    run_column: Any,
    timestamp: Any,
    identity: Any,
    cursor_id: str | None,
    limit: int,
) -> tuple[list[Any], bool]:
    """``limit`` rows of ``statement`` after the cursor, ordered ``(timestamp, id)``, and whether more follow.

    The keyset anchor is read inside the database (a scalar subquery on the cursor row), never bound as a Python
    datetime: SQLite stores a server-stamped timestamp without fractional seconds and a bound parameter with them, and
    the two would not compare equal.  A cursor that names no row of this run is refused, so one run's cursor cannot be
    replayed against another.
    """
    statement = statement.where(run_column == run_id)
    if cursor_id is not None:
        if (
            session.execute(
                select(identity).where(run_column == run_id, identity == cursor_id)
            ).first()
            is None
        ):
            raise CausalLinksCursorError("cursor is invalid")
        anchor = select(timestamp).where(identity == cursor_id).scalar_subquery()
        statement = statement.where(
            or_(timestamp > anchor, and_(timestamp == anchor, identity > cursor_id))
        )
    rows = list(session.execute(statement.order_by(timestamp, identity).limit(limit + 1)).all())
    return rows[:limit], len(rows) > limit


def _run_manifest(session: Session, run: CaliberWorkflowRun) -> dict[str, Any]:
    if run.manifest_legacy:
        return {"status": "legacy", "code": MANIFEST_LEGACY}
    row = session.execute(
        select(
            CaliberRunManifest.manifest_id,
            CaliberRunManifest.schema_version,
            CaliberRunManifest.manifest_digest,
            CaliberRunManifest.created_at,
        ).where(CaliberRunManifest.workflow_run_id == run.workflow_run_id)
    ).first()
    if row is None:
        return {"status": MISSING, "code": MANIFEST_ROW_MISSING}
    return {
        "status": LINKED,
        "manifest_id": row.manifest_id,
        "schema_version": row.schema_version,
        "manifest_digest": row.manifest_digest,
        "created_at": row.created_at,
    }


def _context_manifests(
    session: Session, run_id: str, limit: int, cursor_id: str | None
) -> tuple[list[dict[str, Any]], bool]:
    model = CaliberContextManifest
    rows, more = _page(
        session,
        select(
            model.context_manifest_id,
            model.step_request_id,
            model.node_step_id,
            model.occurrence_id,
            model.attempt,
            model.revision,
            model.schema_version,
            model.manifest_digest,
            model.created_at,
        ),
        run_id=run_id,
        run_column=model.workflow_run_id,
        timestamp=model.created_at,
        identity=model.context_manifest_id,
        cursor_id=cursor_id,
        limit=limit,
    )
    return [dict(row._mapping) for row in rows], more


def _policy_decisions(
    session: Session, run_id: str, limit: int, cursor_id: str | None
) -> tuple[list[dict[str, Any]], bool]:
    model = CaliberPolicyDecision
    rows, more = _page(
        session,
        select(
            model.decision_id,
            model.grant_id,
            model.action,
            model.resource,
            model.result,
            model.reason_codes,
            model.policy_version,
            model.input_hash,
            model.evaluated_at,
        ),
        run_id=run_id,
        run_column=model.workflow_run_id,
        timestamp=model.evaluated_at,
        identity=model.decision_id,
        cursor_id=cursor_id,
        limit=limit,
    )
    return [{**row._mapping, "reason_codes": _codes(row.reason_codes)} for row in rows], more


def _codes(value: object) -> list[str]:
    """A decision's reason codes: strings only, bounded."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:MAX_REASON_CODES]


def _latest_per(
    session: Session,
    columns: Sequence[Any],
    *,
    partition: Any,
    order: Sequence[Any],
    where: Sequence[Any],
) -> list[Any]:
    """The first row of each ``partition`` under ``order``, in one query."""
    ranked = (
        select(
            *columns,
            func.row_number().over(partition_by=partition, order_by=tuple(order)).label("_rank"),
        )
        .where(*where)
        .subquery()
    )
    return list(session.execute(select(ranked).where(ranked.c._rank == 1)).all())


def _context_links(session: Session, run_id: str, node_steps: Iterable[str]) -> dict[str, Any]:
    """The latest context manifest (highest attempt, then revision) of each node occurrence, by ``node_step_id``."""
    model = CaliberContextManifest
    wanted = set(node_steps)
    rows = _latest_per(
        session,
        (
            model.context_manifest_id,
            model.node_step_id,
            model.attempt,
            model.revision,
            model.manifest_digest,
        ),
        partition=model.node_step_id,
        order=(
            func.coalesce(model.attempt, 0).desc(),
            model.revision.desc(),
            model.context_manifest_id.desc(),
        ),
        where=(model.workflow_run_id == run_id, model.node_step_id.in_(wanted)),
    )
    return {row.node_step_id: row for row in rows}


def _approval_rows(
    session: Session, run_id: str, proposals: Sequence[Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(by approval id, latest by proposal id)`` for the page: the approvals proposals name, and the newest request of those that name none."""
    model = CaliberRuntimeApprovalRequest
    columns = (
        model.runtime_approval_id,
        model.proposal_id,
        model.status,
        model.superseded_by,
        model.intent_hash,
        model.evidence_hash,
        model.policy_digest,
        model.requested_at,
        model.decided_at,
        model.expires_at,
        model.consumed_at,
    )
    named = {
        row.approval_ref
        for row in proposals
        if row.approval_kind == EXACT_INTENT_APPROVAL and row.approval_ref
    }
    by_id: dict[str, Any] = {}
    if named:
        for row in session.execute(
            select(*columns).where(
                model.workflow_run_id == run_id, model.runtime_approval_id.in_(named)
            )
        ):
            by_id[row.runtime_approval_id] = row
    asking = {row.proposal_id for row in proposals if row.approval_kind is None}
    latest: dict[str, Any] = {}
    if asking:
        for row in _latest_per(
            session,
            columns,
            partition=model.proposal_id,
            order=(model.requested_at.desc(), model.runtime_approval_id.desc()),
            where=(model.workflow_run_id == run_id, model.proposal_id.in_(asking)),
        ):
            latest[row.proposal_id] = row
    return by_id, latest


def _decision_rows(session: Session, run_id: str, proposals: Sequence[Any]) -> dict[str, Any]:
    model = CaliberPolicyDecision
    named = {
        row.approval_ref
        for row in proposals
        if row.approval_kind == POLICY_DECISION and row.approval_ref
    }
    if not named:
        return {}
    return {
        row.decision_id: row
        for row in session.execute(
            select(
                model.decision_id,
                model.action,
                model.result,
                model.reason_codes,
                model.policy_version,
                model.input_hash,
                model.evaluated_at,
            ).where(model.workflow_run_id == run_id, model.decision_id.in_(named))
        )
    }


def _receipt_rows(session: Session, proposals: Sequence[Any]) -> dict[str, Any]:
    """The receipt of each proposal: the one it records as having settled it, else its latest attempt."""
    model = CaliberEffectReceipt
    columns = (
        model.receipt_id,
        model.proposal_id,
        model.attempt,
        model.receipt_digest,
        model.response_digest,
        model.verified_at,
    )
    ids = {row.proposal_id for row in proposals}
    latest = {
        row.proposal_id: row
        for row in _latest_per(
            session,
            columns,
            partition=model.proposal_id,
            order=(model.attempt.desc(), model.receipt_id.desc()),
            where=(model.proposal_id.in_(ids),),
        )
    }
    settled = {
        row.proposal_id: row.receipt_id
        for row in proposals
        if row.receipt_id
        and getattr(latest.get(row.proposal_id), "receipt_id", None) != row.receipt_id
    }
    if settled:
        for row in session.execute(
            select(*columns).where(
                model.receipt_id.in_(set(settled.values())), model.proposal_id.in_(ids)
            )
        ):
            if settled.get(row.proposal_id) == row.receipt_id:
                latest[row.proposal_id] = row
    return latest


def _approval_link(proposal: Any, by_id: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    if proposal.approval_kind == POLICY_DECISION:
        return {"status": NOT_REQUIRED, "code": APPROVED_BY_POLICY}
    if proposal.approval_kind == EXACT_INTENT_APPROVAL:
        row = by_id.get(proposal.approval_ref)
        if row is None or row.proposal_id != proposal.proposal_id:
            return {"status": MISSING, "code": APPROVAL_ROW_MISSING}
        return _approval_fields(row, LINKED, None)
    row = latest.get(proposal.proposal_id)
    if row is None:
        return {"status": MISSING, "code": APPROVAL_NOT_REQUESTED}
    return _approval_fields(row, PENDING, APPROVAL_NOT_CONSUMED)


def _approval_fields(row: Any, status: str, code: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "approval_id": row.runtime_approval_id,
        "request_status": row.status,
        "superseded_by": row.superseded_by,
        "intent_hash": row.intent_hash,
        "evidence_hash": row.evidence_hash,
        "policy_digest": row.policy_digest,
        "requested_at": row.requested_at,
        "decided_at": row.decided_at,
        "expires_at": row.expires_at,
        "consumed_at": row.consumed_at,
    }


def _decision_link(proposal: Any, rows: dict[str, Any]) -> dict[str, Any]:
    if proposal.approval_kind == POLICY_DECISION:
        row = rows.get(proposal.approval_ref)
        if row is None:
            return {"status": MISSING, "code": DECISION_ROW_MISSING}
        return {
            "status": LINKED,
            "decision_id": row.decision_id,
            "action": row.action,
            "result": row.result,
            "reason_codes": _codes(row.reason_codes),
            "policy_version": row.policy_version,
            "input_hash": row.input_hash,
            "evaluated_at": row.evaluated_at,
        }
    if proposal.approval_kind == EXACT_INTENT_APPROVAL:
        return {"status": UNRECORDED, "code": DECISION_PER_RUN_ONLY}
    return {"status": MISSING, "code": DECISION_NOT_APPROVED_YET}


def _effect_link(proposal: Any, receipts: dict[str, Any]) -> dict[str, Any]:
    row = receipts.get(proposal.proposal_id)
    if row is None:
        return {
            "status": MISSING,
            "code": _NO_RECEIPT_CODE.get(proposal.state, EFFECT_RECEIPT_MISSING),
        }
    return {
        "status": LINKED,
        "effect_id": row.receipt_id,
        "attempt": row.attempt,
        "receipt_digest": row.receipt_digest,
        "response_digest": row.response_digest,
        "verified_at": row.verified_at,
        "committed": proposal.state == "committed" and proposal.receipt_id == row.receipt_id,
    }


def _context_link(run_id: str, proposal: Any, contexts: dict[str, Any]) -> dict[str, Any]:
    row = contexts.get(node_step_id_for(run_id, proposal.node_id, proposal.occurrence_id))
    if row is None:
        return {"status": MISSING, "code": CONTEXT_NOT_FOUND}
    return {
        "status": LINKED,
        "context_manifest_id": row.context_manifest_id,
        "revision": row.revision,
        "attempt": row.attempt,
        "manifest_digest": row.manifest_digest,
    }


def _effects(
    session: Session, run_id: str, limit: int, cursor_id: str | None
) -> tuple[list[dict[str, Any]], bool]:
    model = CaliberEffectProposal
    proposals, more = _page(
        session,
        select(
            model.proposal_id,
            model.node_id,
            model.occurrence_id,
            model.state,
            model.connector,
            model.connector_version,
            model.effect_class,
            model.intent_hash,
            model.approval_kind,
            model.approval_ref,
            model.receipt_id,
            model.created_at,
            model.settled_at,
        ),
        run_id=run_id,
        run_column=model.workflow_run_id,
        timestamp=model.created_at,
        identity=model.proposal_id,
        cursor_id=cursor_id,
        limit=limit,
    )
    if not proposals:
        return [], more
    contexts = _context_links(
        session,
        run_id,
        (node_step_id_for(run_id, row.node_id, row.occurrence_id) for row in proposals),
    )
    approvals, newest = _approval_rows(session, run_id, proposals)
    decisions = _decision_rows(session, run_id, proposals)
    receipts = _receipt_rows(session, proposals)
    effects: list[dict[str, Any]] = []
    for proposal in proposals:
        links = {
            "context_manifest": _context_link(run_id, proposal, contexts),
            "approval": _approval_link(proposal, approvals, newest),
            "decision": _decision_link(proposal, decisions),
            "effect": _effect_link(proposal, receipts),
        }
        effects.append(
            {
                "proposal_id": proposal.proposal_id,
                "node_id": proposal.node_id,
                "occurrence_id": proposal.occurrence_id,
                "node_step_id": node_step_id_for(run_id, proposal.node_id, proposal.occurrence_id),
                "state": proposal.state,
                "connector": proposal.connector,
                "connector_version": proposal.connector_version,
                "effect_class": proposal.effect_class,
                "intent_hash": proposal.intent_hash,
                "created_at": proposal.created_at,
                "settled_at": proposal.settled_at,
                **links,
                "span_attributes": _span_attributes(proposal, links),
                "missing_links": [
                    name for name, link in links.items() if link["status"] in UNRESOLVED
                ],
            }
        )
    return effects, more


def _span_attributes(proposal: Any, links: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The ``caliber.*`` attributes the platform sets on this effect's spans, through the same allow-list function.

    Ids come from the resolved rows above, so an id that names no row of this run is absent here exactly as a
    missing link is absent from the span.  The ``decision_id`` of an exact-intent approval is the run decision read at
    commit time; it is not persisted against the effect and so is not listed (see the ``policy_decisions`` section).
    """

    def linked(link: str, key: str) -> str | None:
        item = links[link]
        return item.get(key) if item["status"] == LINKED else None

    return causal_attributes(
        proposal_id=proposal.proposal_id,
        decision_id=linked("decision", "decision_id"),
        approval_id=linked("approval", "approval_id"),
        effect_id=linked("effect", "effect_id"),
    )


def read_causal_links(
    session: Session,
    run: CaliberWorkflowRun,
    *,
    section: str = "effects",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One page of the causal links of an **already authorized** ``run`` (see the module docstring).

    ``section`` selects the paginated list (``effects``, ``context_manifests`` or ``policy_decisions``); the run-level
    header (manifest link, absent-record markers) is on every page.  Read-only: it issues SELECTs and never flushes.
    Raises :class:`CausalLinksCursorError` for a cursor that is not one of this run's, and ``ValueError`` for an unknown
    section or a ``limit`` outside ``1..MAX_LIMIT``.
    """
    if section not in SECTIONS:
        raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    run_id = run.workflow_run_id
    cursor_id = decode_cursor(cursor)
    page: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTIONS}
    if section == "effects":
        page[section], more = _effects(session, run_id, limit, cursor_id)
        last = page[section][-1]["proposal_id"] if page[section] else None
    elif section == "context_manifests":
        page[section], more = _context_manifests(session, run_id, limit, cursor_id)
        last = page[section][-1]["context_manifest_id"] if page[section] else None
    else:
        page[section], more = _policy_decisions(session, run_id, limit, cursor_id)
        last = page[section][-1]["decision_id"] if page[section] else None
    return {
        "workflow_run_id": run_id,
        "project_id": run.project_id,
        "trace_id": run.trace_id or None,
        "section": section,
        "run_manifest": _run_manifest(session, run),
        "acceptance": {"code": ACCEPTANCE_NOT_AVAILABLE},
        "evidence_package": {"code": EVIDENCE_PACKAGE_NOT_AVAILABLE},
        **page,
        "limit": limit,
        "has_more": more,
        "next_cursor": encode_cursor(last) if more and last is not None else None,
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SECTIONS",
    "CausalLinksCursorError",
    "decode_cursor",
    "encode_cursor",
    "read_causal_links",
]
