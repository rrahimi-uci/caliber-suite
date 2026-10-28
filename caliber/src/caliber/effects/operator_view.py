"""The operator reconciliation view: legacy ledger claims and effect proposals, side by side and never mixed (B05.6, #751).

Two records of "an effect that may need a human" exist and they are different things:

* the **legacy ledger** (``caliber_effect_ledger``, ``workflows.effect_ledger``): one row per claimed effect of the queued-run
  worker, ``in_progress`` until it finishes. Its operator decision is the admin ``resolve`` action (``skip``: the effect reached
  the remote system; ``retry``: it did not). Nothing here changes it, migrates it away or invents a v1 receipt for it;
* the **effect proposal** (``caliber_effect_proposals``, ADR 0003): seven states, compare-and-set, verified receipts. It is
  settled by the reconciler's lookup by key (:mod:`caliber.effects.reconcile`), never by an operator's say-so, so it offers no
  safe action here today (an audited operator action is tracked in #997).

Every row carries ``source`` and ``source_version`` and the ``safe_actions`` that apply to *that* kind of row, so a client cannot
apply the legacy ``resolve`` to a proposal or read a proposal as a ledger claim. No row carries a payload: no arguments, intent
bytes or recorded result, only identifiers, states and evidence codes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberEffectProposal, CaliberWorkflowRun
from caliber.effects.reconcile import OPEN_STATES
from caliber.effects.records import DISPATCHED, STATES
from caliber.ownership import current_lease

LEGACY_SOURCE: Final = "legacy_ledger"
LEGACY_VERSION: Final = "ledger-v0"
PROPOSAL_SOURCE: Final = "effect_proposal"
PROPOSAL_VERSION: Final = "proposal-v1"
#: What an operator can do with a legacy claim that is still ``in_progress`` (the admin ``resolve`` endpoint).
LEGACY_SAFE_ACTIONS: Final = ("resolve:skip", "resolve:retry")
#: The proposals the view shows by default: the ones nobody has the final word on.
OPEN: Final = "open"
ANY: Final = "any"
MAX_LIMIT: Final = 1000


def describe_sources() -> dict[str, dict[str, Any]]:
    """What each kind of row is, how it is settled and what an operator may do with it."""
    return {
        LEGACY_SOURCE: {
            "source_version": LEGACY_VERSION,
            "settled_by": "operator",
            "safe_actions": list(LEGACY_SAFE_ACTIONS),
            "applies_to": "status in_progress",
        },
        PROPOSAL_SOURCE: {
            "source_version": PROPOSAL_VERSION,
            "settled_by": "reconciler",
            "safe_actions": [],
            "applies_to": "none: settled by the reconciler's lookup by key",
        },
    }


def label_legacy(row: dict[str, Any], *, in_progress: str) -> dict[str, Any]:
    """A legacy ledger row, unchanged, plus its source label and the actions that apply to it."""
    resolvable = row.get("status") == in_progress
    return {
        **row,
        "source": LEGACY_SOURCE,
        "source_version": LEGACY_VERSION,
        "safe_actions": list(LEGACY_SAFE_ACTIONS) if resolvable else [],
    }


def proposal_states(requested: str) -> tuple[str, ...]:
    """The states a ``proposal_state`` filter names: ``open``, ``any`` or one of the seven states."""
    value = requested.strip().casefold() or OPEN
    if value == OPEN:
        return OPEN_STATES
    if value == ANY:
        return STATES
    if value in STATES:
        return (value,)
    raise ValueError(f"proposal_state must be 'open', 'any' or one of {', '.join(STATES)}")


def _iso(moment: Any) -> str | None:
    return moment.isoformat() if moment is not None else None


def list_proposals(
    session: Session,
    *,
    states: Sequence[str],
    workflow_run_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Effect proposals in ``states`` for operator inspection, newest first, without any payload.

    ``abandoned`` says whether a ``dispatched`` attempt was dispatched under a lease that has since been taken over (``True``:
    the reconciler will pick it up; ``False``: the dispatcher still holds that lease; ``None``: not a dispatched attempt, or
    there is no token or lease to compare).
    """
    query = (
        select(CaliberEffectProposal, CaliberWorkflowRun.project_id)
        .join(
            CaliberWorkflowRun,
            CaliberWorkflowRun.workflow_run_id == CaliberEffectProposal.workflow_run_id,
        )
        .where(CaliberEffectProposal.state.in_(tuple(states)))
        .order_by(CaliberEffectProposal.created_at.desc(), CaliberEffectProposal.proposal_id)
    )
    if workflow_run_id:
        query = query.where(CaliberEffectProposal.workflow_run_id == workflow_run_id)
    held = current_lease(session)
    rows = session.execute(query.limit(max(1, min(int(limit), MAX_LIMIT)))).all()
    return [_proposal_row(proposal, str(project_id), held) for proposal, project_id in rows]


def _proposal_row(proposal: CaliberEffectProposal, project_id: str, held: Any) -> dict[str, Any]:
    abandoned: bool | None = None
    if proposal.state == DISPATCHED and proposal.dispatch_token is not None and held is not None:
        abandoned = proposal.dispatch_token < held.fencing_token
    return {
        "source": PROPOSAL_SOURCE,
        "source_version": PROPOSAL_VERSION,
        "safe_actions": [],
        "proposal_id": proposal.proposal_id,
        "workflow_run_id": proposal.workflow_run_id,
        "project_id": project_id,
        "node_id": proposal.node_id,
        "occurrence_id": proposal.occurrence_id,
        "logical_key": proposal.logical_key,
        "connector": proposal.connector,
        "connector_version": proposal.connector_version,
        "effect_class": proposal.effect_class,
        "resource": proposal.resource,
        "intent_hash": proposal.intent_hash,
        "state": proposal.state,
        "version": proposal.version,
        "blocks_dependents": proposal.state in OPEN_STATES,
        "attempt": proposal.attempt,
        "owner_id": proposal.owner_id,
        "dispatch_token": proposal.dispatch_token,
        "abandoned": abandoned,
        "reason": proposal.reason,
        "receipt_id": proposal.receipt_id,
        "dispatched_at": _iso(proposal.dispatched_at),
        "unknown_at": _iso(proposal.unknown_at),
        "settled_at": _iso(proposal.settled_at),
        "created_at": _iso(proposal.created_at),
    }


__all__ = [
    "ANY",
    "LEGACY_SAFE_ACTIONS",
    "LEGACY_SOURCE",
    "LEGACY_VERSION",
    "OPEN",
    "PROPOSAL_SOURCE",
    "PROPOSAL_VERSION",
    "describe_sources",
    "label_legacy",
    "list_proposals",
    "proposal_states",
]
