"""B08.2 (#744): a governed agent step's protected action becomes a recorded, approvable proposal, never an effect.

The platform side of a step that stops with ``proposed_action``: the tools the adapter may run are classified by their registry
identity (not the model's word), a proposal is bound to the platform's class and connector version (a claim that differs is
refused), recorded once per ``(run, node, occurrence)`` with the approval request bound to that exact intent, and the node parks
behind it. SQLite throughout; the PostgreSQL races live in ``tests/egi_scenarios/test_b08_protected_action_proposal.py``.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts.agent_step import ProposedAction, StepLimits
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects import step_proposal
from caliber.effects.dispatcher import EffectRefused, UnsupportedEffectPath
from caliber.effects.records import transition
from caliber.effects.step_proposal import (
    GovernedStepContext,
    ToolTrust,
    ensure_approval_request,
    prior_proposal,
    protected_tool_of_record,
    reask_approval,
    record_step_proposal,
    registry_tool_classifier,
    step_evidence_hash,
    stored_context_manifest_digest,
    untrusted_tool,
)
from caliber.ids import new_tool_id
from caliber.workflows import runtime as runtime_module
from caliber.workflows.agent_step import (
    AgentStepInput,
    AgentStepRequest,
    AgentStepResult,
    ScriptedStep,
    StepUsage,
    answer,
)
from caliber.workflows.approval_policy import ApprovalPolicy
from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE, register_builtin_tools
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import IRAgent, IRToolBinding
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database
from tests.workflow_helpers import make_manifest

RUN = "RUN-ep"
SECRET = "secret-args-marker-91d2"
CONNECTOR = "document-request"
RESOURCE = "cases/MORT-2026-000184/document-requests"
PROTECTED = "request_missing_document"
READ = "list_workdir_files"
LIMITS = StepLimits(
    max_turns=8, max_tool_calls=4, max_tokens=20000, deadline="2099-01-01T00:00:00Z"
)
USAGE = StepUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, turns=1, tool_calls=0)


def _action(**changes: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "connector": CONNECTOR,
        "connector_version": "1",
        "effect_class": "irreversible_write",
        "resource": RESOURCE,
        "args": {"document_type": "pay_statement"},
    }
    fields.update(changes)
    return ProposedAction(**fields)


def _proposing(action: ProposedAction | None = None, **changes: Any) -> ScriptedStep:
    return ScriptedStep("proposed_action", (action or _action(),), usage=USAGE, **changes)


def _seed_registry(factory: Callable[[], Session]) -> None:
    with factory() as session:
        register_builtin_tools(session)
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name=PROTECTED,
                version="1.0",
                module_path="downstream_tools",
                callable_name=PROTECTED,
                side_effect_level="external_action",
                requires_approval=True,
                owner="@ops",
            )
        )
        session.commit()


def _manifest() -> dict[str, Any]:
    manifest = make_manifest("governed_wf")
    manifest["nodes"]["agent"]["tools"] = [READ, PROTECTED]
    manifest["tools"] = {
        READ: {"registry_ref": f"tool.{READ}.v1", "version_constraint": ">=1.0,<2.0"},
        PROTECTED: {
            "registry_ref": f"tool.{PROTECTED}.v1",
            "version_constraint": ">=1.0,<2.0",
            "requires_approval": True,
        },
    }
    return manifest


def _resolver() -> InMemoryToolResolver:
    return InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name=READ,
                version="1.0",
                module_path=BUILTIN_TOOL_MODULE,
                callable_name=READ,
                side_effect_level="read",
                allow_in_preview=True,
            ),
            ToolRegistryEntry(
                name=PROTECTED,
                version="1.0",
                module_path="downstream_tools",
                callable_name=PROTECTED,
                side_effect_level="external_action",
                requires_approval=True,
            ),
        ]
    )


class Canaries:
    """The two callables a step's tools resolve to: the protected one is a downstream mutation counter."""

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []

    def tools(self) -> dict[str, Callable[..., Any]]:
        def read(**arguments: Any) -> str:
            self.reads.append(arguments)
            return "data"

        def mutate(**arguments: Any) -> str:
            self.mutations.append(arguments)
            return "mutated"

        return {READ: read, PROTECTED: mutate}


class Recorder:
    """A context recorder: deterministic step ids, and the assemblies it was asked to record."""

    def __init__(self) -> None:
        self.assemblies: list[Any] = []

    def __call__(self, assembly: Any) -> str:
        self.assemblies.append(assembly)
        return f"step-{assembly.node_id}-{assembly.occurrence_id}-{assembly.attempt}"


def _context(factory: Callable[[], Session], **changes: Any) -> GovernedStepContext:
    fields: dict[str, Any] = {
        "session_factory": factory,
        "workflow_run_id": RUN,
        "run_manifest_digest": "a" * 64,
        "grant_id": "grant-1",
        "classify_tool": registry_tool_classifier(factory),
        "context_manifest_digest": lambda step_request_id: "b" * 64,
        "limits": lambda agent: LIMITS,
        "requested_by": "editor-1",
        "approval_ttl_seconds": 3600,
    }
    fields.update(changes)
    return GovernedStepContext(**fields)


def _plan(
    factory: Callable[[], Session],
    *,
    governed: bool = True,
    recorder: Recorder | None = None,
    manifest: dict[str, Any] | None = None,
    **context_changes: Any,
) -> RuntimePlan:
    resolver = _resolver()
    return RuntimePlan(
        ir=build_ir(parse_manifest(manifest or _manifest()), resolver, version="1"),
        resolver=resolver,
        workflow_version_id="governed-version",
        context_recorder=recorder if recorder is not None else Recorder(),
        governed_agent_steps=_context(factory, **context_changes) if governed else None,
    )


def _fail(factory: Callable[[], Session], proposal_id: str) -> None:
    with factory() as session:
        proposal = session.get(CaliberEffectProposal, proposal_id)
        assert proposal is not None
        transition(
            session, proposal_id, "failed", expected_version=proposal.version, reason="rejected"
        )
        session.commit()


