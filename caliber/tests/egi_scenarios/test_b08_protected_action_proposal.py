"""B08.2 (#744) on PostgreSQL: a step's proposal is recorded once and asked about once, whoever else is writing.

The reference-profile database. Two workers that run the same agent step (a restarted run and the one that was never stopped) both
propose the same protected action: one effect and one live approval request come out, and neither worker is parked behind a
request the other replaced. Two occurrences of one node leave exactly one live request (the newer replaces the older). Two
resumes of a step whose request lapsed ask once. A restarted run finds its proposal instead of asking the model again.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import CaliberEffectProposal, CaliberRuntimeApprovalRequest
from caliber.effects import step_proposal
from caliber.effects.step_proposal import (
    prior_proposal,
    reask_approval,
    record_step_proposal,
    untrusted_tool,
)
from caliber.workflows.agent_step import ScriptedStep
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from tests.egi_scenarios.conftest import ReferenceApp, Workspaces
from tests.egi_scenarios.test_v04_stale_approval import NODE, _waiting_run
from tests.test_governed_agent_step import (
    PROTECTED,
    Canaries,
    _action,
    _context,
    _plan,
    _proposing,
    _request,
    _result,
    _seed_registry,
)


def _postgres_only(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip("row-lock ordering is proven on PostgreSQL; SQLite serializes every writer")


def _proposals(app: ReferenceApp) -> list[CaliberEffectProposal]:
    with app.session_factory() as session:
        return list(session.execute(select(CaliberEffectProposal)).scalars())


def _requests(app: ReferenceApp) -> list[CaliberRuntimeApprovalRequest]:
    with app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberRuntimeApprovalRequest).where(
                    CaliberRuntimeApprovalRequest.proposal_id.is_not(None)
                )
            ).scalars()
        )


def _in_threads(*targets: Callable[[], Any]) -> list[Any]:
    """Run each target on its own thread and return what each returned, re-raising the first thread's exception."""
    outcomes: list[Any] = [None] * len(targets)
    errors: list[BaseException] = []

    def wrap(index: int, target: Callable[[], Any]) -> Callable[[], None]:
        def run() -> None:
            try:
                outcomes[index] = target()
            except BaseException as exc:
                errors.append(exc)

        return run

    threads = [threading.Thread(target=wrap(i, t)) for i, t in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads), "a writer is stuck"
    if errors:
        raise errors[0]
    return outcomes


