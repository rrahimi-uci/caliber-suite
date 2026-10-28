"""V08: a connector timeout leaves the effect unknown, and a retry never makes a second mutation (B05.7, #760; ADR 0003).

The downstream is armed to commit and then answer 20 seconds late; the connector's own timeout is 2 seconds. The request
went out (and committed), the answer never arrived in time, so the outcome is ``ambiguous`` and the proposal ``unknown``:
not ``failed`` (the effect happened) and not ``committed`` (nobody read a receipt). From there:

* a retry from the node layer is refused (``UnknownEffectError``: the runtime does not run a node whose effect has no final
  outcome) and there is no edge from ``unknown`` back to ``dispatched``, so nothing sends the request a second time;
* the reconciler's lookup by the original key finds the stored request, records the verified receipt and commits;
* the downstream counted exactly one mutation throughout.

The dispatch runs in this process, with the platform's own modules (the execution-owner lease, ``dispatched`` recorded under it,
the real OpenAPI executor with the proposal's logical key, ``settle_dispatch`` for what the attempt proved).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.effects.connectors import TransportOutcome, classify_error
from caliber.effects.connectors.document_request import submit_config, submit_input
from caliber.effects.receipts import settle_dispatch
from caliber.effects.reconcile import (
    RECONCILED_COMMITTED,
    UnknownEffectError,
    effect_gate,
)
from caliber.effects.records import (
    COMMITTED,
    DISPATCHED,
    ILLEGAL_TRANSITION,
    UNKNOWN,
    ProposalError,
    proposal_state,
    proposal_view,
    transition,
)
from caliber.integrations.openapi.executor import OpenApiExecutionError, execute_openapi_http_tool
from caliber.ownership import acquire
from tests.egi_scenarios.conftest import Downstream
from tests.egi_scenarios.test_v07_restart_exactly_once import (  # noqa: F401 - fixtures re-used here
    CASE,
    DOC,
    NODE,
    _approved_proposal,
    _assert_receipt_matches_the_downstream,
    _loopback_allowed,
    _new_owner,
    _reconcile,
    _workflow_result,
    ledger,
)

CONNECTOR_TIMEOUT_SECONDS = 2.0
LATE_ANSWER_SECONDS = 20


def _dispatch_and_time_out(
    factory: sessionmaker[Session], proposal_id: str, downstream: Downstream
) -> Any:
    """Dispatch under the lease, send, and let the connector's timeout fire; return the lease and the typed outcome."""
    with factory() as session:
        lease = acquire(session, "dispatcher-1", ttl_seconds=600)
        assert lease is not None
        transition(
            session,
            proposal_id,
            DISPATCHED,
            expected_version=proposal_state(session, proposal_id).version,
            owner_id="dispatcher-1",
            attempt=1,
            lease=lease,
        )
        key = proposal_view(session, proposal_id).logical_key
        session.commit()
    downstream.set_fault("delay_response_seconds", seconds=LATE_ANSWER_SECONDS)
    started = time.monotonic()
    with pytest.raises(OpenApiExecutionError) as timed_out:
        execute_openapi_http_tool(
            execution_config=submit_config(
                downstream.base_url, timeout_seconds=CONNECTOR_TIMEOUT_SECONDS
            ),
            input_schema=None,
            input_data=submit_input(
                case_id=CASE,
                idempotency_key=key,
                expected_resource_version=1,
                body={"document_type": DOC},
            ),
        )
    assert time.monotonic() - started < LATE_ANSWER_SECONDS / 2, (
        "the connector gave up, not the downstream"
    )
    return lease, classify_error(timed_out.value)


def test_effect_timeout_leaves_unknown_never_second_mutation(
    ledger: tuple[Engine, sessionmaker[Session]],  # noqa: F811 - the V07 fixture
    downstream: Downstream,
) -> None:
    _, factory = ledger
    proposal_id = _approved_proposal(factory)
    lease, outcome = _dispatch_and_time_out(factory, proposal_id, downstream)
    assert outcome.outcome is TransportOutcome.AMBIGUOUS, "a timeout after the send proves nothing"

    with factory() as session:
        settled = settle_dispatch(
            session,
            proposal_id,
            expected_version=proposal_state(session, proposal_id).version,
            owner_id="dispatcher-1",
            attempt=1,
            lease=lease,
            outcome=outcome,
            verify=lambda: pytest.fail("an ambiguous outcome is not verified by a query"),
        )
        session.commit()
    assert (settled.state, settled.receipt_id) == (UNKNOWN, None)
    assert (settled.reason or "").startswith("ambiguous:")

    deadline = time.monotonic() + 10
    while downstream.mutations() < 1:  # the downstream commits before it delays its answer
        assert time.monotonic() < deadline, "the downstream never committed"
        time.sleep(0.05)
    assert downstream.mutations() == 1

    # A retry from the node layer is refused, and nothing can dispatch the proposal again.
    with pytest.raises(UnknownEffectError) as refused:
        effect_gate(factory, "RUN-ep")({NODE})
    assert proposal_id in str(refused.value)
    blocked = _workflow_result(factory)
    assert blocked.status == "error" and (blocked.error or "").startswith("UnknownEffectError")
    with factory() as session, pytest.raises(ProposalError) as blind:
        transition(
            session,
            proposal_id,
            DISPATCHED,
            expected_version=proposal_state(session, proposal_id).version,
            owner_id="dispatcher-1",
            attempt=2,
            lease=lease,
        )
    assert blind.value.code == ILLEGAL_TRANSITION
    assert downstream.mutations() == 1, "no second mutation"

    # The reconciler asks by the original key and commits with a verified receipt.
    results = _reconcile(factory, _new_owner(factory), downstream)
    assert [(r.proposal_id, r.outcome, r.state) for r in results] == [
        (proposal_id, RECONCILED_COMMITTED, COMMITTED)
    ]
    assert downstream.mutations() == 1
    _assert_receipt_matches_the_downstream(factory, proposal_id, downstream)
    assert _workflow_result(factory).status == "completed"
