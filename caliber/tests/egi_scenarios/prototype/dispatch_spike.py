"""DISPOSABLE spike (D1, #625, #626): persist a proposal, derive a stable key, dispatch it, survive a lost response.

#625 asks, before the production dispatcher (#520) is estimated: if the downstream commits and the response is
lost, does sending again with the *same* key produce exactly one downstream mutation? #626 adds the states
around that ambiguity: an ambiguous dispatch is ``unknown``, a dependent action is blocked while it is, and the
state is settled by asking the downstream by key (``reconcile``), including after the dispatching process was
killed.

It lives under ``tests/`` on purpose and is deleted by #520 task 1. Nothing under ``caliber/src`` may import
it (``test_src_never_imports_egi_scenarios`` enforces that). The table is created with ``create_all``, with no
Alembic migration, because a spike must not change the schema of the platform it is measuring.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import JSON, Column, MetaData, String, Table, Text, select, update
from sqlalchemy.orm import Session

from caliber.contracts import validate_record

metadata = MetaData()
spike_proposals = Table(
    "spike_proposals",
    metadata,
    Column("proposal_id", String(64), primary_key=True),
    Column("workflow_run_id", String(64), nullable=False),
    Column("node_id", String(64), nullable=False),
    Column("logical_key", String(64), nullable=False, unique=True),
    Column("intent_hash", Text, nullable=False),
    Column("args", JSON, nullable=False),
    Column("expected_resource_version", String(32)),
    Column("state", String(16), nullable=False),
    Column("downstream_request_id", String(64)),
)

STATES = ("proposed", "dispatched", "unknown", "committed", "failed")
#: ``unknown`` is left only by asking the downstream: a blind second dispatch is not a transition.
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"dispatched"},
    "dispatched": {"committed", "failed", "unknown"},
    "unknown": {"committed", "failed"},
}


class IllegalTransition(ValueError):  # noqa: N818 (name fixed by the issue)
    """The requested state change is not in :data:`TRANSITIONS`."""


class UnknownEffectError(RuntimeError):
    """An action that depends on an effect was attempted while the effect's outcome is unknown."""


CONNECTOR = "document-request"
TIMEOUT_SECONDS = 5.0