def test_two_workers_proposing_one_step_record_one_effect_and_ask_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    context = _context(reference_app.session_factory, workflow_run_id=run_id)
    request = _request(workflow_run_id=run_id, node_id=NODE)
    result = _result(request)
    tools = {PROTECTED: untrusted_tool(PROTECTED, (PROTECTED, "1.0"))}

    # Worker A holds its uncommitted proposal and request while worker B arrives and has to wait for it.
    a_inside, b_started = threading.Event(), threading.Event()
    real_create = step_proposal.create_approval_request

    def create_while_b_waits(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "worker-a":
            a_inside.set()
            assert b_started.wait(30)
            time.sleep(0.5)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(step_proposal, "create_approval_request", create_while_b_waits)
    outcomes: dict[str, Any] = {}

    def worker(name: str) -> None:
        outcomes[name] = record_step_proposal(context, request, result, tools)

    first = threading.Thread(name="worker-a", target=worker, args=("a",))
    second = threading.Thread(name="worker-b", target=worker, args=("b",))
    first.start()
    assert a_inside.wait(30)
    b_started.set()
    second.start()
    first.join(60)
    second.join(60)
    assert not (first.is_alive() or second.is_alive())

    assert set(outcomes) == {"a", "b"}
    assert sorted(item.created for item in outcomes.values()) == [False, True]
    assert outcomes["a"].proposal_id == outcomes["b"].proposal_id
    assert outcomes["a"].approval_request_id == outcomes["b"].approval_request_id
    assert len(_proposals(reference_app)) == 1
    (live,) = _requests(reference_app)
    assert (live.status, live.superseded_by) == ("pending", None)
    assert live.runtime_approval_id == outcomes["a"].approval_request_id


def test_several_occurrences_of_one_node_leave_exactly_one_live_request_and_no_deadlock(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    context = _context(reference_app.session_factory, workflow_run_id=run_id)
    tools = {PROTECTED: untrusted_tool(PROTECTED, (PROTECTED, "1.0"))}
    occurrences = [f"occ-{number}" for number in range(6)]
    barrier = threading.Barrier(len(occurrences))

    def propose(occurrence: str) -> Callable[[], Any]:
        request = _request(
            workflow_run_id=run_id,
            node_id=NODE,
            occurrence_id=occurrence,
            step_request_id=f"step-{occurrence}",
        )

        def run() -> Any:
            barrier.wait(30)
            return record_step_proposal(context, request, _result(request), tools)

        return run

    outcomes = _in_threads(*(propose(occurrence) for occurrence in occurrences))

    assert len({item.proposal_id for item in outcomes}) == len(occurrences)
    assert all(item.created for item in outcomes)
    assert len(_proposals(reference_app)) == len(occurrences)
    requests = {item.runtime_approval_id: item for item in _requests(reference_app)}
    assert len(requests) == len(occurrences)
    live = [item for item in requests.values() if item.status == "pending"]
    assert len(live) == 1, "the node has exactly one question open"
    replaced = [item for item in requests.values() if item.status == "superseded"]
    assert len(replaced) == len(occurrences) - 1
    assert {item.superseded_by for item in replaced} <= set(requests)


def test_two_resumes_of_a_lapsed_request_ask_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    context = _context(reference_app.session_factory, workflow_run_id=run_id)
    request = _request(workflow_run_id=run_id, node_id=NODE)
    recorded = record_step_proposal(
        context,
        request,
        _result(request),
        {PROTECTED: untrusted_tool(PROTECTED, (PROTECTED, "1.0"))},
    )
    with reference_app.session_factory() as session:
        old = session.get(CaliberRuntimeApprovalRequest, recorded.approval_request_id)
        assert old is not None
        old.status = "superseded"
        old.superseded_by = "rap-gone"
        session.commit()
    prior = prior_proposal(reference_app.session_factory, run_id, NODE, request.occurrence_id)
    assert prior is not None
    barrier = threading.Barrier(2)

    def resume() -> Any:
        barrier.wait(30)
        return reask_approval(context, prior)

    first, second = _in_threads(resume, resume)

    assert first.approval_request_id == second.approval_request_id
    assert first.approval_request_id != recorded.approval_request_id
    live = [item for item in _requests(reference_app) if item.status == "pending"]
    assert [item.runtime_approval_id for item in live] == [first.approval_request_id]


def test_a_restarted_run_finds_its_proposal_and_does_not_ask_the_model_again(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    run_id = _waiting_run(reference_app)
    _seed_registry(reference_app.session_factory)
    plan = _plan(reference_app.session_factory, workflow_run_id=run_id)
    canaries = Canaries()
    executor = FakeWorkflowExecutor(step_script=[_proposing()])

    first = execute(
        plan, "ask for the pay statement", executor=executor, extra_tools=canaries.tools()
    )
    assert first.status == "blocked" and first.error == "waiting_approval:agent"
    (proposal,) = _proposals(reference_app)
    (request,) = _requests(reference_app)

    restarted = FakeWorkflowExecutor(
        step_script=[ScriptedStep("proposed_action", (_action(args={"document_type": "other"}),))]
    )
    again = execute(
        plan, "ask for the pay statement", executor=restarted, extra_tools=canaries.tools()
    )

    assert again.status == "blocked" and again.error == "waiting_approval:agent"
    assert restarted.step_requests == [], "the occurrence already has its logical effect"
    assert [item.proposal_id for item in _proposals(reference_app)] == [proposal.proposal_id]
    assert [item.runtime_approval_id for item in _requests(reference_app)] == [
        request.runtime_approval_id
    ]
    assert canaries.mutations == []