def _rows(factory: Callable[[], Session]) -> tuple[list[Any], list[Any]]:
    with factory() as session:
        proposals = list(session.execute(select(CaliberEffectProposal)).scalars())
        requests = list(session.execute(select(CaliberRuntimeApprovalRequest)).scalars())
    return proposals, requests


@pytest.fixture(scope="module")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One migrated SQLite database (a run and the tool registry seeded) copied for every test: migrating is the slow part."""
    folder = tmp_path_factory.mktemp("governed-step-template")
    patch = pytest.MonkeyPatch()
    try:
        with _database("sqlite", folder, patch) as (_, factory):
            _seed_run(factory)
            _seed_registry(factory)
    finally:
        patch.undo()
    return folder / "event_outbox.db"


@pytest.fixture
def world(_migrated_template: Path, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database = tmp_path / "governed.db"
    shutil.copyfile(_migrated_template, database)
    engine = create_engine(f"sqlite:///{database}")
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------------------ the classifier


def test_a_seeded_read_builtin_is_read_and_a_custom_tool_is_the_fail_closed_class(
    world: Any,
) -> None:
    classify = registry_tool_classifier(world)
    ir = build_ir(parse_manifest(_manifest()), _resolver(), version="1")
    agent = ir.nodes["agent"]
    assert isinstance(agent, IRAgent)
    bindings = {binding.local_name: binding for binding in agent.tools}
    read = classify(bindings[READ])
    assert (read.effect_class, read.caliber_defined, read.name) == ("read", True, READ)
    protected = classify(bindings[PROTECTED])
    assert (protected.effect_class, protected.caliber_defined) == ("irreversible_write", False)
    assert protected.disposition == "dispatch"


def test_a_seeded_write_builtin_keeps_the_inventory_class(world: Any) -> None:
    binding = IRToolBinding(
        local_name="write_workdir_file",
        registry_ref="tool.write_workdir_file.v1",
        version_constraint=">=1.0",
        requires_approval=True,
        side_effect_level="write",
        allow_in_preview=False,
        module_path=BUILTIN_TOOL_MODULE,
        callable_name="write_workdir_file",
        resolved_name="write_workdir_file",
        resolved_version="1.0",
    )
    trust = registry_tool_classifier(world)(binding)
    assert (trust.effect_class, trust.caliber_defined) == ("reversible_write", True)


def _binding(**changes: Any) -> IRToolBinding:
    fields: dict[str, Any] = {
        "local_name": "list_workdir_files",
        "registry_ref": "tool.list_workdir_files.v1",
        "version_constraint": ">=1.0",
        "requires_approval": False,
        "side_effect_level": "read",
        "allow_in_preview": True,
        "module_path": BUILTIN_TOOL_MODULE,
        "callable_name": "list_workdir_files",
        "resolved_name": "list_workdir_files",
        "resolved_version": "1.0",
    }
    fields.update(changes)
    return IRToolBinding(**fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"module_path": "evil.module"},
        {"callable_name": "something_else"},
        {"resolved_name": "unknown_tool"},
        {"resolved_version": "9.9"},
        {"resolved_name": ""},
        {"resolved_version": ""},
        {"binding_type": "mcp_tool"},
    ],
    ids=[
        "module_differs",
        "callable_differs",
        "no_such_row",
        "no_such_version",
        "unresolved_name",
        "unresolved_version",
        "mcp_tool",
    ],
)
def test_a_tool_whose_registry_identity_is_not_proven_is_never_read(
    world: Any, changes: dict[str, Any]
) -> None:
    assert registry_tool_classifier(world)(_binding()).effect_class == "read"  # the control
    trust = registry_tool_classifier(world)(_binding(**changes))
    assert (trust.effect_class, trust.caliber_defined) == ("irreversible_write", False)


def test_a_custom_tool_that_squats_a_builtin_name_is_not_read(world: Any) -> None:
    with world() as session:
        row = session.execute(
            select(CaliberToolRegistry).where(CaliberToolRegistry.name == "list_workdir_files")
        ).scalar_one()
        row.owner = (
            "@mallory"  # the owner is free text; the seed's own owner is part of the identity
        )
        session.commit()
    trust = registry_tool_classifier(world)(_binding())
    assert (trust.effect_class, trust.caliber_defined) == ("irreversible_write", False)


def test_an_unreadable_registry_is_the_fail_closed_class() -> None:
    def broken() -> Any:
        raise __import__("sqlalchemy").exc.OperationalError("select 1", {}, Exception("down"))

    assert registry_tool_classifier(broken)(_binding()) == untrusted_tool("list_workdir_files")


def test_an_unloadable_inventory_is_the_fail_closed_class(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: Any, **_k: Any) -> str:
        raise step_proposal.InventoryError("no table")

    monkeypatch.setattr(step_proposal, "effect_class", refuse)
    assert registry_tool_classifier(world)(_binding()) == untrusted_tool("list_workdir_files")


# ------------------------------------------------------------------------------------ tool of record


def test_the_tool_of_record_is_the_strictest_non_read_tool() -> None:
    tools = {
        "a": ToolTrust("a", "read", True, "dispatch"),
        "b": ToolTrust("b", "reversible_write", True, "dispatch"),
        "c": ToolTrust("c", "export", True, "dispatch"),
    }
    assert protected_tool_of_record(tools).name == "c"
    tools["d"] = ToolTrust("d", "export", True, "refuse")
    assert protected_tool_of_record(tools).name == "d", "a refused tool outranks a dispatchable one"
    tools["e"] = ToolTrust("e", "irreversible_write", False, "dispatch")
    assert protected_tool_of_record(tools).name == "e"


def test_an_agent_without_a_protected_tool_cannot_propose() -> None:
    with pytest.raises(EffectRefused) as refused:
        protected_tool_of_record({"a": ToolTrust("a", "read", True, "dispatch")})
    assert refused.value.code == "no_protected_tool"
    with pytest.raises(EffectRefused):
        protected_tool_of_record({})


# ---------------------------------------------------------------------------- the proposal service


def _request(**changes: Any) -> AgentStepRequest:
    fields: dict[str, Any] = {
        "step_request_id": "step-1",
        "workflow_run_id": RUN,
        "node_id": "agent",
        "occurrence_id": "occ-1",
        "attempt": 1,
        "run_manifest_digest": "a" * 64,
        "context_manifest_digest": "b" * 64,
        "grant_id": "grant-1",
        "limits": LIMITS,
    }
    fields.update(changes)
    return AgentStepRequest(**fields)


def _result(request: AgentStepRequest, action: ProposedAction | None = None, **changes: Any) -> Any:
    fields: dict[str, Any] = {
        "stop_reason": "proposed_action",
        "usage": USAGE,
        "proposed_actions": (action or _action(),),
    }
    fields.update(changes)
    stop = fields.pop("stop_reason")
    return answer(request, stop, **fields)


def _tools(**classes: str) -> dict[str, ToolTrust]:
    tools = {PROTECTED: untrusted_tool(PROTECTED)}
    tools.update({name: ToolTrust(name, cls, True, "dispatch") for name, cls in classes.items()})
    return tools


def test_a_proposal_is_bound_recorded_once_and_asks_for_approval(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())

    assert recorded.created is True and recorded.state == "proposed"
    proposals, requests = _rows(world)
    (proposal,) = proposals
    assert (proposal.workflow_run_id, proposal.node_id, proposal.occurrence_id) == (
        RUN,
        "agent",
        "occ-1",
    )
    assert (proposal.connector, proposal.connector_version) == (CONNECTOR, "1")
    assert proposal.effect_class == "irreversible_write"
    assert proposal.args == {"document_type": "pay_statement"}
    assert proposal.state == "proposed"
    assert proposal.intent_hash == _action().bound_to("irreversible_write").intent_hash
    (approval,) = requests
    assert approval.runtime_approval_id == recorded.approval_request_id
    assert (approval.status, approval.proposal_id) == ("pending", proposal.proposal_id)
    assert approval.intent_hash == proposal.intent_hash
    assert approval.evidence_hash == step_evidence_hash(request, proposal.intent_hash)
    assert approval.requested_by == "editor-1"
    assert approval.node_id == "agent" and approval.workflow_run_id == RUN
    assert approval.policy_snapshot == ApprovalPolicy().to_snapshot()
    assert approval.expires_at is not None and approval.superseded_by is None


def test_the_same_step_again_finds_the_same_proposal_and_request(world: Any) -> None:
    context = _context(world)
    request = _request()
    first = record_step_proposal(context, request, _result(request), _tools())
    again = record_step_proposal(context, request, _result(request), _tools())
    assert again.created is False
    assert (again.proposal_id, again.approval_request_id) == (
        first.proposal_id,
        first.approval_request_id,
    )
    proposals, requests = _rows(world)
    assert len(proposals) == 1 and len(requests) == 1
    assert requests[0].superseded_by is None and requests[0].status == "pending"


def test_a_new_occurrence_is_a_new_logical_effect(world: Any) -> None:
    context = _context(world)
    one, two = _request(), _request(occurrence_id="occ-2", step_request_id="step-2")
    first = record_step_proposal(context, one, _result(one), _tools())
    second = record_step_proposal(context, two, _result(two), _tools())
    assert second.created is True and second.proposal_id != first.proposal_id
    proposals, requests = _rows(world)
    assert len(proposals) == 2
    # one live request per node: the newer replaces the older (B06.2)
    assert sorted(r.status for r in requests) == ["pending", "superseded"]


def test_the_models_claims_never_decide_the_class(world: Any) -> None:
    context = _context(world)
    request = _request()
    for claimed in ("read", "reversible_write", "export"):
        lying = _action(effect_class=claimed)
        with pytest.raises(EffectRefused) as refused:
            record_step_proposal(context, request, _result(request, lying), _tools())
        assert refused.value.code == "claim_mismatch"
        assert str(refused.value) == "claim_mismatch", "the refusal carries a code, not the claim"
    assert _rows(world) == ([], [])


def test_the_connector_version_is_the_dispatchers_not_the_models(world: Any) -> None:
    context = _context(world)
    request = _request()
    wrong = _action(connector_version="9.9.9-" + SECRET)
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(request, wrong), _tools())
    assert refused.value.code == "claim_mismatch"
    assert SECRET not in str(refused.value) and SECRET not in repr(refused.value)
    assert _rows(world) == ([], [])


def test_a_connector_the_dispatcher_cannot_perform_is_refused_before_anyone_is_asked(
    world: Any,
) -> None:
    context = _context(world)
    request = _request()
    stranger = _action(connector="payroll-" + SECRET)
    with pytest.raises(UnsupportedEffectPath) as refused:
        record_step_proposal(context, request, _result(request, stranger), _tools())
    assert refused.value.code == "unsupported_effect_path"
    assert SECRET not in str(refused.value)
    assert _rows(world) == ([], [])


def test_a_refused_tool_path_is_never_proposed(world: Any) -> None:
    context = _context(world)
    request = _request()
    tools = {PROTECTED: ToolTrust(PROTECTED, "irreversible_write", False, "refuse")}
    with pytest.raises(UnsupportedEffectPath):
        record_step_proposal(context, request, _result(request), tools)
    assert _rows(world) == ([], [])


def test_the_class_is_the_strictest_of_the_agents_tools_whatever_the_model_says(
    world: Any,
) -> None:
    """The v1 proposal names no tool, so a lesser claim cannot ride on a lesser tool of an agent that holds a stricter one."""
    context = _context(world)
    request = _request()
    tools = _tools(write_workdir_file="reversible_write")  # plus the fail-closed custom tool
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(
            context, request, _result(request, _action(effect_class="reversible_write")), tools
        )
    assert refused.value.code == "claim_mismatch"
    only_lesser = {
        "write_workdir_file": ToolTrust("write_workdir_file", "reversible_write", True, "dispatch")
    }
    recorded = record_step_proposal(
        context, request, _result(request, _action(effect_class="reversible_write")), only_lesser
    )
    proposals, _ = _rows(world)
    assert proposals[0].effect_class == "reversible_write"
    assert recorded.intent_hash == proposals[0].intent_hash


def test_a_run_that_does_not_exist_records_nothing(world: Any) -> None:
    request = _request(workflow_run_id="RUN-nope")
    context = _context(world, workflow_run_id="RUN-nope")
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(request), _tools())
    assert refused.value.code == "run_not_found"
    assert _rows(world) == ([], [])


@pytest.mark.parametrize("stop", ["completed", "max_turns", "error", "refused"])
def test_only_a_proposed_action_stop_is_recorded(world: Any, stop: str) -> None:
    context = _context(world)
    request = _request()
    result = _result(
        request,
        stop_reason=stop,
        proposed_actions=(),
        usage_source="unknown",
        error_code="provider_error" if stop in ("error", "refused") else None,
    )
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, result, _tools())
    assert refused.value.code == "no_proposed_action"
    assert _rows(world) == ([], [])


def test_a_result_for_another_step_or_run_is_refused(world: Any) -> None:
    context = _context(world)
    request = _request()
    other = _request(step_request_id="step-other")
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(other), _tools())
    assert refused.value.code == "step_mismatch"
    elsewhere = _request(workflow_run_id="RUN-other")
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, elsewhere, _result(elsewhere), _tools())
    assert refused.value.code == "step_mismatch"
    retried = _request(attempt=2)
    with pytest.raises(EffectRefused):
        record_step_proposal(context, request, _result(retried), _tools())
    assert _rows(world) == ([], [])


def test_an_agent_with_no_protected_tool_records_nothing(world: Any) -> None:
    request = _request()
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(
            _context(world),
            request,
            _result(request),
            {"a": ToolTrust("a", "read", True, "dispatch")},
        )
    assert refused.value.code == "no_protected_tool"
    assert _rows(world) == ([], [])


def test_a_classifier_that_disagrees_with_the_inventory_cannot_record_a_different_effect(
    world: Any,
) -> None:
    """The stored intent must be the bound one: a trust the inventory does not confirm is an ``intent_mismatch``, rolled back."""
    request = _request()
    forged = {PROTECTED: ToolTrust(PROTECTED, "reversible_write", False, "dispatch")}
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(
            _context(world),
            request,
            _result(request, _action(effect_class="reversible_write")),
            forged,
        )
    assert refused.value.code == "intent_mismatch"
    assert _rows(world) == ([], [])


def test_the_same_occurrence_with_another_intent_is_a_conflict_and_changes_nothing(
    world: Any,
) -> None:
    context = _context(world)
    request = _request()
    record_step_proposal(context, request, _result(request), _tools())
    other = _action(args={"document_type": "w2"})
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(request, other), _tools())
    assert refused.value.code == "intent_conflict"
    proposals, requests = _rows(world)
    assert len(proposals) == 1 and len(requests) == 1
    assert proposals[0].args == {"document_type": "pay_statement"}


def test_a_refusal_never_carries_the_actions_arguments(world: Any, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    context = _context(world)
    request = _request()
    bad = _action(args={"document_type": SECRET}, effect_class="read")
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(request, bad), _tools())
    assert SECRET not in str(refused.value) and SECRET not in repr(refused.value)
    assert SECRET not in caplog.text


def test_a_proposal_that_has_moved_on_is_not_asked_about_again(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    _fail(world, recorded.proposal_id)
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(context, request, _result(request), _tools())
    assert refused.value.code == "proposal_not_proposed"


# ------------------------------------------------------------------------- the approval request


def test_a_live_request_is_reused_never_duplicated(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    with world() as session:
        first = ensure_approval_request(session, context, recorded.proposal_id, evidence_hash=None)
        second = ensure_approval_request(session, context, recorded.proposal_id, evidence_hash=None)
        session.commit()
    assert first.runtime_approval_id == second.runtime_approval_id == recorded.approval_request_id
    assert len(_rows(world)[1]) == 1


def test_an_approved_unconsumed_request_is_still_the_live_one(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    with world() as session:
        row = session.get(CaliberRuntimeApprovalRequest, recorded.approval_request_id)
        assert row is not None
        row.status, row.decided_by = "approved", "approver"
        session.commit()
    again = reask_approval(context, step_proposal.prior_proposal(world, RUN, "agent", "occ-1"))  # type: ignore[arg-type]
    assert again.approval_request_id == recorded.approval_request_id
    assert len(_rows(world)[1]) == 1


@pytest.mark.parametrize("lapse", ["expired", "consumed", "superseded"])
def test_a_lapsed_request_is_re_asked_with_the_original_evidence(
    world: Any, lapse: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(world)
    request = _request()
    real_create = step_proposal.create_approval_request
    if lapse == "expired":
        # The binding (``expires_at`` included) is frozen once stored, so the request is made in the past.
        long_ago = datetime.now(timezone.utc) - timedelta(days=2)
        monkeypatch.setattr(
            step_proposal,
            "create_approval_request",
            lambda *a, **k: real_create(*a, now=long_ago, **k),
        )
    recorded = record_step_proposal(context, request, _result(request), _tools())
    monkeypatch.setattr(step_proposal, "create_approval_request", real_create)
    with world() as session:
        row = session.get(CaliberRuntimeApprovalRequest, recorded.approval_request_id)
        assert row is not None
        if lapse == "expired":
            pass
        elif lapse == "consumed":
            row.status, row.consumed_at = (
                "approved",
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
        else:
            row.status, row.superseded_by = "superseded", "rap-other"
        session.commit()
    prior = prior_proposal(world, RUN, "agent", "occ-1")
    assert prior is not None
    fresh = reask_approval(context, prior)
    assert fresh.approval_request_id != recorded.approval_request_id
    _, requests = _rows(world)
    replacement = next(r for r in requests if r.runtime_approval_id == fresh.approval_request_id)
    old = next(r for r in requests if r.runtime_approval_id == recorded.approval_request_id)
    assert replacement.status == "pending" and replacement.superseded_by is None
    assert (
        replacement.evidence_hash
        == old.evidence_hash
        == step_evidence_hash(request, prior.intent_hash)
    )


def test_a_proposal_with_no_request_and_no_evidence_is_not_asked_about(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    with world() as session:
        session.execute(CaliberRuntimeApprovalRequest.__table__.delete())  # type: ignore[attr-defined]
        session.commit()
    prior = prior_proposal(world, RUN, "agent", "occ-1")
    assert prior is not None and prior.proposal_id == recorded.proposal_id
    with pytest.raises(EffectRefused) as refused:
        reask_approval(context, prior)
    assert refused.value.code == "approval_evidence_unavailable"


def test_a_proposal_that_does_not_exist_is_not_asked_about(world: Any) -> None:
    with world() as session, pytest.raises(EffectRefused) as refused:
        ensure_approval_request(session, _context(world), "prop-nope", evidence_hash=None)
    assert refused.value.code == "proposal_not_found"


def test_a_rejection_is_final_for_the_proposal(world: Any) -> None:
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    with world() as session:
        row = session.get(CaliberRuntimeApprovalRequest, recorded.approval_request_id)
        assert row is not None
        row.status, row.decided_by = "rejected", "approver"
        session.commit()
    prior = prior_proposal(world, RUN, "agent", "occ-1")
    assert prior is not None
    with pytest.raises(EffectRefused) as refused:
        reask_approval(context, prior)
    assert refused.value.code == "approval_rejected"
    assert len(_rows(world)[1]) == 1, "nobody is asked again"


def test_an_error_code_that_is_not_a_fixed_token_never_reaches_the_run_error() -> None:
    request = _request()
    for code, shown in (
        ("model_refused", " (model_refused)"),
        ("Not A Token " + SECRET, ""),
        (None, ""),
    ):
        unvalidated = AgentStepResult(
            step_request_id=request.step_request_id,
            workflow_run_id=request.workflow_run_id,
            node_id=request.node_id,
            occurrence_id=request.occurrence_id,
            attempt=request.attempt,
            stop_reason="error",
            usage=USAGE,
            error_code=code,
        )
        error = runtime_module._governed_stop_error(unvalidated)
        assert str(error) == f"governed agent step stopped: error{shown}"
        assert SECRET not in str(error)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"requested_by": " padded "}, "bad_field"),
        ({"approval_ttl_seconds": 0}, "bad_field"),
        ({"approval_policy": ApprovalPolicy(required_role="not-a-role")}, "bad_field"),
    ],
)
def test_a_request_the_runtime_cannot_honour_records_nothing(
    world: Any, changes: dict[str, Any], code: str
) -> None:
    request = _request()
    with pytest.raises(EffectRefused) as refused:
        record_step_proposal(_context(world, **changes), request, _result(request), _tools())
    assert refused.value.code == code
    assert _rows(world) == ([], []), "the effect and its request are one transaction"


def test_prior_proposal_reads_the_logical_key_only(world: Any) -> None:
    assert prior_proposal(world, RUN, "agent", "occ-1") is None
    context = _context(world)
    request = _request()
    recorded = record_step_proposal(context, request, _result(request), _tools())
    prior = prior_proposal(world, RUN, "agent", "occ-1")
    assert prior is not None
    assert (prior.proposal_id, prior.state, prior.intent_hash) == (
        recorded.proposal_id,
        "proposed",
        recorded.intent_hash,
    )
    assert prior_proposal(world, RUN, "agent", "occ-2") is None
    assert prior_proposal(world, RUN, "other", "occ-1") is None
    assert prior_proposal(world, "RUN-other", "agent", "occ-1") is None


def test_the_evidence_binds_the_step_the_context_and_the_intent() -> None:
    request = _request()
    base = step_evidence_hash(request, "sha256:" + "1" * 64)
    assert base == step_evidence_hash(request, "sha256:" + "1" * 64)
    assert base.startswith("sha256:") and len(base) == 71
    assert base != step_evidence_hash(request, "sha256:" + "2" * 64)
    assert base != step_evidence_hash(
        replace(request, context_manifest_digest="c" * 64), "sha256:" + "1" * 64
    )
    assert base != step_evidence_hash(
        replace(request, step_request_id="step-2"), "sha256:" + "1" * 64
    )
    assert base != step_evidence_hash(
        replace(request, run_manifest_digest="d" * 64), "sha256:" + "1" * 64
    )


def test_a_stored_context_digest_is_the_recorded_manifests_and_a_missing_one_refuses(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import caliber.context.manifest as manifests

    class Built:
        manifest_digest = "f" * 64

    seen: list[tuple[str, str]] = []

    def load(session: Any, run_id: str, step_request_id: str) -> Any:
        seen.append((run_id, step_request_id))
        return Built() if step_request_id == "known" else None

    monkeypatch.setattr(manifests, "load_context_manifest", load)
    digest = stored_context_manifest_digest(world, RUN)
    assert digest("known") == "f" * 64
    assert seen == [(RUN, "known")]
    with pytest.raises(EffectRefused) as refused:
        digest("missing")
    assert refused.value.code == "context_manifest_missing"


# ------------------------------------------------------------------------ the real runtime, scripted


def _run(
    factory: Callable[[], Session],
    script: list[Any],
    *,
    canaries: Canaries | None = None,
    plan: RuntimePlan | None = None,
) -> tuple[Any, FakeWorkflowExecutor, Canaries]:
    canaries = canaries or Canaries()
    executor = FakeWorkflowExecutor(step_script=script)
    result = execute(
        plan or _plan(factory),
        "please ask for the pay statement",
        executor=executor,
        extra_tools=canaries.tools(),
    )
    return result, executor, canaries


def test_a_scripted_protected_call_is_a_proposal_not_an_effect(world: Any) -> None:
    """The oracle of #744: one proposal row, the mutation counter at zero, the run parked, a read tool still ran in-loop."""
    result, executor, canaries = _run(
        world,
        [replace(_proposing(), read_tool_calls=((READ, {"path": "case.txt"}),))],
    )

    assert canaries.mutations == [], "the model never executed a protected tool"
    assert canaries.reads == [{"path": "case.txt"}], "a read tool still returns data in-loop"
    proposals, requests = _rows(world)
    assert len(proposals) == 1 and len(requests) == 1
    assert (proposals[0].state, proposals[0].effect_class) == ("proposed", "irreversible_write")
    assert (proposals[0].node_id, proposals[0].occurrence_id) == ("agent", "agent")

    assert result.status == "blocked" and result.error == "waiting_approval:agent"
    (step,) = [s for s in result.steps if s.node_id == "agent"]
    assert step.status == "blocked" and step.node_type == "agent"
    assert step.tokens == USAGE.total_tokens == result.tokens
    state = step.checkpoint_state
    assert state is not None
    assert state["kind"] == "runtime_approval" and state["node_id"] == "agent"
    assert state["proposal_id"] == proposals[0].proposal_id
    assert state["approval_request_id"] == requests[0].runtime_approval_id
    assert state["intent_hash"] == proposals[0].intent_hash
    assert state["resume_policy"] == "same_run_same_occurrence"
    assert "pay_statement" not in repr(state), (
        "the checkpoint names the proposal, not its arguments"
    )
    # the request is what the adapter was asked, with the platform's own digests
    (asked,) = executor.step_requests
    assert (asked.workflow_run_id, asked.node_id, asked.occurrence_id, asked.attempt) == (
        RUN,
        "agent",
        "agent",
        1,
    )
    assert (asked.run_manifest_digest, asked.context_manifest_digest) == ("a" * 64, "b" * 64)
    assert (asked.grant_id, asked.limits) == ("grant-1", LIMITS)
    assert requests[0].evidence_hash == step_evidence_hash(asked, proposals[0].intent_hash)


