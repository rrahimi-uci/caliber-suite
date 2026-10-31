"""V07: a dispatcher killed at any moment never causes a second mutation (B05.5b, #742; ADR 0003).

A dispatch attempt runs in a real process (``harness/effect_child.py``: the execution-owner lease, ``dispatched`` recorded under
it, the effect sent through the real OpenAPI executor with the proposal's logical key) that the test kills at an exact moment.
A new owner then takes the lease and runs the reconciler against the downstream fixture process; the downstream counts its own
mutations. In every case the new owner asks by key and *never sends the request again*:

* killed before the send: nothing was committed, and the reconciler cannot know that (a 404 is delayed visibility as far as
  anyone can tell), so the proposal stays ``unknown`` and everything that depends on it stays blocked;
* killed after the downstream committed, while its answer was in flight: ``committed`` with a verified receipt;
* killed after the answer, before the outcome was persisted: the same.

The old owner's lease is expired by ``release`` (the ownership module's own expiry-by-database-time is covered by its tests).

B05.5d (#997) repeats the recoveries with the owner-only loop (``EffectReconcilerTask``) in place of a hand-called pass: it recovers
a killed dispatch by key, keeps an unprovable one ``unknown`` pass after pass without ever sending, and a loop whose lease is no
longer honoured changes nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberEffectReceipt
from caliber.effects.connectors.document_request import build_intent
from caliber.effects.reconcile import (
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    UnknownEffectError,
    ask_downstream,
    effect_gate,
    reconcile_pass,
)
from caliber.effects.records import (
    APPROVED,
    COMMITTED,
    DISPATCHED,
    ILLEGAL_TRANSITION,
    POLICY_DECISION,
    UNKNOWN,
    Approval,
    ProposalError,
    proposal_state,
    proposal_view,
    record_proposal,
    transition,
)
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy, execute_openapi_http_tool
from caliber.orchestrator.effect_reconciler import COMPLETE, LEASE_REFUSED, EffectReconcilerTask
from caliber.ownership import acquire, current_lease, release
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    RuntimeResumeCheckpoint,
    execute,
)
from tests.dialect_helpers import DIALECTS
from tests.egi_scenarios.conftest import Downstream
from tests.test_effect_proposals import _seed_run
from tests.test_effect_records import AT, _decision
from tests.test_event_outbox import _count, _database
from tests.workflow_helpers import fake_resolver, make_manifest

CASE = "MORT-2026-000184"
DOC = "pay_statement"
PROJECT = "PRJ-ep"
CALIBER_DIR = Path(__file__).resolve().parents[2]
MARKER_SECONDS = 60.0
NODE = "agent"  # the node of the workflow below that asked for the effect


@pytest.fixture(autouse=True)
def _loopback_allowed() -> Iterator[None]:
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield
    bind_egress_policy(None)


@pytest.fixture(params=DIALECTS)
def ledger(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


def _approved_proposal(factory: sessionmaker[Session]) -> str:
    """A read proposal for the document-request connector, approved and not yet dispatched."""
    with factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id="RUN-ep",
            node_id=NODE,
            occurrence_id="occ-1",
            intent=build_intent(
                case_id=CASE, document_type=DOC, expected_resource_version=1, effect_class="read"
            ),
        )
        decision = _decision(
            session, recorded.proposal_id, decision_id=f"pd-{recorded.proposal_id}"
        )
        transition(
            session,
            recorded.proposal_id,
            APPROVED,
            expected_version=proposal_state(session, recorded.proposal_id).version,
            approval=Approval(POLICY_DECISION, decision),
            now=AT,
        )
        session.commit()
        return str(recorded.proposal_id)


def _start_child(
    engine: Engine,
    downstream: Downstream,
    proposal_id: str,
    stop_at: str,
    marker: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.egi_scenarios.harness.effect_child",
            "--base-url",
            downstream.base_url,
            "--proposal-id",
            proposal_id,
            "--owner-id",
            "dispatcher-1",
            "--case-id",
            CASE,
            "--document-type",
            DOC,
            "--expected-version",
            "1",
            "--stop-at",
            stop_at,
            "--marker",
            str(marker),
        ],
        cwd=CALIBER_DIR,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(CALIBER_DIR), str(CALIBER_DIR / "src")]),
            # in the environment, not the command line, where `ps` would show a password
            "EFFECT_CHILD_DB_URL": engine.url.render_as_string(hide_password=False),
        },
    )


def _wait_for(condition: Any, child: subprocess.Popen[bytes], what: str) -> None:
    deadline = time.monotonic() + MARKER_SECONDS
    while not condition():
        assert child.poll() is None, f"the dispatching process exited before {what}"
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _kill(child: subprocess.Popen[bytes]) -> None:
    try:
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover - the kill above did not take
            child.kill()
    assert child.returncode == -9


def _marked(marker: Path, text: str) -> bool:
    return marker.exists() and marker.read_text() == text


def _new_owner(factory: sessionmaker[Session]) -> Any:
    """The old owner's lease lapses; a new process acquires it with a higher token."""
    with factory() as session:
        old = current_lease(session)
        assert old is not None and old.owner_id == "dispatcher-1"
        assert release(session, old)
        lease = acquire(session, "reconciler-1", ttl_seconds=600)
        assert lease is not None and lease.fencing_token == old.fencing_token + 1
        session.commit()
    return lease


