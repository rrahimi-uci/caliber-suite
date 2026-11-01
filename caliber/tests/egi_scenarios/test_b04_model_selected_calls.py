"""B04.6 (#768), the public path: an agent step's model chooses a protected tool, and the platform proposes instead of acting.

Against the reference app's database (PostgreSQL in the "EGI scenarios (PostgreSQL)" job, SQLite on a laptop): a governed agent
step whose model picks a protected **registered**, **OpenAPI** or **MCP** tool leaves exactly one ``proposed`` effect and one
pending approval request, the run parked at ``waiting_approval:<node>``, and the downstream untouched (measured at the tool, the
HTTP server and the MCP server, not in CALIBER's logs). A second worker that proposes the same step, or a restarted run, adds
nothing and asks the model nothing.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import CaliberEffectProposal, CaliberRuntimeApprovalRequest
from caliber.effects.step_proposal import record_step_proposal
from caliber.workflows import runtime as rt
from caliber.workflows.agent_step import AgentStepRequest, ScriptedStep, answer
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from tests.egi_scenarios.conftest import ReferenceApp, Workspaces
from tests.egi_scenarios.test_v04_stale_approval import NODE, _waiting_run
from tests.mcp_fakes import SERVER_ID, FakeMcp, seed_server, use_database
from tests.test_model_selected_calls import (
    LIMITS,
    LOOPBACK,
    USAGE,
    agent_manifest,
    mcp_binding,
    registered,
)
from tests.test_model_selected_proposals import (
    _mcp_action,
    _openapi_action,
    _registered_action,
    governed,
)
from tests.test_openapi_governed_dispatch import Server, config, entry_for
from tests.test_openapi_governed_dispatch import seed_row as seed_openapi_row
from tests.test_registered_tool_dispatch import Tool, seed_row, tool_entry

pytestmark = pytest.mark.usefixtures("fixed_step_clock")


def _rows(app: ReferenceApp) -> tuple[list[Any], list[Any]]:
    with app.session_factory() as session:
        return (
            list(session.execute(select(CaliberEffectProposal)).scalars()),
            list(
                session.execute(
                    select(CaliberRuntimeApprovalRequest).where(
                        CaliberRuntimeApprovalRequest.proposal_id.is_not(None)
                    )
                ).scalars()
            ),
        )


def _scripted(action: Any) -> FakeWorkflowExecutor:
    return FakeWorkflowExecutor(
        step_script=[ScriptedStep("proposed_action", (action,), usage=USAGE)]
    )


def _registered_world(app: ReferenceApp, run_id: str) -> Any:
    entry = tool_entry("open_ticket", "write")
    seed_row(app.session_factory, entry)
    return governed(
        app.session_factory,
        agent_manifest({"open_ticket": registered("open_ticket")}),
        [entry],
        workflow_run_id=run_id,
        tool_backends={("open_ticket", "1.0"): "registered_tool"},
    )


def _assert_parked_with_one_proposal(app: ReferenceApp, result: Any) -> CaliberEffectProposal:
    assert result.status == "blocked" and result.error == f"waiting_approval:{'agent'}"
    proposals, requests = _rows(app)
    assert [p.state for p in proposals] == ["proposed"]
    assert [r.status for r in requests] == ["pending"]
    assert requests[0].proposal_id == proposals[0].proposal_id
    assert requests[0].intent_hash == proposals[0].intent_hash
    return proposals[0]


def test_a_model_chosen_registered_write_is_a_proposal_and_the_tool_never_runs(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    run_id = _waiting_run(reference_app)
    world = _registered_world(reference_app, run_id)
    tool = Tool()

    result = execute(
        world.plan,
        "go",
        executor=_scripted(_registered_action()),
        extra_tools={"open_ticket": tool},
    )

    proposal = _assert_parked_with_one_proposal(reference_app, result)
    assert proposal.connector == "registered_tool" and proposal.workflow_run_id == run_id
    assert tool.calls == [], "downstream.mutations() == 0"


def test_a_model_chosen_openapi_write_is_a_proposal_and_nothing_is_sent(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    run_id = _waiting_run(reference_app)
    with Server() as server:
        entry = entry_for("file_ticket", "write", config(server.url), None)
        seed_openapi_row(reference_app.session_factory, entry)
        world = governed(
            reference_app.session_factory,
            agent_manifest({"file_ticket": registered("file_ticket")}),
            [entry],
            workflow_run_id=run_id,
            tool_backends={("file_ticket", "1.0"): "openapi_http"},
        )
        world.plan.egress_policy = LOOPBACK
        result = execute(
            world.plan, "go", executor=_scripted(_openapi_action(server.url, args={"body": {}}))
        )
        proposal = _assert_parked_with_one_proposal(reference_app, result)
        assert server.requests == [], "no request left the platform"
    assert proposal.connector == "openapi-http"


def test_a_model_chosen_mcp_write_is_a_proposal_and_the_server_is_never_reached(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.mcp_policy import _reset_rate_limits_for_tests

    _reset_rate_limits_for_tests()
    run_id = _waiting_run(reference_app)
    fake = FakeMcp().install(monkeypatch)
    use_database(monkeypatch, reference_app.session_factory)
    seed_server(reference_app.session_factory)
    world = governed(
        reference_app.session_factory,
        agent_manifest({"write_issue": mcp_binding("issue_write")}),
        [],
        workflow_run_id=run_id,
        mcp_tools=frozenset({(SERVER_ID, "issue_write")}),
    )

    result = execute(world.plan, "go", executor=_scripted(_mcp_action()))

    proposal = _assert_parked_with_one_proposal(reference_app, result)
    assert proposal.connector == "mcp-tool"
    assert fake.calls == [], "downstream.mutations() == 0"


def test_a_restarted_run_finds_its_model_chosen_proposal_and_does_not_ask_again(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    run_id = _waiting_run(reference_app)
    world = _registered_world(reference_app, run_id)
    tool = Tool()
    first = execute(
        world.plan,
        "go",
        executor=_scripted(_registered_action()),
        extra_tools={"open_ticket": tool},
    )
    proposal = _assert_parked_with_one_proposal(reference_app, first)

    restarted = _scripted(_registered_action(args={"title": "another"}))
    again = execute(world.plan, "go", executor=restarted, extra_tools={"open_ticket": tool})

    assert again.status == "blocked" and again.error == "waiting_approval:agent"
    assert restarted.step_requests == [], "the occurrence already has its logical effect"
    assert [p.proposal_id for p in _rows(reference_app)[0]] == [proposal.proposal_id]
    assert tool.calls == []


def test_two_workers_proposing_one_model_chosen_call_record_one_effect_and_ask_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    if reference_app.dialect != "postgresql":
        pytest.skip("row-lock ordering is proven on PostgreSQL; SQLite serializes every writer")
    run_id = _waiting_run(reference_app)
    world = _registered_world(reference_app, run_id)
    agent = world.agent()
    context = world.plan.governed_agent_steps
    assert context is not None
    binder = rt._RuntimeToolBinder(world.plan, NODE)
    trust = {b.local_name: context.classify_tool(b) for b in agent.tools}
    request = AgentStepRequest(
        step_request_id="step-b04-6",
        workflow_run_id=run_id,
        node_id="agent",
        occurrence_id="occ-b04-6",
        attempt=1,
        run_manifest_digest="a" * 64,
        context_manifest_digest="b" * 64,
        grant_id="grant-1",
        limits=LIMITS,
    )
    result = answer(
        request, "proposed_action", usage=USAGE, proposed_actions=(_registered_action(),)
    )
    barrier = threading.Barrier(2)
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(30)
            outcomes.append(
                record_step_proposal(
                    context, request, result, trust, binder=binder, bindings=agent.tools
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors and not any(t.is_alive() for t in threads), errors

    assert sorted(item.created for item in outcomes) == [False, True]
    assert outcomes[0].proposal_id == outcomes[1].proposal_id
    proposals, requests = _rows(reference_app)
    assert len(proposals) == 1
    assert [r.status for r in requests] == ["pending"], (
        "one question is open, the second worker found it"
    )