def test_the_adapter_is_handed_only_the_tools_the_platform_calls_read(world: Any) -> None:
    seen: list[AgentStepInput] = []

    class Spy(FakeWorkflowExecutor):
        def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
            seen.append(step_input)
            return super().run_step(request, step_input)

    canaries = Canaries()
    result = execute(
        _plan(world),
        "go",
        executor=Spy(step_script=[_proposing()]),
        extra_tools=canaries.tools(),
    )
    assert result.status == "blocked"
    (step_input,) = seen
    assert set(step_input.read_tools) == {READ}
    assert PROTECTED not in step_input.read_tools
    assert canaries.mutations == []


def test_a_handoff_target_is_handed_only_its_own_read_tools(world: Any) -> None:
    manifest = _manifest()
    manifest["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    manifest["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "tools": [READ, PROTECTED],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    seen: list[AgentStepInput] = []

    class Spy(FakeWorkflowExecutor):
        def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
            seen.append(step_input)
            return super().run_step(request, step_input)

    canaries = Canaries()
    result = execute(
        _plan(world, manifest=manifest),
        "go",
        executor=Spy(step_script=[ScriptedStep("completed", usage=USAGE)]),
        extra_tools=canaries.tools(),
    )
    assert result.status == "completed"
    (step_input,) = seen
    assert step_input.handoff_agents is not None
    assert "billing" in step_input.handoff_agents
    for target in step_input.handoff_agents.values():
        assert set(target.read_tools) == {READ}, (
            "a target is given its read tools, never a protected one"
        )
    assert canaries.mutations == []


def test_an_agent_without_declared_outputs_publishes_its_final_output(world: Any) -> None:
    manifest = _manifest()
    del manifest["nodes"]["final"]
    manifest["edges"] = [manifest["edges"][0]]
    manifest["nodes"]["agent"]["outputs"] = {}
    result, _, _ = _run(
        world, [ScriptedStep("completed", usage=USAGE)], plan=_plan(world, manifest=manifest)
    )
    assert result.status == "completed"


def test_a_tool_the_platform_cannot_place_is_not_handed_to_the_adapter_as_read(
    world: Any,
) -> None:
    """A classifier that raises, or answers with a lookalike, is the fail-closed class: nothing is handed over."""
    seen: list[AgentStepInput] = []

    class Spy(FakeWorkflowExecutor):
        def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
            seen.append(step_input)
            return super().run_step(request, step_input)

    def explode(_binding: IRToolBinding) -> ToolTrust:
        raise RuntimeError("registry down")

    class Lookalike(ToolTrust):
        pass

    for classifier in (explode, lambda b: Lookalike(b.local_name, "read", True, "dispatch")):
        seen.clear()
        plan = _plan(world, classify_tool=classifier)
        execute(
            plan,
            "go",
            executor=Spy(step_script=[ScriptedStep("completed", usage=USAGE)]),
            extra_tools=Canaries().tools(),
        )
        assert seen and dict(seen[0].read_tools) == {}


def test_a_lying_class_is_refused_and_nothing_is_recorded(world: Any) -> None:
    result, _, canaries = _run(world, [_proposing(_action(effect_class="read"))])
    assert result.status == "error"
    assert (
        result.error == "ToolExecutionError: governed agent step proposal refused: claim_mismatch"
    )
    assert canaries.mutations == []
    assert _rows(world) == ([], [])


def test_a_refusal_in_the_run_error_is_payload_free(world: Any) -> None:
    secret_action = _action(connector="payroll", args={"document_type": SECRET})
    result, _, _ = _run(world, [_proposing(secret_action)])
    assert result.status == "error"
    assert SECRET not in (result.error or "")
    assert "unsupported_effect_path" in (result.error or "")


def test_a_step_that_completes_publishes_an_empty_output_and_its_usage(world: Any) -> None:
    result, executor, canaries = _run(
        world,
        [
            ScriptedStep(
                "completed",
                usage=StepUsage(7, 3, 10, 1, 1),
                read_tool_calls=((READ, {"path": "a"}),),
            )
        ],
    )
    assert result.status == "completed" and result.output == ""
    (step,) = [s for s in result.steps if s.node_id == "agent"]
    assert (step.tokens, step.prompt_tokens, step.completion_tokens) == (10, 7, 3)
    assert step.status == "ok" and step.output == ""
    assert canaries.reads == [{"path": "a"}] and canaries.mutations == []
    assert _rows(world) == ([], [])
    assert len(executor.step_requests) == 1
    assert executor.calls == [], "the legacy run_agent path is not used by a governed step"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (ScriptedStep("max_turns", usage=USAGE), "stopped: max_turns"),
        (ScriptedStep("max_tokens", usage=USAGE), "stopped: max_tokens"),
        (
            ScriptedStep("refused", usage_source="unknown", error_code="model_refused"),
            "stopped: refused (model_refused)",
        ),
        (TimeoutError("boom-" + SECRET), "stopped: error (adapter_timeout)"),
        (RuntimeError("boom-" + SECRET), "stopped: error (provider_error)"),
    ],
    ids=["max_turns", "max_tokens", "refused", "timeout", "provider_error"],
)
def test_a_step_that_stops_without_a_proposal_is_a_bounded_run_error(
    world: Any, entry: Any, expected: str
) -> None:
    result, _, canaries = _run(world, [entry])
    assert result.status == "error"
    assert result.error == f"ToolExecutionError: governed agent step {expected}"
    assert SECRET not in (result.error or "")
    assert canaries.mutations == [] and _rows(world) == ([], [])


