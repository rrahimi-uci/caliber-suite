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
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberEffectProposal, CaliberStepTokenReservation
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
from caliber.integrations.openapi.executor import OpenApiExecutionError, _send_raw
from caliber.ownership import acquire
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
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
from tests.test_governed_agent_step import LIMITS as GOVERNED_LIMITS
from tests.test_governed_agent_step import (
    Canaries,
    _manifest,
    _plan,
    _proposing,
    _seed_registry,
)

CONNECTOR_TIMEOUT_SECONDS = 2.0
LATE_ANSWER_SECONDS = 20
GOVERNED_CEILING = GOVERNED_LIMITS.max_tokens


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
        _send_raw(
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
    assert blocked.status == "blocked"
    assert blocked.error == "waiting_effect:agent"
    checkpoint = blocked.steps[-1].checkpoint_state
    assert checkpoint and checkpoint["kind"] == "effect_reconciliation"
    assert checkpoint["effects"][0]["proposal_id"] == proposal_id
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


# ----------------------------------------------------------------------------------------- the token budget (B08.4, #761)


def _governed_run(
    factory: sessionmaker[Session], script: list[Any], *, budget: int, retries: int = 0
) -> tuple[Any, _CountingAdapter]:
    """Run the workflow's governed agent step through the public interpreter on the scenario database."""
    manifest = _manifest()
    manifest["nodes"]["agent"]["execution_policy"] = {"max_retries": retries}
    adapter = _CountingAdapter(step_script=script)
    result = execute(
        _plan(factory, manifest=manifest, run_token_budget=budget),
        "please ask for the pay statement",
        executor=adapter,
        extra_tools=Canaries().tools(),
    )
    return result, adapter


class _CountingAdapter(FakeWorkflowExecutor):
    """A scripted adapter that counts how often the (stand-in) provider was reached."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.provider_calls = 0

    def run_step(self, request: Any, step_input: Any) -> Any:
        self.provider_calls += 1
        return super().run_step(request, step_input)


def _reservations(factory: sessionmaker[Session]) -> list[CaliberStepTokenReservation]:
    with factory() as session:
        return list(
            session.execute(
                select(CaliberStepTokenReservation).order_by(CaliberStepTokenReservation.attempt)
            ).scalars()
        )


def test_budget_refuses_before_call(
    ledger: tuple[Engine, sessionmaker[Session]],  # noqa: F811 - the V07 fixture
    downstream: Downstream,
) -> None:
    """A step whose ceiling would take the run past its budget never reaches the provider, and nothing is left behind."""
    _, factory = ledger
    _seed_registry(factory)

    result, adapter = _governed_run(factory, [_proposing()], budget=GOVERNED_CEILING - 1)

    assert result.status == "error"
    assert result.error == (
        "ToolExecutionError: governed agent step token reservation refused: token_budget_exhausted "
        f"(budget={GOVERNED_CEILING - 1} committed=0 requested={GOVERNED_CEILING})"
    )
    assert adapter.provider_calls == 0, "the provider was not called"
    assert adapter.step_requests == []
    assert _reservations(factory) == [], "a refusal reserves nothing"
    with factory() as session:
        assert session.execute(select(CaliberEffectProposal)).scalars().all() == []
    assert downstream.mutations() == 0, "no effect reached the downstream"

    # The same step, with room for its ceiling, is admitted: the refusal was the budget's, nothing else's.
    admitted, again = _governed_run(factory, [_proposing()], budget=GOVERNED_CEILING)
    assert admitted.status == "blocked", admitted.error
    assert again.provider_calls == 1
    (row,) = _reservations(factory)
    assert (row.state, row.reserved_tokens, row.charged_tokens, row.charge_basis) == (
        "settled",
        GOVERNED_CEILING,
        15,
        "reported",
    )
    assert downstream.mutations() == 0, "a proposal is not an effect"


def test_a_timed_out_call_keeps_its_reservation_and_each_retry_is_charged_again(
    ledger: tuple[Engine, sessionmaker[Session]],  # noqa: F811 - the V07 fixture
    downstream: Downstream,
) -> None:
    """A provider timeout is an unknown outcome: its ceiling stays charged, and a retry is a new call that is reserved too.

    Two ceilings of budget, three attempts: the first two reach the provider (each times out and keeps its whole reservation),
    the third is refused before the provider is reached. The attempts add up against the run; nothing was released or counted twice.
    """
    _, factory = ledger
    _seed_registry(factory)

    result, adapter = _governed_run(
        factory,
        [TimeoutError(), TimeoutError(), _proposing()],
        budget=2 * GOVERNED_CEILING,
        retries=2,
    )

    assert result.status == "error"
    assert "token_budget_exhausted" in (result.error or "")
    assert adapter.provider_calls == 2, "the third attempt was refused before the provider"
    rows = _reservations(factory)
    assert [(r.attempt, r.state, r.charged_tokens, r.charge_basis) for r in rows] == [
        (1, "settled", GOVERNED_CEILING, "conservative"),
        (2, "settled", GOVERNED_CEILING, "conservative"),
    ]
    assert {(r.stop_reason, r.error_code) for r in rows} == {("error", "adapter_timeout")}
    assert sum(int(r.charged_tokens or 0) for r in rows) == 2 * GOVERNED_CEILING
    assert downstream.mutations() == 0
