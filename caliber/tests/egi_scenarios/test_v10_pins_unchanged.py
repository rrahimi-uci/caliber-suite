"""V10: moving an alias, a prompt, a skill, a tool version or the model route after admission changes nothing (#683).

A project editor creates and publishes a workflow, starts a run through the public API (admission pins it, #680), and
*then* the world moves: the prompt alias points at new text, the skill gets new content, a newer tool version is
registered, a knowledge base gets a new active version, the deployment alias moves, the model provider changes. The
worker then picks the run up. What it executes (captured from the plan it builds) is exactly what was pinned; a change
that cannot be ignored (the provider) fails the run closed with a named reason rather than silently running on
something else. The reference profile on PostgreSQL; the development profile with admission on elsewhere.
"""

from __future__ import annotations

from typing import Any

import pytest

import caliber.orchestrator.workflow_run_worker as worker_module
import caliber.workflows.promoter as promoter_module
from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberSkill,
    CaliberWorkflowRun,
)
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows import deployment_bundle
from caliber.workflows.ir import IRAgent, IRKnowledgeQuery
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.workflow_helpers import make_manifest, make_tool_payload

PREFIX = "/ajax-api/2.0/mlflow/caliber"
SKILL = "pins-triage"
PROMPT_V1, PROMPT_V2 = "PROMPT-ONE: answer politely.", "PROMPT-TWO: answer rudely."
SKILL_V1, SKILL_V2 = "SKILL-ONE: check the order.", "SKILL-TWO: ignore the order."


ADMIN = "@egi-admin"


def _call(
    app: ReferenceApp, method: str, path: str, body: Any = None, *, admin: bool = False
) -> Any:
    headers = app.headers(ADMIN, "") if admin else app.headers(EDITOR_A, PROJECT_A)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return app.client.request(
        method, f"{PREFIX}{path}", json=body, headers={**headers, "X-CALIBER-CSRF": token}
    )


