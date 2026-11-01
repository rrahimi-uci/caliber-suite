#!/usr/bin/env python3
"""Seeds the approval panel's Playwright fixtures (B06.5, #743) into the database the E2E server will open.

``caliber-ui/e2e/approval-panel.spec.ts`` drives the exact-intent approval panel through the real decision routes, so each
scenario needs a run that is parked at an approval with a request bound to an effect proposal. Nothing in the running
platform creates such a request from the browser yet (the writer, ``create_approval_request``, is called by the runtime
paths that land with the governed-effect work), so this script records them directly, through the same writers the
runtime uses: ``record_proposal`` and ``create_approval_request``. Nothing is faked: the rows are exactly what those writers
store, and the console reads them through the same routes as for any run.

Invoked by ``scripts/run-playwright-server.sh`` against a fresh database, before the server starts (the same slot as
``two_week_alpha_e2e_seed.py``), and runnable by hand against a disposable database. It is not idempotent: it inserts fixed
ids and refuses to run twice against the same database.

The scenarios (one run each, all in workflow ``WF-E2E-APPROVALS``):

``WR-E2E-QUORUM``      two approvals required: approve (1 of 2), then Approve is refused for the same person; then reject.
``WR-E2E-KEYBOARD``    one approval required: decided from the keyboard alone.
``WR-E2E-EXPIRED``     a request whose window has closed: read-only, with the expiry message.
``WR-E2E-SUPERSEDED``  an old request replaced by a newer one: the old one is read-only and links to the new one.
``WR-E2E-SELF``        the signed-in user requested it and self-approval is off: Approve is unavailable, Reject is not.
"""

# This is a bootstrap tool: its printed lines are its operator-facing output (as in two_week_alpha_e2e_seed.py).
# ruff: noqa: T201

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
)
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.effects.records import Intent, record_proposal
from caliber.ids import new_workflow_run_checkpoint_id
from caliber.workflows.approval_policy import ApprovalPolicy
from caliber.workflows.approval_requests import create_approval_request

WORKFLOW_ID = "WF-E2E-APPROVALS"
NODE_ID = "human_gate"
#: The user the E2E server signs in (``CALIBER_DEV_USER``); it holds the approver scope.
SIGNED_IN_USER = "admin"
#: A secret-looking argument value the panel must never show: the fixture proves it by carrying one.
ARGUMENT_CANARY = "e2e-secret-argument-value-do-not-show"


def _run(session: Any, run_id: str, *, created_by: str) -> None:
    now = datetime.now(timezone.utc)
    checkpoint_id = new_workflow_run_checkpoint_id()
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id=WORKFLOW_ID,
            status="waiting_approval",
            source="manual",
            created_by=created_by,
            current_node_id=NODE_ID,
            queued_at=now,
            started_at=now,
            last_heartbeat_at=now,
            error_code="waiting_approval",
            error_summary="waiting for runtime approval decision",
            summary={"resume_checkpoint_id": checkpoint_id, "output": "", "steps": []},
        )
    )
    session.flush()
    session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id=NODE_ID,
            state_blob={
                "kind": "runtime_approval",
                "node_id": NODE_ID,
                "output": "approval requested",
                "input_by_port": {"input": "hello"},
            },
        )
    )


def _request(
    session: Any,
    run_id: str,
    *,
    occurrence: str,
    requested_by: str = "editor_a",
    approval_count: int = 1,
    allow_self: bool = True,
    window_start: datetime | None = None,
) -> CaliberRuntimeApprovalRequest:
    recorded = record_proposal(
        session,
        workflow_run_id=run_id,
        node_id=NODE_ID,
        occurrence_id=occurrence,
        intent=Intent(
            connector="document-request",
            connector_version="1.0.0",
            effect_class="irreversible_write",
            resource="cases/MORT-2026-000184/document-requests",
            args={
                "document_type": "pay_statement",
                "customer": {"email": ARGUMENT_CANARY},
                "api_key": ARGUMENT_CANARY,
            },
            expected_resource_version="7",
        ),
    )
    proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
    assert proposal is not None
    return create_approval_request(
        session,
        proposal=proposal,
        policy=ApprovalPolicy(allow_self_approval=allow_self, approval_count=approval_count),
        evidence_hash="sha256:" + "e" * 64,
        requested_by=requested_by,
        ttl_seconds=3600,
        now=window_start or datetime.now(timezone.utc),
    )


def main() -> int:
    config = CaliberConfig.load()
    engine = create_engine_from_config(config)
    try:
        factory = sessionmaker_from_engine(engine)
        with factory() as session:
            if session.get(CaliberWorkflow, WORKFLOW_ID) is not None:
                print("error: the approval-panel fixtures are already seeded", file=sys.stderr)
                return 1
            session.add(
                CaliberWorkflow(
                    workflow_id=WORKFLOW_ID,
                    name="E2E approval panel",
                    owner=SIGNED_IN_USER,
                    visibility="public",
                )
            )
            session.flush()
            seeded: dict[str, list[str]] = {}

            _run(session, "WR-E2E-QUORUM", created_by="editor_a")
            seeded["WR-E2E-QUORUM"] = [
                _request(
                    session, "WR-E2E-QUORUM", occurrence="q", approval_count=2
                ).runtime_approval_id
            ]
            _run(session, "WR-E2E-KEYBOARD", created_by="editor_a")
            seeded["WR-E2E-KEYBOARD"] = [
                _request(session, "WR-E2E-KEYBOARD", occurrence="k").runtime_approval_id
            ]
            _run(session, "WR-E2E-EXPIRED", created_by="editor_a")
            seeded["WR-E2E-EXPIRED"] = [
                _request(
                    session,
                    "WR-E2E-EXPIRED",
                    occurrence="x",
                    window_start=datetime.now(timezone.utc) - timedelta(hours=3),
                ).runtime_approval_id
            ]
            _run(session, "WR-E2E-SUPERSEDED", created_by="editor_a")
            old = _request(session, "WR-E2E-SUPERSEDED", occurrence="s1")
            new = _request(session, "WR-E2E-SUPERSEDED", occurrence="s2")
            seeded["WR-E2E-SUPERSEDED"] = [old.runtime_approval_id, new.runtime_approval_id]
            _run(session, "WR-E2E-SELF", created_by=SIGNED_IN_USER)
            seeded["WR-E2E-SELF"] = [
                _request(
                    session,
                    "WR-E2E-SELF",
                    occurrence="m",
                    requested_by=SIGNED_IN_USER,
                    allow_self=False,
                ).runtime_approval_id
            ]
            session.commit()
    finally:
        engine.dispose()
    print(f">> approval-panel E2E fixtures ready: {json.dumps(seeded)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