def _reconcile(factory: sessionmaker[Session], lease: Any, downstream: Downstream) -> list[Any]:
    return reconcile_pass(
        factory,
        lease=lease,
        targets={("document-request", PROJECT): downstream.base_url},
        lookup=ask_downstream,
        now=AT,
    )


def _workflow_result(
    factory: sessionmaker[Session],
    *,
    resume_checkpoint: RuntimeResumeCheckpoint | None = None,
) -> Any:
    """Run a start -> agent -> output workflow whose ``agent`` node asked for the effect, with the effect gate in place."""
    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="v07")
    plan = RuntimePlan(ir=ir, resolver=fake_resolver())
    plan.effect_gate = effect_gate(factory, "RUN-ep")
    return execute(
        plan,
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=resume_checkpoint,
    )


def test_kill_before_dispatch(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """The attempt was recorded as dispatched and the process died before sending: nothing is re-sent, nothing is guessed."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    marker = tmp_path / "marker"
    child = _start_child(engine, downstream, proposal_id, "before_send", marker)
    try:
        _wait_for(lambda: _marked(marker, "dispatched"), child, "the attempt was recorded")
        _kill(child)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
    assert downstream.mutations() == 0
    with factory() as session:
        assert proposal_state(session, proposal_id).state == DISPATCHED

    lease = _new_owner(factory)
    results = _reconcile(factory, lease, downstream)
    assert [(r.proposal_id, r.outcome, r.state) for r in results] == [
        (proposal_id, STILL_UNKNOWN, UNKNOWN)
    ], "a request that is not found may still be in flight: it is not proof that nothing committed"
    assert downstream.mutations() == 0, "the reconciler asks; it never sends"
    with factory() as session:
        state = proposal_state(session, proposal_id)
        assert (state.state, state.owner_id, state.attempt) == (UNKNOWN, "dispatcher-1", 1)
        with pytest.raises(ProposalError) as blind:  # and nothing can dispatch it a second time
            transition(
                session,
                proposal_id,
                DISPATCHED,
                expected_version=state.version,
                owner_id="reconciler-1",
                attempt=2,
                lease=lease,
            )
        assert blind.value.code == ILLEGAL_TRANSITION
    assert _count(factory, CaliberEffectReceipt) == 0


def test_kill_after_downstream_commit(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """The downstream committed and was slow to answer; the dispatcher died waiting. One mutation, found by key."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    downstream.set_fault("delay_response_seconds", seconds=20)
    marker = tmp_path / "marker"
    child = _start_child(engine, downstream, proposal_id, "after_response", marker)
    try:
        _wait_for(lambda: downstream.mutations() >= 1, child, "the downstream committed")
        _kill(child)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
    assert marker.read_text() != "responded", "the dead process never saw the answer"
    with factory() as session:
        assert proposal_state(session, proposal_id).state == DISPATCHED

    parked = _workflow_result(factory)
    assert parked.status == "blocked"
    parked_checkpoint = parked.steps[-1].checkpoint_state
    assert parked_checkpoint and parked_checkpoint["kind"] == "effect_reconciliation"

    results = _reconcile(factory, _new_owner(factory), downstream)
    assert [(r.proposal_id, r.outcome, r.state) for r in results] == [
        (proposal_id, RECONCILED_COMMITTED, COMMITTED)
    ]
    assert downstream.mutations() == 1
    _assert_receipt_matches_the_downstream(factory, proposal_id, downstream)
    resumed = _workflow_result(
        factory,
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id=str(parked_checkpoint["node_id"]),
            checkpoint_kind="effect_reconciliation",
            input_by_port=dict(parked_checkpoint["input_by_port"]),
            replay_output=False,
        ),
    )
    assert resumed.status == "completed", resumed.error
    assert downstream.mutations() == 1, "resuming the parked run must not resend the effect"