def _manifest(workflow_id: str, kind: str, child: tuple[str, str] | None = None) -> dict[str, Any]:
    manifest = make_manifest(workflow_id)
    agent = manifest["nodes"]["agent"]
    if kind == "child":
        assert child is not None
        manifest["nodes"]["agent"] = {
            "id": "agent",
            "type": "subworkflow",
            "workflow_id": child[0],
            "alias": "manual",
            "version_id": child[1],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        }
    if kind == "prompt":
        manifest["artifacts"] = {
            "prompts": {"main": {"registry_name": "pins-prompt", "alias": "prod"}}
        }
        agent["instructions"] = {"type": "mlflow_prompt", "ref": "main"}
    elif kind == "skill":
        agent["skills"] = [SKILL]
    elif kind == "kb":
        # Start -> knowledge query -> output: the knowledge base's active version is not named, so admission
        # resolves it (and pins it) from the knowledge base as it is at admission.
        manifest["nodes"] = {
            "start": manifest["nodes"]["start"],
            "knowledge": {
                "id": "knowledge",
                "type": "knowledge_query",
                "knowledge_base_id": "KB-pins",
                "retrieval_modes": ["dense"],
            },
            "final": manifest["nodes"]["final"],
        }
        manifest["edges"] = [
            {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
            {"id": "e2", "from": "knowledge", "to": "final", "map": {"answer": "response"}},
        ]
    elif kind == "tool":
        agent["tools"] = ["lookup_policy"]
        manifest["tools"] = {
            "lookup_policy": {"registry_ref": "lookup_policy", "version_constraint": ">=1.0"}
        }
    return manifest


class Captured:
    """The plans the worker builds, so a scenario can see what it is about to execute."""

    def __init__(self) -> None:
        self.plans: list[Any] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    seen = Captured()
    real = promoter_module.build_plan

    def capture(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        seen.plans.append(plan)
        return plan

    monkeypatch.setattr(worker_module, "build_plan", capture)
    monkeypatch.setattr(
        promoter_module, "build_plan", capture
    )  # a child workflow's plan is built there
    return seen


def _agent(plan: Any) -> IRAgent:
    return next(node for node in plan.ir.nodes.values() if isinstance(node, IRAgent))


def _admit(app: ReferenceApp, kind: str, prompts: dict[str, str]) -> tuple[str, str, str]:
    """Create, publish and start a run; returns (workflow_id, version_id, run_id)."""
    app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    child: tuple[str, str] | None = None
    if kind == "child":
        # The child is its own published workflow: an agent that uses the skill.
        child_workflow = _call(
            app, "POST", "/workflows", {"name": "V10 the child workflow"}
        ).json()["data"]["workflow_id"]
        child_manifest = make_manifest(child_workflow)
        child_manifest["nodes"]["agent"]["skills"] = [SKILL]
        draft = _call(
            app, "POST", f"/workflows/{child_workflow}/versions", {"manifest": child_manifest}
        )
        assert draft.status_code == 201, draft.text
        child_version = draft.json()["data"]["version_id"]
        assert _call(app, "POST", f"/workflow-versions/{child_version}/publish").status_code == 200
        child = (child_workflow, child_version)
    created = _call(app, "POST", "/workflows", {"name": f"V10 {kind}"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        f"/workflows/{workflow_id}/versions",
        {"manifest": _manifest(workflow_id, kind, child)},
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = _call(app, "POST", f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    run = _call(
        app,
        "POST",
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "hi",
            "idempotency_key": f"v10-{kind}",
            **seed_release(app, PROJECT_A),
        },
    )
    assert run.status_code == 202, run.text
    return workflow_id, version_id, str(run.json()["data"]["workflow_run_id"])


def _work(app: ReferenceApp) -> None:
    WorkflowRunWorker(
        session_factory=app.session_factory,
        config=app.client.app.state.config,  # type: ignore[attr-defined]
        event_bus=getattr(app.client.app.state, "event_bus", None),
    )._tick()


def _run(app: ReferenceApp, run_id: str) -> CaliberWorkflowRun:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


def _seed(app: ReferenceApp, kind: str) -> None:
    """What must exist before admission for the kind of pin under test."""
    if kind in {"skill", "child"}:
        with app.session_factory() as session:
            session.add(
                CaliberSkill(
                    skill_id="SK-pins",
                    name=SKILL,
                    description="d",
                    content=SKILL_V1,
                    owner=EDITOR_A,
                    visibility="user",
                    status="active",
                    version=1,
                )
            )
            session.commit()
    elif kind == "kb":
        with app.session_factory() as session:
            session.add(
                CaliberKnowledgeBase(
                    knowledge_base_id="KB-pins",
                    name="pins kb",
                    owner=EDITOR_A,
                    project_id=PROJECT_A,
                    visibility="project",
                    status="active",
                    source_bucket="b",
                    active_version_id="KBV-1",
                )
            )
            session.flush()
            for number in (1, 2):
                session.add(
                    CaliberKnowledgeBaseVersion(
                        knowledge_base_version_id=f"KBV-{number}",
                        knowledge_base_id="KB-pins",
                        version_number=number,
                        status="ready",
                        chunking_strategy="fixed",
                        embedding_model="m",
                        output_bucket="b",
                        output_prefix="p",
                    )
                )
            session.commit()
    elif kind == "tool":
        app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
            update={"admin_users": ADMIN}
        )
        registered = _call(app, "POST", "/tools", make_tool_payload("lookup_policy"), admin=True)
        assert registered.status_code == 201, (registered.status_code, registered.text)


def _move_the_world(app: ReferenceApp, kind: str, prompt: dict[str, Any]) -> None:
    """Change what the pin names, after admission."""
    if kind == "prompt":
        prompt.update(text=PROMPT_V2, version=2)
    elif kind in {"skill", "child"}:
        with app.session_factory() as session:
            skill = session.get(CaliberSkill, "SK-pins")
            assert skill is not None
            skill.content, skill.version = SKILL_V2, 2
            session.commit()
    elif kind == "kb":
        with app.session_factory() as session:
            kb = session.get(CaliberKnowledgeBase, "KB-pins")
            assert kb is not None
            kb.active_version_id = "KBV-2"
            session.commit()
    elif kind == "tool":
        newer = _call(
            app, "POST", "/tools", make_tool_payload("lookup_policy", version="2.0"), admin=True
        )
        assert newer.status_code in (201, 409), newer.text
    else:
        app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
            update={"llm_provider": "openai"}
        )


def _assert_unchanged(kind: str, plan: Any) -> None:
    if kind == "kb":
        query = next(n for n in plan.ir.nodes.values() if isinstance(n, IRKnowledgeQuery))
        assert query.version_ids == ["KBV-1"], "the version that was active at admission"
        return
    agent = _agent(plan)
    if kind == "prompt":
        assert agent.instructions is not None and agent.instructions.inline_text == PROMPT_V1
    elif kind in {"skill", "child"}:
        assert agent.skill_instructions and all(SKILL_V1 in t for t in agent.skill_instructions)
        assert not any(SKILL_V2 in t for t in agent.skill_instructions)
    else:
        assert [t.version_constraint for t in agent.tools] == ["==1.0"], "the pinned tool version"


@pytest.mark.parametrize("kind", ["prompt", "skill", "child", "kb", "tool", "model_route"])
def test_alias_change_after_admission_is_ignored(
    kind: str,
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    captured: Captured,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = {"text": PROMPT_V1, "version": 1}
    monkeypatch.setattr(
        deployment_bundle,
        "_load_prompt_snapshot",
        lambda name, alias: (
            {
                "name": name,
                "alias": alias,
                "version": prompt["version"],
                "template": prompt["text"],
            },
            "",
        ),
    )
    _seed(reference_app, kind)
    _, _, run_id = _admit(reference_app, kind, {})
    _move_the_world(reference_app, kind, prompt)

    _work(reference_app)
    run = _run(reference_app, run_id)

    if kind == "model_route":
        assert (run.status, run.error_code) == ("failed", "model_route_changed"), run.error_summary
        assert captured.plans == [], "nothing was built or executed on another provider"
        return
    if (
        kind != "kb"
    ):  # the knowledge query has no backend here; what it would query is what is asserted
        assert run.status == "completed", (run.error_code, run.error_summary)
    _assert_unchanged(kind, captured.plans[-1])