def test_a_malformed_result_from_the_adapter_is_not_recorded(world: Any) -> None:
    def claims_commit(request: AgentStepRequest) -> dict[str, Any]:
        return {
            "schema_version": "v1",
            "step_request_id": request.step_request_id,
            "workflow_run_id": request.workflow_run_id,
            "node_id": request.node_id,
            "occurrence_id": request.occurrence_id,
            "attempt": request.attempt,
            "stop_reason": "proposed_action",
            "proposed_actions": [],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "turns": 1,
                "tool_calls": 0,
            },
            "state": "committed",
        }

    result, _, canaries = _run(world, [claims_commit])
    assert result.status == "error"
    assert "malformed_result" in (result.error or "")
    assert canaries.mutations == [] and _rows(world) == ([], [])


def test_without_a_governed_context_the_agent_runs_exactly_as_before(world: Any) -> None:
    plan = _plan(world, governed=False)
    executor = FakeWorkflowExecutor(step_script=[_proposing()])
    canaries = Canaries()
    result = execute(plan, "hello", executor=executor, extra_tools=canaries.tools())
    assert result.status == "completed"
    assert result.output == "[test-agent] processed: hello (used 2 tool(s))"
    assert executor.calls == ["agent"]
    assert executor.step_requests == [], "no governed step was asked for"
    assert executor.step_script.remaining == 1, "the script was not consumed"
    # the legacy fake path, unchanged: the read tool runs, the approval-gated one is a marker and is not executed
    assert len(canaries.reads) == 1 and canaries.mutations == []
    assert _rows(world) == ([], [])