def canonical_args(args: dict[str, Any]) -> str:
    """``args`` as one string that does not depend on dict ordering."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def logical_key(workflow_run_id: str, node_id: str, args: dict[str, Any]) -> str:
    """The stable key of one effect: the same run, node and arguments always give the same key."""
    material = f"{workflow_run_id}|{node_id}|{canonical_args(args)}"
    return hashlib.sha256(material.encode()).hexdigest()


def _intent_hash(target: dict[str, str], args: dict[str, Any], expected_version: str | None) -> str:
    intent = {"target": target, "args": args, "expected_resource_version": expected_version}
    return "sha256:" + hashlib.sha256(canonical_args(intent).encode()).hexdigest()


def propose(
    session: Session,
    *,
    workflow_run_id: str,
    node_id: str,
    case_id: str,
    document_type: str,
    expected_version: str | None,
) -> dict[str, Any]:
    """Persist a ``proposed`` record for the effect and return it (it is validated against the contract)."""
    args = {"case_id": case_id, "document_type": document_type}
    target = {"connector": CONNECTOR, "resource": f"cases/{case_id}/document-requests"}
    record: dict[str, Any] = {
        "schema_version": "v1",
        "proposal_id": uuid.uuid4().hex,
        "workflow_run_id": workflow_run_id,
        "node_id": node_id,
        "logical_key": logical_key(workflow_run_id, node_id, args),
        "intent_hash": _intent_hash(target, args, expected_version),
        "effect_class": "irreversible_write",
        "target": target,
        "args": args,
        "expected_resource_version": expected_version,
        "state": "proposed",
        "tenant_id": "local",
        "region": "local",
    }
    validate_record("proposal", record)
    session.execute(
        spike_proposals.insert().values(
            proposal_id=record["proposal_id"],
            workflow_run_id=workflow_run_id,
            node_id=node_id,
            logical_key=record["logical_key"],
            intent_hash=record["intent_hash"],
            args=args,
            expected_resource_version=expected_version,
            state="proposed",
        )
    )
    session.commit()
    return record


def state_of(session: Session, proposal: dict[str, Any]) -> str:
    return str(
        session.execute(
            select(spike_proposals.c.state).where(
                spike_proposals.c.proposal_id == proposal["proposal_id"]
            )
        ).scalar_one()
    )


def transition(session: Session, proposal_id: str, to_state: str) -> None:
    """Move a proposal to ``to_state``; raise :class:`IllegalTransition` unless :data:`TRANSITIONS` allows it."""
    current = session.execute(
        select(spike_proposals.c.state).where(spike_proposals.c.proposal_id == proposal_id)
    ).scalar_one()
    if to_state not in TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"{current} -> {to_state} is not allowed")
    session.execute(
        update(spike_proposals)
        .where(spike_proposals.c.proposal_id == proposal_id)
        .values(state=to_state)
    )
    session.commit()


def load_proposal(session: Session, proposal_id: str) -> dict[str, Any]:
    """The stored proposal, as ``dispatch`` needs it (a process that did not create it can dispatch it)."""
    row = session.execute(
        select(spike_proposals).where(spike_proposals.c.proposal_id == proposal_id)
    ).one()
    return {
        "proposal_id": row.proposal_id,
        "logical_key": row.logical_key,
        "args": row.args,
        "expected_resource_version": row.expected_resource_version,
    }


def send(proposal: dict[str, Any], base_url: str) -> httpx.Response:
    """POST the proposal with its stable key and expected version. No state is touched: this is only the call."""
    args = proposal["args"]
    headers = {"Idempotency-Key": proposal["logical_key"]}
    if proposal["expected_resource_version"] is not None:
        headers["If-Match"] = proposal["expected_resource_version"]
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        return client.post(
            f"{base_url}/cases/{args['case_id']}/document-requests",
            json={"document_type": args["document_type"]},
            headers=headers,
        )


def dispatch(session: Session, proposal: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Send a ``proposed`` proposal. The state is ``dispatched`` *before* the request leaves.

    200 or 201 → ``committed`` (the downstream's request id is stored); a refusal (412, 422, any 4xx or 5xx)
    → ``failed``. A transport error (a dropped connection, a timeout) means the request may or may not have
    committed, so the state becomes ``unknown`` and the error propagates: only :func:`reconcile` settles it.
    """
    transition(session, proposal["proposal_id"], "dispatched")
    try:
        response = send(proposal, base_url)
    except httpx.HTTPError:
        transition(session, proposal["proposal_id"], "unknown")
        raise
    body: dict[str, Any] = response.json()
    if response.status_code in (200, 201):
        _store_request_id(session, proposal["proposal_id"], body.get("request_id"))
        transition(session, proposal["proposal_id"], "committed")
    else:
        transition(session, proposal["proposal_id"], "failed")
    return {"status_code": response.status_code, "body": body, "at": datetime.now(UTC).isoformat()}


def _store_request_id(session: Session, proposal_id: str, request_id: object) -> None:
    session.execute(
        update(spike_proposals)
        .where(spike_proposals.c.proposal_id == proposal_id)
        .values(downstream_request_id=None if request_id is None else str(request_id))
    )


def dependent_action(session: Session, proposal_id: str) -> str:
    """Stand-in for anything that must follow a committed effect; refuses while the outcome is unknown."""
    state = state_of(session, {"proposal_id": proposal_id})
    if state == "unknown":
        raise UnknownEffectError(
            f"proposal {proposal_id} is unknown: ask the downstream (reconcile) before going on"
        )
    return state


def reconcile(session: Session, proposal_id: str, base_url: str) -> str:
    """Settle an ``unknown`` proposal by asking the downstream whether its key committed; return the new state.

    200 → ``committed`` (the downstream's request id is stored); 404 → ``failed``. Any other answer raises and
    leaves the state ``unknown``: an unreadable system of record is not an answer.
    """
    proposal = load_proposal(session, proposal_id)
    if state_of(session, {"proposal_id": proposal_id}) != "unknown":
        raise IllegalTransition("only an unknown proposal is reconciled")
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        response = client.get(
            f"{base_url}/document-requests", params={"idempotency_key": proposal["logical_key"]}
        )
    if response.status_code == 200:
        _store_request_id(session, proposal_id, response.json().get("request_id"))
        transition(session, proposal_id, "committed")
        return "committed"
    if response.status_code == 404:
        transition(session, proposal_id, "failed")
        return "failed"
    raise RuntimeError(f"the downstream answered {response.status_code} to a query by key")