def test_kill_before_receipt_persist(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """The downstream answered and the dispatcher died before it recorded anything: the answer is lost, the effect is not."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    marker = tmp_path / "marker"
    child = _start_child(engine, downstream, proposal_id, "after_response", marker)
    try:
        _wait_for(lambda: _marked(marker, "responded"), child, "the downstream answered")
        _kill(child)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
    with factory() as session:
        assert proposal_state(session, proposal_id).state == DISPATCHED
    assert _count(factory, CaliberEffectReceipt) == 0
    assert downstream.mutations() == 1

    results = _reconcile(factory, _new_owner(factory), downstream)
    assert [(r.outcome, r.state) for r in results] == [(RECONCILED_COMMITTED, COMMITTED)]
    assert downstream.mutations() == 1, "still one: the reconciler never re-sent"
    _assert_receipt_matches_the_downstream(factory, proposal_id, downstream)


def _assert_receipt_matches_the_downstream(
    factory: sessionmaker[Session], proposal_id: str, downstream: Downstream
) -> None:
    with factory() as session:
        view = proposal_view(session, proposal_id)
        state = proposal_state(session, proposal_id)
        stored = downstream.query(view.logical_key)
        assert stored is not None
        receipt = session.execute(
            select(CaliberEffectReceipt).where(CaliberEffectReceipt.proposal_id == proposal_id)
        ).scalar_one()
        assert state.state == COMMITTED and state.receipt_id == receipt.receipt_id
        assert receipt.downstream_request_id == stored["request_id"]
        assert receipt.logical_key == view.logical_key and receipt.verified_by == "query"


def test_dependent_node_blocked_while_unknown(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """While the effect is unknown nothing that depends on it runs; when the downstream shows it, the run goes through."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    marker = tmp_path / "marker"
    child = _start_child(engine, downstream, proposal_id, "before_send", marker)
    try:
        _wait_for(lambda: _marked(marker, "dispatched"), child, "the attempt was recorded")
        _kill(child)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
    lease = _new_owner(factory)
    assert _reconcile(factory, lease, downstream)[0].outcome == STILL_UNKNOWN

    blocked = _workflow_result(factory)
    assert blocked.status == "blocked"
    assert blocked.error == "waiting_effect:agent"
    effect_checkpoint = blocked.steps[-1].checkpoint_state
    assert effect_checkpoint and effect_checkpoint["kind"] == "effect_reconciliation"
    assert effect_checkpoint["effects"][0]["proposal_id"] == proposal_id
    assert [step.node_id for step in blocked.steps] == ["start", "agent"], (
        "the effect's own node did not run again"
    )
    with pytest.raises(UnknownEffectError):
        effect_gate(factory, "RUN-ep")({NODE, "final"})

    # delayed visibility: the request that was "in flight" lands at the downstream late, from somewhere else
    with factory() as session:
        key = proposal_view(session, proposal_id).logical_key
    late = execute_openapi_http_tool(
        execution_config=_submit_config(downstream),
        input_schema=None,
        input_data=_submit_input(key),
    )
    assert late["status_code"] == 201 and downstream.mutations() == 1
    assert _reconcile(factory, lease, downstream)[0].outcome == RECONCILED_COMMITTED
    assert downstream.mutations() == 1
    with factory() as session:
        assert proposal_state(session, proposal_id).state == COMMITTED

    completed = _workflow_result(
        factory,
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id=str(effect_checkpoint["node_id"]),
            checkpoint_kind="effect_reconciliation",
            input_by_port=dict(effect_checkpoint["input_by_port"]),
            replay_output=False,
        ),
    )
    assert completed.status == "completed", completed.error


def _submit_config(downstream: Downstream) -> dict[str, Any]:
    from caliber.effects.connectors.document_request import submit_config

    return submit_config(downstream.base_url)


def _submit_input(key: str) -> dict[str, Any]:
    from caliber.effects.connectors.document_request import submit_input

    return submit_input(
        case_id=CASE, idempotency_key=key, expected_resource_version=1, body={"document_type": DOC}
    )


# ---------------------------------------------------------------- the owner loop drives the recovery (B05.5d, #997)


def _loop(
    factory: sessionmaker[Session], downstream: Downstream, lease_provider: Any
) -> EffectReconcilerTask:
    """The owner-only loop, with the downstream fixture as the bound connector and the real lookup through the executor."""
    bindings = json.dumps(
        [
            {
                "connector": "document-request",
                "project_id": PROJECT,
                "secret_source": "env:UNUSED",
                "base_url": downstream.base_url,
            }
        ]
    )
    return EffectReconcilerTask(
        factory,
        SimpleNamespace(effect_callback_connectors=bindings),
        lease_provider=lease_provider,
    )