def test_a_governed_step_needs_a_recorded_context(world: Any) -> None:
    plan = _plan(world)
    plan.context_recorder = None
    result, executor, canaries = _run(world, [_proposing()], plan=plan)
    assert result.status == "error"
    assert "requires a recorded context manifest" in (result.error or "")
    assert executor.step_requests == [] and canaries.mutations == [] and _rows(world) == ([], [])


def test_the_context_is_recorded_for_the_step_before_the_provider_is_asked(world: Any) -> None:
    recorder = Recorder()
    digests: list[str] = []

    def digest(step_request_id: str) -> str:
        digests.append(step_request_id)
        assert recorder.assemblies, "the manifest is recorded before its digest is read"
        return "c" * 64

    plan = _plan(world, recorder=recorder, context_manifest_digest=digest)
    result, executor, _ = _run(world, [_proposing()], plan=plan)
    assert result.status == "blocked"
    assert digests == ["step-agent-agent-1"] == [executor.step_requests[0].step_request_id]
    assert executor.step_requests[0].context_manifest_digest == "c" * 64


def test_a_resumed_step_does_not_ask_the_model_again(world: Any) -> None:
    first, executor, _ = _run(world, [_proposing()])
    assert first.status == "blocked"
    proposals, requests = _rows(world)
    again_script = [_proposing(_action(args={"document_type": "a-different-proposal"}))]
    again, executor2, canaries = _run(world, again_script)
    assert again.status == "blocked" and again.error == "waiting_approval:agent"
    assert executor2.step_requests == [], "the occurrence already has its logical effect"
    assert executor2.step_script.remaining == 1
    after, after_requests = _rows(world)
    assert [p.proposal_id for p in after] == [proposals[0].proposal_id]
    assert [r.runtime_approval_id for r in after_requests] == [requests[0].runtime_approval_id]
    assert after[0].args == {"document_type": "pay_statement"}
    (step,) = [s for s in again.steps if s.node_id == "agent"]
    assert step.checkpoint_state is not None
    assert step.checkpoint_state["approval_request_id"] == requests[0].runtime_approval_id
    assert step.checkpoint_state["step_request_id"] is None and step.tokens == 0
    assert canaries.mutations == []


