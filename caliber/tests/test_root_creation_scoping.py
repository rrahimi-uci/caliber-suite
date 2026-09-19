"""`P2-A` (isolation closure, item 2): root-creation identity derivation.

Every project-owned "root" resource (`db/resource_inventory.py`'s
``SCOPING_VISIBILITY`` classification -- a model with ``project_id`` +
``visibility`` + a resolvable owner column) must derive its owner/
project/visibility from the *authenticated caller's identity*, never from
caller-supplied request-body metadata, when a new row is created. This is
the same guarantee slice 9 already proved for skill-package imports
(``test_skill_package_zip_import.py`` / ``test_skill_packages.py``) and
that ``test_routes_scoping.py`` proves for ``create_skill`` itself (used
there as "the representative scoped resource -- every scoped list/create
endpoint shares the same wiring") and ``test_mcp_servers_routes.py``
proves for ``create_mcp_server``.

A full read-through audit of every root-creation route for the 15
``SCOPING_VISIBILITY`` models found every one of them already following
this pattern correctly (see the `P2-A` row in ``docs/workspace-plan.md``
for the full inventory). This file closes the *test-coverage* gap that
audit also found: several of these routes -- ``register_tool``,
``create_dataset``, ``create_judge``, ``create_prompt`` -- had no
route-level test proving the invariant directly, unlike skills/workflows/
mcp-servers. Nothing here changes production code; it locks in behavior
that was already correct so a future regression is caught.

``create_judge`` cannot even accept a caller-supplied ``owner`` -- its
request schema has no such field and rejects unknown keys
(``extra="forbid"``, rendered as a structured ``400`` by
``routes/_errors.py``, not a raw pydantic ``422``) -- so its test proves
the *stronger* claim that the override surface does not exist at all,
rather than merely that a supplied value is discarded. ``register_tool``
does accept (and ignore) a caller-supplied ``owner`` field, matching the
weaker "accepted but discarded" shape the other families use.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberJudge,
    CaliberProject,
    CaliberToolRegistry,
    CaliberWorkflow,
)
from caliber.routes.eval_datasets import LIST_PATH as EVAL_DATASETS_PATH
from caliber.routes.judges import LIST_PATH as JUDGES_PATH
from caliber.routes.tools import LIST_PATH as TOOLS_PATH

WORKFLOWS_PATH = "/ajax-api/2.0/mlflow/caliber/workflows"
PROMPTS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts"


def _seed_project(session: Session, project_id: str, *, owner: str = "@test") -> None:
    session.add(CaliberProject(project_id=project_id, name=project_id, owner=owner))
    session.commit()


# ── tools (register_tool) ──────────────────────────────────────────────


def _tool_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "root-scoping-tool",
        "version": "1.0.0",
        "module_path": "caliber.tools.demo",
        "callable_name": "run",
    }
    body.update(overrides)
    return body


def test_register_tool_under_active_project_is_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session, "PRJ-TOOLS")
    resp = client.post(TOOLS_PATH, json=_tool_body(), headers={"X-CALIBER-Project": "PRJ-TOOLS"})
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberToolRegistry, resp.json()["data"]["tool_id"])
    assert row is not None
    assert row.owner == "@test"
    assert row.project_id == "PRJ-TOOLS"
    assert row.visibility == "project"


def test_register_tool_without_active_project_defaults_to_public_catalog(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post(TOOLS_PATH, json=_tool_body(name="root-scoping-tool-no-proj"))
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberToolRegistry, resp.json()["data"]["tool_id"])
    assert row is not None
    assert row.project_id is None
    assert row.visibility == "public"


def test_register_tool_ignores_caller_supplied_owner(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post(
        TOOLS_PATH, json=_tool_body(name="root-scoping-tool-owner", owner="@spoofed")
    )
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberToolRegistry, resp.json()["data"]["tool_id"])
    assert row is not None
    assert row.owner == "@test"  # actor, not the spoofed body value


# ── eval datasets / "test sets" (create_dataset) ───────────────────────


def test_create_dataset_under_active_project_is_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session, "PRJ-EVALDS")
    resp = client.post(
        EVAL_DATASETS_PATH,
        json={"name": "root-scoping-dataset", "owner": "@spoofed"},
        headers={"X-CALIBER-Project": "PRJ-EVALDS"},
    )
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberEvalDataset, resp.json()["data"]["dataset_id"])
    assert row is not None
    assert row.owner == "@test"  # actor, not the spoofed body value
    assert row.project_id == "PRJ-EVALDS"
    assert row.visibility == "project"


def test_create_dataset_without_active_project_defaults_to_personal_library(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post(
        EVAL_DATASETS_PATH,
        json={"name": "root-scoping-dataset-no-proj", "owner": "@spoofed"},
    )
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberEvalDataset, resp.json()["data"]["dataset_id"])
    assert row is not None
    assert row.owner == "@test"
    assert row.project_id is None
    assert row.visibility == "user"


# ── judges / "scorers" (create_judge) ──────────────────────────────────


def _judge_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "root-scoping-judge",
        "instructions": "Score the response given {{ outputs }}.",
    }
    body.update(overrides)
    return body


def test_create_judge_under_active_project_is_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session, "PRJ-JUDGES")
    resp = client.post(JUDGES_PATH, json=_judge_body(), headers={"X-CALIBER-Project": "PRJ-JUDGES"})
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberJudge, resp.json()["data"]["judge_id"])
    assert row is not None
    assert row.owner == "@test"
    assert row.project_id == "PRJ-JUDGES"
    assert row.visibility == "project"


def test_create_judge_without_active_project_defaults_to_personal_library(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post(JUDGES_PATH, json=_judge_body(name="root-scoping-judge-no-proj"))
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberJudge, resp.json()["data"]["judge_id"])
    assert row is not None
    assert row.project_id is None
    assert row.visibility == "user"


def test_create_judge_has_no_owner_override_surface(client: TestClient) -> None:
    # JudgeCreateRequest has no `owner` field and forbids unknown keys --
    # a caller cannot even attempt to choose the owner, unlike a field
    # that is merely accepted and ignored. Rendered as a structured 400 by
    # `routes/_errors.py`'s pydantic-ValidationError handler.
    resp = client.post(JUDGES_PATH, json=_judge_body(owner="@spoofed"))
    assert resp.status_code == 400


# ── workflows (create_workflow) ─────────────────────────────────────────
#
# ``test_routes_workflows.py`` already proves the owner-from-actor half
# (``test_create_workflow_publishes_created_event`` posts
# ``owner="@ignored"`` and asserts the persisted/broadcast owner is
# ``@test``). This closes the other half: project derivation itself was
# untested.


def test_create_workflow_under_active_project_is_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session, "PRJ-WORKFLOWS")
    resp = client.post(
        WORKFLOWS_PATH,
        json={"name": "root-scoping-workflow"},
        headers={"X-CALIBER-Project": "PRJ-WORKFLOWS"},
    )
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberWorkflow, resp.json()["data"]["workflow_id"])
    assert row is not None
    assert row.project_id == "PRJ-WORKFLOWS"
    assert row.visibility == "project"


def test_create_workflow_without_active_project_defaults_to_personal_library(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post(WORKFLOWS_PATH, json={"name": "root-scoping-workflow-no-proj"})
    assert resp.status_code == 201, resp.text
    row = db_session.get(CaliberWorkflow, resp.json()["data"]["workflow_id"])
    assert row is not None
    assert row.project_id is None
    assert row.visibility == "user"


# ── prompts (create_prompt / its hidden runtime target) ────────────────
#
# A "prompt" has no `CaliberPrompt` model of its own (`P2-G`): the only
# CALIBER-side row is the hidden runtime target `ensure_prompt_target`
# provisions. `create_prompt` parses its body as a raw dict (not a
# pydantic schema), so an `owner` key is not even read anywhere -- there
# is no override surface to forbid, it is simply never consulted.


def _install_fake_mlflow_registry(monkeypatch) -> None:
    import sys
    import types
    from types import SimpleNamespace

    def register_prompt(**kwargs: object) -> object:
        name = str(kwargs["name"])
        return SimpleNamespace(version=1, uri=f"prompts:/{name}/1")

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = SimpleNamespace(
        load_prompt=lambda ref, allow_missing=False: None,
        search_prompts=lambda: [],
        register_prompt=register_prompt,
        set_prompt_alias=lambda name, alias, version: None,
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)


def test_create_prompt_under_active_project_is_project_scoped(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _install_fake_mlflow_registry(monkeypatch)
    _seed_project(db_session, "PRJ-PROMPTS")
    resp = client.post(
        PROMPTS_PATH,
        json={"name": "root_scoping_prompt", "template": "hi", "owner": "@spoofed"},
        headers={"X-CALIBER-Project": "PRJ-PROMPTS"},
    )
    assert resp.status_code == 201, resp.text
    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "root_scoping_prompt")
    assert target is not None
    assert target.owner == "@test"  # actor, never `owner` from the body
    assert target.project_id == "PRJ-PROMPTS"
    assert target.visibility == "project"


def test_create_prompt_without_active_project_defaults_to_personal_library(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _install_fake_mlflow_registry(monkeypatch)
    resp = client.post(
        PROMPTS_PATH,
        json={"name": "root_scoping_prompt_no_proj", "template": "hi", "owner": "@spoofed"},
    )
    assert resp.status_code == 201, resp.text
    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "root_scoping_prompt_no_proj")
    assert target is not None
    assert target.owner == "@test"
    assert target.project_id is None
    assert target.visibility == "user"
