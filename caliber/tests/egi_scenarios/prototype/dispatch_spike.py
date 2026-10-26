"""DISPOSABLE spike (D1, #625): persist a proposal, derive a stable key, dispatch it, survive a lost response.

It answers one question before the production dispatcher (#520) is estimated: if the downstream commits and
the response is lost, does dispatching again with the *same* key produce exactly one downstream mutation?

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
)

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


def _set_state(session: Session, proposal: dict[str, Any], state: str) -> None:
    session.execute(
        update(spike_proposals)
        .where(spike_proposals.c.proposal_id == proposal["proposal_id"])
        .values(state=state)
    )
    session.commit()


def dispatch(session: Session, proposal: dict[str, Any], base_url: str) -> dict[str, Any]:
    """POST the proposal to the downstream with its stable key and expected version.

    The state is ``dispatched`` *before* the request leaves, so a crash or a lost response leaves a proposal
    that says "may have committed". It becomes ``committed`` on 200 or 201 and ``failed`` on a refusal
    (412, 422, 4xx, 5xx). A transport error (a dropped connection) propagates and the state stays
    ``dispatched``: that is the ambiguity this spike exists to resolve by dispatching again.
    """
    args = proposal["args"]
    headers = {"Idempotency-Key": proposal["logical_key"]}
    if proposal["expected_resource_version"] is not None:
        headers["If-Match"] = proposal["expected_resource_version"]
    _set_state(session, proposal, "dispatched")
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{base_url}/cases/{args['case_id']}/document-requests",
            json={"document_type": args["document_type"]},
            headers=headers,
        )
    _set_state(session, proposal, "committed" if response.status_code in (200, 201) else "failed")
    body: dict[str, Any] = response.json()
    return {"status_code": response.status_code, "body": body, "at": datetime.now(UTC).isoformat()}