def test_a_resumed_step_after_the_request_lapsed_is_re_asked_not_stranded(world: Any) -> None:
    _run(world, [_proposing()])
    _, requests = _rows(world)
    with world() as session:
        row = session.get(CaliberRuntimeApprovalRequest, requests[0].runtime_approval_id)
        assert row is not None
        row.status, row.consumed_at = "approved", datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
    again, executor, _ = _run(world, [])
    assert again.status == "blocked" and executor.step_requests == []
    _, after = _rows(world)
    assert sorted(r.status for r in after) == ["approved", "pending"]
    (step,) = [s for s in again.steps if s.node_id == "agent"]
    assert step.checkpoint_state is not None
    live = next(r for r in after if r.status == "pending")
    assert step.checkpoint_state["approval_request_id"] == live.runtime_approval_id


def _settle(factory: Callable[[], Session], state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the node's proposal in ``state``: ``failed`` for real, every other state by what the runtime is told.

    Reaching ``dispatched``, ``unknown`` or ``committed`` takes an exact-intent approval, an owner lease and a receipt (the
    dispatcher's own tests cover them); what is under test here is how the node reacts to each state.
    """
    with factory() as session:
        proposal = session.execute(select(CaliberEffectProposal)).scalar_one()
        real = step_proposal.PriorProposal(
            proposal.proposal_id, proposal.state, proposal.intent_hash
        )
    if state == "failed":
        _fail(factory, real.proposal_id)
        return
    monkeypatch.setattr(
        step_proposal,
        "prior_proposal",
        lambda *_a, **_k: replace(real, state=state),
    )


def test_a_resumed_step_whose_effect_committed_completes(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(world, [_proposing()])
    _settle(world, "committed", monkeypatch)
    result, executor, _ = _run(world, [])
    assert result.status == "completed"
    assert executor.step_requests == []
    (step,) = [s for s in result.steps if s.node_id == "agent"]
    proposals, _ = _rows(world)
    assert step.output == f"effect {proposals[0].proposal_id} committed"


def test_a_resumed_step_whose_effect_failed_fails_the_node(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(world, [_proposing()])
    _settle(world, "failed", monkeypatch)
    result, executor, _ = _run(world, [])
    assert result.status == "error" and "its proposed effect failed" in (result.error or "")
    assert executor.step_requests == []


@pytest.mark.parametrize("state", ["approved", "dispatched", "unknown", "reconciled"])
def test_a_resumed_step_whose_effect_is_open_is_the_effect_reconciliation_park(
    world: Any, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(world, [_proposing()])
    _settle(world, state, monkeypatch)
    result, executor, _ = _run(world, [])
    assert result.status == "blocked" and result.error == "waiting_effect:agent"
    assert executor.step_requests == []


def test_a_resumed_step_whose_request_cannot_be_re_asked_fails_with_a_code(world: Any) -> None:
    _run(world, [_proposing()])
    with world() as session:
        session.execute(CaliberRuntimeApprovalRequest.__table__.delete())  # type: ignore[attr-defined]
        session.commit()
    result, _, _ = _run(world, [])
    assert result.status == "error"
    assert (
        result.error
        == "ToolExecutionError: governed agent step approval refused: approval_evidence_unavailable"
    )


def test_a_node_retry_asks_the_model_again_only_when_nothing_was_recorded(world: Any) -> None:
    """A refused proposal records nothing, so the node's retry asks again; a recorded one is found, not re-asked."""
    plan = _plan(world)
    node = plan.ir.nodes["agent"]
    node.execution_policy = replace(node.execution_policy, max_retries=1)
    result, executor, _ = _run(
        world, [_proposing(_action(effect_class="read")), _proposing()], plan=plan
    )
    assert result.status == "blocked"
    assert len(executor.step_requests) == 2
    assert [r.attempt for r in executor.step_requests] == [1, 2]
    assert len({r.step_request_id for r in executor.step_requests}) == 2
    proposals, requests = _rows(world)
    assert len(proposals) == 1 and len(requests) == 1


# ------------------------------------------------------------------- resuming a parked governed step


def _approval_checkpoint(node_id: str = "agent") -> runtime_module.RuntimeResumeCheckpoint:
    return runtime_module.RuntimeResumeCheckpoint(
        node_id=node_id,
        checkpoint_kind="runtime_approval",
        input_by_port={"input": "hello"},
        replay_output=False,
    )


def test_an_agent_node_resumes_from_a_runtime_approval_checkpoint_only_in_a_governed_plan(
    world: Any,
) -> None:
    from caliber.orchestrator.workflow_run_worker import _resume_checkpoint_preflight_error

    checkpoint = _approval_checkpoint()
    ungoverned = _plan(world, governed=False)
    refused = execute(
        ungoverned, "hello", executor=FakeWorkflowExecutor(), resume_checkpoint=checkpoint
    )
    assert refused.status == "error"
    assert "resume checkpoint kind 'runtime_approval' does not match current node 'agent'" in (
        refused.error or ""
    )
    assert _resume_checkpoint_preflight_error(ungoverned, checkpoint) is not None

    governed = _plan(world)
    assert _resume_checkpoint_preflight_error(governed, checkpoint) is None
    resumed = execute(
        governed,
        "hello",
        executor=FakeWorkflowExecutor(step_script=[_proposing()]),
        resume_checkpoint=checkpoint,
        extra_tools=Canaries().tools(),
    )
    assert resumed.status == "blocked" and resumed.error == "waiting_approval:agent"
    assert len(_rows(world)[0]) == 1


def test_a_node_that_is_neither_a_gated_tool_nor_a_governed_agent_cannot_resume_from_an_approval(
    world: Any,
) -> None:
    from caliber.orchestrator.workflow_run_worker import _resume_checkpoint_preflight_error

    plan = _plan(world)
    assert (
        runtime_module._resume_checkpoint_kind_matches_node(
            "runtime_approval", plan.ir.nodes["final"], governed_agent_steps=True
        )
        is False
    )
    assert _resume_checkpoint_preflight_error(plan, _approval_checkpoint("final")) is not None