def _killed(
    engine: Engine,
    downstream: Downstream,
    proposal_id: str,
    stop_at: str,
    marker: Path,
    wait: Any,
) -> None:
    child = _start_child(engine, downstream, proposal_id, stop_at, marker)
    try:
        _wait_for(wait, child, "the attempt reached the kill point")
        _kill(child)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()


def test_the_owner_loop_finds_a_killed_dispatchs_effect_by_key_and_never_sends_it_again(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """The dispatcher died after the downstream committed. The new owner's *loop* (not a hand-called pass) abandons the dead
    attempt, asks by key, commits with a verified receipt, and the downstream still counts one mutation."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    downstream.set_fault("delay_response_seconds", seconds=20)
    marker = tmp_path / "marker"
    _killed(
        engine,
        downstream,
        proposal_id,
        "after_response",
        marker,
        lambda: downstream.mutations() >= 1,
    )
    with factory() as session:
        assert proposal_state(session, proposal_id).state == DISPATCHED

    lease = _new_owner(factory)
    report = _loop(factory, downstream, lambda: lease).tick()

    assert report is not None and report.ended == COMPLETE
    assert [(r.proposal_id, r.outcome, r.state) for r in report.results] == [
        (proposal_id, RECONCILED_COMMITTED, COMMITTED)
    ]
    assert downstream.mutations() == 1
    _assert_receipt_matches_the_downstream(factory, proposal_id, downstream)
    again = _loop(factory, downstream, lambda: lease).tick()
    assert again is not None and again.results == (), "nothing is left for the next pass"
    assert downstream.mutations() == 1


def test_the_owner_loop_leaves_an_unprovable_kill_unknown_pass_after_pass_until_the_request_lands(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """Killed before the send: nothing committed, and no pass can know that. The loop keeps the effect ``unknown`` and keeps
    asking; it never fails it and never sends it. When the request lands late from elsewhere, the next pass commits it."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    marker = tmp_path / "marker"
    _killed(
        engine,
        downstream,
        proposal_id,
        "before_send",
        marker,
        lambda: _marked(marker, "dispatched"),
    )
    lease = _new_owner(factory)
    loop = _loop(factory, downstream, lambda: lease)
    for _ in range(3):
        report = loop.tick()
        assert report is not None
        assert [(r.outcome, r.state) for r in report.results] == [(STILL_UNKNOWN, UNKNOWN)]
        assert downstream.mutations() == 0, "the loop asks; it never sends"
    with pytest.raises(UnknownEffectError):
        effect_gate(factory, "RUN-ep")({NODE})

    with factory() as session:
        key = proposal_view(session, proposal_id).logical_key
    late = execute_openapi_http_tool(
        execution_config=_submit_config(downstream),
        input_schema=None,
        input_data=_submit_input(key),
    )
    assert late["status_code"] == 201 and downstream.mutations() == 1
    report = loop.tick()
    assert report is not None
    assert [(r.outcome, r.state) for r in report.results] == [(RECONCILED_COMMITTED, COMMITTED)]
    assert downstream.mutations() == 1


def test_a_loop_that_lost_its_lease_after_the_kill_changes_nothing_and_the_new_owners_loop_recovers(
    ledger: tuple[Engine, sessionmaker[Session]], downstream: Downstream, tmp_path: Path
) -> None:
    """The old owner's loop still holds the lease it captured, which the database no longer honours: it records nothing and
    asks nothing; the new owner's loop does the recovery. No new work starts after the lease is lost."""
    engine, factory = ledger
    proposal_id = _approved_proposal(factory)
    marker = tmp_path / "marker"
    _killed(
        engine,
        downstream,
        proposal_id,
        "before_send",
        marker,
        lambda: _marked(marker, "dispatched"),
    )
    with factory() as session:
        stale = current_lease(session)
        assert stale is not None and stale.owner_id == "dispatcher-1"
    fresh = _new_owner(factory)

    old = _loop(factory, downstream, lambda: stale).tick()
    assert old is not None and old.ended == LEASE_REFUSED and old.results == ()
    with factory() as session:
        assert proposal_state(session, proposal_id).state == DISPATCHED, "untouched"
    assert downstream.mutations() == 0

    new = _loop(factory, downstream, lambda: fresh).tick()
    assert new is not None and new.ended == COMPLETE
    assert [(r.outcome, r.state) for r in new.results] == [(STILL_UNKNOWN, UNKNOWN)]
    assert downstream.mutations() == 0
