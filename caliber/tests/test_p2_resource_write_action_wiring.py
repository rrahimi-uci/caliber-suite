"""Route-level proof for `P2` (isolation closure): `resource.write.runtime`
(prompts/workflows/tools/skills, and now knowledge bases/OpenAPI
integrations/LLM pricing -- `P2-A`'s own remaining "convert root routes by
resource family to centralized authorization" scope) and `resource.write.
evidence` (test sets/judges) are now wired onto the real CRUD routes for
these resource families, closing the gap `docs/workspace-plan.md` section
16's `P1-D` row left open -- those routes previously checked only a global
platform scope (`caliber.operator`/`caliber.admin`), with no
project-membership-role check at all, so any identity holding the right
global scope could write into *any* project's prompts/workflows/tools/
skills/test-sets/judges/knowledge-bases/OpenAPI-integrations/LLM-pricing
rows regardless of their actual role or membership in that project.

Follows `test_p1d_action_registry_wiring.py`'s established pattern and
denial-shape split:

* A second **admin** with no project membership reaches the project-role
  check at all only because `caliber.admin` bypasses the separate
  resource-*visibility* filter (`db/scoping.py`) unconditionally (where the
  route reads an existing row through it) -- proving the role check is a
  real, independent gate, not a no-op riding on that bypass.
* A genuine project **viewer** member is visible (membership grants
  visibility) but denied by role -- proving the check denies a real, if
  under-privileged, member, not just a stranger.
* A correctly-roled member (**editor** for `.runtime`, **editor**/
  **reviewer** for `.evidence`) succeeds.

One route per resource family (the family's primary "create" route) gets
the full three-scenario proof plus the unscoped (`project_id is None`)
no-op case; every other touched route in that family gets the deny/allow
pair, since it shares the exact same `require_project_access_if_scoped`
call and `PROJECT_ACTIONS` role set already fully proven by its family's
canonical route -- what differs per additional route is only that the call
is actually present at that specific call site, which the deny/allow pair
already demonstrates.
"""

from __future__ import annotations

import io
import types
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberKnowledgeBase, CaliberProject, CaliberProjectMember
from caliber.resource_access import ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER
from caliber.routes import mcp_servers as mcp_routes
from caliber.routes.eval_datasets import DETAIL_PATH as DATASET_DETAIL_PATH
from caliber.routes.eval_datasets import EXAMPLES_PATH
from caliber.routes.eval_datasets import FROM_TRACE_PATH as DATASET_FROM_TRACE_PATH
from caliber.routes.eval_datasets import LIST_PATH as DATASET_LIST_PATH
from caliber.routes.eval_datasets import RESTORE_PATH as DATASET_RESTORE_PATH
from caliber.routes.eval_datasets import REVISE_PATH as DATASET_REVISE_PATH
from caliber.routes.eval_datasets import SUPERSEDE_PATH as DATASET_SUPERSEDE_PATH
from caliber.routes.judges import DETAIL_PATH as JUDGE_DETAIL_PATH
from caliber.routes.judges import LIST_PATH as JUDGE_LIST_PATH
from caliber.routes.knowledge_bases import DETAIL_PATH as KB_DETAIL_PATH
from caliber.routes.knowledge_bases import LIST_PATH as KB_LIST_PATH
from caliber.routes.llm_pricing import DETAIL_PATH as PRICING_DETAIL_PATH
from caliber.routes.llm_pricing import LIST_PATH as PRICING_LIST_PATH
from caliber.routes.mcp_servers import DETAIL_PATH as MCP_DETAIL_PATH
from caliber.routes.mcp_servers import DISCOVER_PATH as MCP_DISCOVER_PATH
from caliber.routes.mcp_servers import INVOKE_PATH as MCP_INVOKE_PATH
from caliber.routes.mcp_servers import LIST_PATH as MCP_LIST_PATH
from caliber.routes.mcp_servers import TEST_PATH as MCP_TEST_PATH
from caliber.routes.mcp_servers import TOOL_POLICY_PATH as MCP_TOOL_POLICY_PATH
from caliber.routes.openapi_integrations import ARCHIVE_PATH as OPENAPI_ARCHIVE_PATH
from caliber.routes.openapi_integrations import DETAIL_PATH as OPENAPI_DETAIL_PATH
from caliber.routes.openapi_integrations import LIST_PATH as OPENAPI_LIST_PATH
from caliber.routes.prompts import CREATE_PATH as PROMPT_CREATE_PATH
from caliber.routes.prompts import DETAIL_PATH as PROMPT_DETAIL_PATH
from caliber.routes.prompts import VERSION_PATH as PROMPT_VERSION_PATH
from caliber.routes.skills import DETAIL_PATH as SKILL_DETAIL_PATH
from caliber.routes.skills import IMPORT_PACKAGE_PATH as SKILL_IMPORT_PACKAGE_PATH
from caliber.routes.skills import IMPORT_PACKAGE_ZIP_PATH as SKILL_IMPORT_PACKAGE_ZIP_PATH
from caliber.routes.skills import LIST_PATH as SKILL_LIST_PATH
from caliber.routes.tools import ARCHIVE_PATH as TOOL_ARCHIVE_PATH
from caliber.routes.tools import DETAIL_PATH as TOOL_DETAIL_PATH
from caliber.routes.tools import LIST_PATH as TOOL_LIST_PATH
from caliber.routes.workflows import DETAIL_PATH as WORKFLOW_DETAIL_PATH
from caliber.routes.workflows import IMPORT_PATH as WORKFLOW_IMPORT_PATH
from caliber.routes.workflows import LIST_PATH as WORKFLOW_LIST_PATH
from tests.workflow_helpers import (
    make_support_manifest,
    make_tool_payload,
    register_demo_tools,
    seed_eval_dataset,
)

PROJECT_ID = "P-p2-wiring"


def _seed_project(session: Session, *, owner: str = "@test") -> None:
    session.add(CaliberProject(project_id=PROJECT_ID, name="P2 wiring project", owner=owner))
    session.commit()


def _add_member(session: Session, user_id: str, role: str) -> None:
    session.add(
        CaliberProjectMember(
            member_id=f"M-{user_id.lstrip('@')}",
            project_id=PROJECT_ID,
            user_id=user_id,
            role=role,
            created_by="@test",
        )
    )
    session.commit()


def _grant_operator(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.operator_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(
        update={"operator_users": ",".join(sorted(existing))}
    )


def _grant_admin(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.admin_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(update={"admin_users": ",".join(sorted(existing))})


class _FakeMlflowClient:
    """Just enough of ``MlflowClient`` for ``delete_prompt``'s direct
    (non-cascade) path to succeed."""

    def delete_prompt(self, _name: str) -> None:
        return None


def _install_fake_mlflow_prompt_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    """A minimal fake ``mlflow.genai`` registry -- just enough for
    ``register_prompt_version``'s create/version-create calls and
    ``delete_prompt``'s ``MlflowClient`` lookup to succeed without a real
    MLflow server, matching ``test_routes_prompts.py``'s own
    ``_install_mlflow`` helper (kept local/minimal here since this file only
    needs the create/delete paths, not the full prompt-listing surface).

    Returns the list of ``register_prompt`` call kwargs so a refused request
    can assert it never reached MLflow (no orphaned version)."""
    calls: list[dict[str, object]] = []

    def register_prompt(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(version=len(calls), uri=f"prompts:/{kwargs['name']}/{len(calls)}")

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = SimpleNamespace(register_prompt=register_prompt)
    mlflow_mod.MlflowClient = _FakeMlflowClient  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "mlflow", mlflow_mod)
    return calls


# ---------------------------------------------------------------------------
# prompts.py -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_create_prompt_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-wiring-prompt", "template": "hello"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_prompt_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-wiring-prompt", "template": "hello"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_prompt_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-wiring-prompt", "template": "hello"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_prompt_still_works_for_an_unscoped_prompt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-op path: no active project means no workspace to gate against."""
    _install_fake_mlflow_prompt_registry(monkeypatch)

    resp = client.post(PROMPT_CREATE_PATH, json={"name": "personal-prompt", "template": "hello"})
    assert resp.status_code == 201, resp.text


# `P2-G` (docs/workspace-plan.md section 16): the actual MLflow-side
# namespace-collision refusal. MLflow's Prompt Registry has one flat, global
# namespace -- registering a version under a name that already exists there
# always lands on the SAME entity, whoever calls it. `create_prompt`'s
# pre-existing check just above (`get_visible` -- proven 404 by
# ``test_create_prompt_denies_an_admin_with_no_project_membership`` above for
# a *new* name) only covers whether the caller can *see* an existing hidden
# target; `db/scoping.py::apply_visibility_filter` deliberately gives an
# admin identity an unconditional cross-project bypass of that same check
# (so admins can inspect/manage every project), which would otherwise let an
# admin acting on behalf of project B sail straight past project A's
# ownership and silently add a version onto project A's own MLflow prompt
# entity. The two tests below prove the *new*, separate project-ownership
# comparison closes that gap without touching same-project re-registration.


def test_create_prompt_refuses_a_cross_project_name_collision_even_for_an_admin(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_calls = _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    # Project A (PROJECT_ID) claims the name first, as a real project editor.
    first = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "shared-name", "template": "hello"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert first.status_code == 201, first.text
    assert len(register_calls) == 1

    # The default test client is an admin (`DEFAULT_TEST_USER`, see
    # ``conftest.py``'s ``app_config``) with no membership in either
    # project. Admin visibility would let this request see project A's
    # target just fine -- it must still be refused because the target
    # belongs to a genuinely different project than the caller's active one.
    collide = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "shared-name", "template": "goodbye"},
        headers={"X-CALIBER-Project": "P-a-different-project"},
    )
    assert collide.status_code == 409, collide.text
    assert "shared-name" in collide.json()["detail"]
    # No orphaned MLflow version: the refused attempt never reached the
    # registry at all -- still exactly the one call from project A above.
    assert len(register_calls) == 1


def test_create_prompt_allows_the_same_project_to_reregister_its_own_name(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new project-ownership check above must not block a project
    re-registering/re-versioning its OWN existing name -- this is not a
    collision, and must keep working exactly as before this change."""
    register_calls = _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    headers = {"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID}

    first = client.post(
        PROMPT_CREATE_PATH, json={"name": "own-name", "template": "v1"}, headers=headers
    )
    assert first.status_code == 201, first.text

    again = client.post(
        PROMPT_CREATE_PATH, json={"name": "own-name", "template": "v2"}, headers=headers
    )
    assert again.status_code == 201, again.text
    assert len(register_calls) == 2


def test_create_prompt_version_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    created = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-version-prompt", "template": "v1"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        PROMPT_VERSION_PATH.replace("{name}", "p2-version-prompt"),
        json={"template": "v2"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_prompt_version_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    created = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-version-prompt", "template": "v1"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        PROMPT_VERSION_PATH.replace("{name}", "p2-version-prompt"),
        json={"template": "v2"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_delete_prompt_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    created = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-delete-prompt", "template": "v1"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    # `delete_prompt`'s global-scope floor is `SCOPE_ADMIN`, kept unchanged --
    # grant it so this request reaches the project-role check under test
    # rather than being refused earlier by the global-scope gate.
    _grant_admin(client, "@viewer-user")

    resp = client.delete(
        PROMPT_DETAIL_PATH.replace("{name}", "p2-delete-prompt"),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_delete_prompt_allows_a_project_editor_with_admin_scope(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_mlflow_prompt_registry(monkeypatch)
    _seed_project(db_session)
    created = client.post(
        PROMPT_CREATE_PATH,
        json={"name": "p2-delete-prompt", "template": "v1"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.delete(
        PROMPT_DETAIL_PATH.replace("{name}", "p2-delete-prompt"),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# workflows.py -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_create_workflow_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-wiring-workflow"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_workflow_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-wiring-workflow"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_workflow_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-wiring-workflow"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_workflow_still_works_for_an_unscoped_workflow(client: TestClient) -> None:
    resp = client.post(WORKFLOW_LIST_PATH, json={"name": "personal-workflow"})
    assert resp.status_code == 201, resp.text


def test_update_workflow_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-update-workflow"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        WORKFLOW_DETAIL_PATH.replace("{workflow_id}", workflow_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_workflow_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-update-workflow"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.patch(
        WORKFLOW_DETAIL_PATH.replace("{workflow_id}", workflow_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_delete_workflow_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-delete-workflow"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.delete(
        WORKFLOW_DETAIL_PATH.replace("{workflow_id}", workflow_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_delete_workflow_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        WORKFLOW_LIST_PATH,
        json={"name": "p2-delete-workflow"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.delete(
        WORKFLOW_DETAIL_PATH.replace("{workflow_id}", workflow_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_import_workflow_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    register_demo_tools(client)
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        WORKFLOW_IMPORT_PATH,
        json={"manifest": make_support_manifest("p2-import-workflow")},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_import_workflow_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    register_demo_tools(client)
    # The support manifest's eval-dataset dependency (`support-eval-v3`) must
    # be visible to resolve in preflight -- unscoped (`project_id` unset), so
    # visible cross-project, same as the demo tools above.
    seed_eval_dataset(db_session)
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        WORKFLOW_IMPORT_PATH,
        json={"manifest": make_support_manifest("p2-import-workflow")},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# tools.py -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_register_tool_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_wiring_tool"),
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_register_tool_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_wiring_tool"),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_register_tool_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_wiring_tool"),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_register_tool_still_works_for_an_unscoped_tool(client: TestClient) -> None:
    resp = client.post(TOOL_LIST_PATH, json=make_tool_payload("personal_tool"))
    assert resp.status_code == 201, resp.text


def test_update_tool_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_update_tool"),
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["data"]["tool_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        TOOL_DETAIL_PATH.replace("{tool_id}", tool_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_tool_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_update_tool"),
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["data"]["tool_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        TOOL_DETAIL_PATH.replace("{tool_id}", tool_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_archive_tool_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_archive_tool"),
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["data"]["tool_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        TOOL_ARCHIVE_PATH.replace("{tool_id}", tool_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_archive_tool_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        TOOL_LIST_PATH,
        json=make_tool_payload("p2_archive_tool"),
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["data"]["tool_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        TOOL_ARCHIVE_PATH.replace("{tool_id}", tool_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# skills.py -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_create_skill_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        SKILL_LIST_PATH,
        json={"name": "p2-wiring-skill", "content": "body", "owner": "@admin2"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_skill_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        SKILL_LIST_PATH,
        json={"name": "p2-wiring-skill", "content": "body", "owner": "@viewer-user"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_skill_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        SKILL_LIST_PATH,
        json={"name": "p2-wiring-skill", "content": "body", "owner": "@editor-user"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_skill_still_works_for_an_unscoped_skill(client: TestClient) -> None:
    resp = client.post(
        SKILL_LIST_PATH, json={"name": "personal-skill", "content": "body", "owner": "@test"}
    )
    assert resp.status_code == 201, resp.text


def test_update_skill_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        SKILL_LIST_PATH,
        json={"name": "p2-update-skill", "content": "body", "owner": "@test"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["data"]["skill_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        SKILL_DETAIL_PATH.replace("{skill_id}", skill_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_skill_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        SKILL_LIST_PATH,
        json={"name": "p2-update-skill", "content": "body", "owner": "@test"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["data"]["skill_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        SKILL_DETAIL_PATH.replace("{skill_id}", skill_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


_SKILL_PACKAGE_FILES = [
    {
        "path": "folder-reader/SKILL.md",
        "content": (
            "---\nname: folder-reader\ndescription: Read files before answering.\n---\n\n"
            "# Folder Reader\n\nInspect files and summarize them."
        ),
    }
]


def test_import_skill_package_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        SKILL_IMPORT_PACKAGE_PATH,
        json={"owner": "@viewer-user", "files": _SKILL_PACKAGE_FILES},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_import_skill_package_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        SKILL_IMPORT_PACKAGE_PATH,
        json={"owner": "@editor-user", "files": _SKILL_PACKAGE_FILES},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def _skill_zip(name: str = "portable-skill") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: Portable skill\n---\n\nBody.\n",
        )
    return stream.getvalue()


def _upload_skill_zip(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(
        SKILL_IMPORT_PACKAGE_ZIP_PATH,
        data={"conflict_strategy": "reject", "rename_to": ""},
        files={"file": ("skill.zip", _skill_zip(), "application/zip")},
        headers=headers,
    )


def test_import_skill_package_zip_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = _upload_skill_zip(
        client, {"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID}
    )
    assert resp.status_code == 403, resp.text


def test_import_skill_package_zip_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = _upload_skill_zip(
        client, {"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID}
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# eval_datasets.py -- `resource.write.evidence`
# ---------------------------------------------------------------------------


def test_create_dataset_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        DATASET_LIST_PATH,
        json={"name": "p2-wiring-dataset", "owner": "@admin2"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_dataset_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        DATASET_LIST_PATH,
        json={"name": "p2-wiring-dataset", "owner": "@viewer-user"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_dataset_allows_a_project_reviewer(client: TestClient, db_session: Session) -> None:
    """`resource.write.evidence`'s role set is wider than `.runtime`'s --
    includes `reviewer` (QA), not just `owner`/`editor`. Using a reviewer
    here (rather than editor, already proven for `.runtime`) exercises that
    extra grant explicitly."""
    _seed_project(db_session)
    _add_member(db_session, "@reviewer-user", ROLE_REVIEWER)
    _grant_operator(client, "@reviewer-user")

    resp = client.post(
        DATASET_LIST_PATH,
        json={"name": "p2-wiring-dataset", "owner": "@reviewer-user"},
        headers={"X-CALIBER-User": "@reviewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_dataset_still_works_for_an_unscoped_dataset(client: TestClient) -> None:
    resp = client.post(DATASET_LIST_PATH, json={"name": "personal-dataset", "owner": "@test"})
    assert resp.status_code == 201, resp.text


def _create_project_dataset(client: TestClient, name: str = "p2-scoped-dataset") -> str:
    created = client.post(
        DATASET_LIST_PATH,
        json={"name": name, "owner": "@test"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["dataset_id"]


def test_update_dataset_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        DATASET_DETAIL_PATH.replace("{dataset_id}", dataset_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_dataset_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        DATASET_DETAIL_PATH.replace("{dataset_id}", dataset_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_create_example_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_example_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_example_from_trace_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        DATASET_FROM_TRACE_PATH.replace("{dataset_id}", dataset_id),
        # Explicit input/expected bypass the empty-capture guard, so the
        # request reaches the authorization check regardless of whether a
        # real MLflow trace "tr-1" exists.
        json={"trace_id": "tr-1", "input": {"q": "hi"}, "expected": {"a": "hi back"}},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_example_from_trace_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        DATASET_FROM_TRACE_PATH.replace("{dataset_id}", dataset_id),
        json={"trace_id": "tr-1", "input": {"q": "hi"}, "expected": {"a": "hi back"}},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_supersede_example_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    example = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert example.status_code == 201, example.text
    example_id = example.json()["data"]["example_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        DATASET_SUPERSEDE_PATH.replace("{dataset_id}", dataset_id).replace(
            "{example_id}", example_id
        ),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_supersede_example_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    example = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert example.status_code == 201, example.text
    example_id = example.json()["data"]["example_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        DATASET_SUPERSEDE_PATH.replace("{dataset_id}", dataset_id).replace(
            "{example_id}", example_id
        ),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_revise_example_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    example = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert example.status_code == 201, example.text
    example_id = example.json()["data"]["example_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        DATASET_REVISE_PATH.replace("{dataset_id}", dataset_id).replace("{example_id}", example_id),
        json={"input": {"q": "hi again"}},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_revise_example_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    example = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert example.status_code == 201, example.text
    example_id = example.json()["data"]["example_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        DATASET_REVISE_PATH.replace("{dataset_id}", dataset_id).replace("{example_id}", example_id),
        json={"input": {"q": "hi again"}},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_restore_dataset_version_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    # Bump the dataset to version 2 so there is a prior version (1) to restore to.
    bumped = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert bumped.status_code == 201, bumped.text
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        DATASET_RESTORE_PATH.replace("{dataset_id}", dataset_id),
        json={"version": 1},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_restore_dataset_version_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    dataset_id = _create_project_dataset(client)
    bumped = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", dataset_id),
        json={"input": {"q": "hi"}},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert bumped.status_code == 201, bumped.text
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        DATASET_RESTORE_PATH.replace("{dataset_id}", dataset_id),
        json={"version": 1},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# judges.py -- `resource.write.evidence`
# ---------------------------------------------------------------------------

_JUDGE_INSTRUCTIONS = "Score whether {{ outputs }} matches {{ expectations }}."


def test_create_judge_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        JUDGE_LIST_PATH,
        json={"name": "p2-wiring-judge", "instructions": _JUDGE_INSTRUCTIONS},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_judge_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        JUDGE_LIST_PATH,
        json={"name": "p2-wiring-judge", "instructions": _JUDGE_INSTRUCTIONS},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_judge_allows_a_project_reviewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@reviewer-user", ROLE_REVIEWER)
    _grant_operator(client, "@reviewer-user")

    resp = client.post(
        JUDGE_LIST_PATH,
        json={"name": "p2-wiring-judge", "instructions": _JUDGE_INSTRUCTIONS},
        headers={"X-CALIBER-User": "@reviewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_judge_still_works_for_an_unscoped_judge(client: TestClient) -> None:
    resp = client.post(
        JUDGE_LIST_PATH,
        json={"name": "personal-judge", "instructions": _JUDGE_INSTRUCTIONS},
    )
    assert resp.status_code == 201, resp.text


def test_update_judge_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        JUDGE_LIST_PATH,
        json={"name": "p2-update-judge", "instructions": _JUDGE_INSTRUCTIONS},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    judge_id = created.json()["data"]["judge_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        JUDGE_DETAIL_PATH.replace("{judge_id}", judge_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_judge_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    created = client.post(
        JUDGE_LIST_PATH,
        json={"name": "p2-update-judge", "instructions": _JUDGE_INSTRUCTIONS},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    judge_id = created.json()["data"]["judge_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.patch(
        JUDGE_DETAIL_PATH.replace("{judge_id}", judge_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# knowledge_bases.py -- `resource.write.runtime` (`P2-A`)
# ---------------------------------------------------------------------------

_KB_CREATE_BODY = {
    "name": "p2-wiring-kb",
    "description": "P2-A wiring fixture",
    "source_bucket": "p2-wiring-bucket",
    "sources": [{"kind": "folder", "path": "docs/"}],
    "chunking_strategy": "recursive",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
}


def test_create_knowledge_base_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        KB_LIST_PATH,
        json=_KB_CREATE_BODY,
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_knowledge_base_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    # The authorization check runs before any embedding/object-store work, so
    # a viewer is refused without ever needing a real bucket wired up.
    resp = client.post(
        KB_LIST_PATH,
        json=_KB_CREATE_BODY,
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_knowledge_base_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-p2-wiring-update",
            name="p2-wiring-update-kb",
            owner="@test",
            project_id=PROJECT_ID,
            visibility="project",
            status="active",
            source_bucket="p2-wiring-bucket",
        )
    )
    db_session.commit()
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        KB_DETAIL_PATH.replace("{knowledge_base_id}", "KB-p2-wiring-update"),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_knowledge_base_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-p2-wiring-update2",
            name="p2-wiring-update-kb2",
            owner="@test",
            project_id=PROJECT_ID,
            visibility="project",
            status="active",
            source_bucket="p2-wiring-bucket",
        )
    )
    db_session.commit()
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.patch(
        KB_DETAIL_PATH.replace("{knowledge_base_id}", "KB-p2-wiring-update2"),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_delete_knowledge_base_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-p2-wiring-delete",
            name="p2-wiring-delete-kb",
            owner="@test",
            project_id=PROJECT_ID,
            visibility="project",
            status="active",
            source_bucket="p2-wiring-bucket",
        )
    )
    db_session.commit()
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.delete(
        KB_DETAIL_PATH.replace("{knowledge_base_id}", "KB-p2-wiring-delete"),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_delete_knowledge_base_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-p2-wiring-delete2",
            name="p2-wiring-delete-kb2",
            owner="@test",
            project_id=PROJECT_ID,
            visibility="project",
            status="active",
            source_bucket="p2-wiring-bucket",
        )
    )
    db_session.commit()
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.delete(
        KB_DETAIL_PATH.replace("{knowledge_base_id}", "KB-p2-wiring-delete2"),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# openapi_integrations.py -- `resource.write.runtime` (`P2-A`)
# ---------------------------------------------------------------------------


def test_create_openapi_integration_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-wiring-openapi"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_openapi_integration_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-wiring-openapi"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_openapi_integration_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-wiring-openapi"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_openapi_integration_still_works_for_an_unscoped_integration(
    client: TestClient,
) -> None:
    resp = client.post(OPENAPI_LIST_PATH, json={"name": "personal-openapi"})
    assert resp.status_code == 201, resp.text


def test_update_openapi_integration_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    created = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-update-openapi"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["data"]["integration_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        OPENAPI_DETAIL_PATH.replace("{integration_id}", integration_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_openapi_integration_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    created = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-update-openapi2"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["data"]["integration_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.patch(
        OPENAPI_DETAIL_PATH.replace("{integration_id}", integration_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_archive_openapi_integration_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    created = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-archive-openapi"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["data"]["integration_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        OPENAPI_ARCHIVE_PATH.replace("{integration_id}", integration_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_archive_openapi_integration_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    created = client.post(
        OPENAPI_LIST_PATH,
        json={"name": "p2-archive-openapi2"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["data"]["integration_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        OPENAPI_ARCHIVE_PATH.replace("{integration_id}", integration_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# llm_pricing.py -- `resource.write.runtime` (`P2-A`)
# ---------------------------------------------------------------------------

_PRICING_BODY = {
    "provider": "p2-wiring-provider",
    "model_id": "p2-wiring-model",
    "prompt_price": 0.001,
    "completion_price": 0.002,
}


def test_create_pricing_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        PRICING_LIST_PATH,
        json=_PRICING_BODY,
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_pricing_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        PRICING_LIST_PATH,
        json=_PRICING_BODY,
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_pricing_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        PRICING_LIST_PATH,
        json=_PRICING_BODY,
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_pricing_still_works_for_an_unscoped_pricing_row(client: TestClient) -> None:
    resp = client.post(
        PRICING_LIST_PATH,
        json={
            "provider": "personal-provider",
            "model_id": "personal-model",
            "prompt_price": 0.001,
            "completion_price": 0.002,
        },
    )
    assert resp.status_code == 201, resp.text


def test_update_pricing_denies_a_project_viewer_with_admin_scope(
    client: TestClient, db_session: Session
) -> None:
    """`update_pricing` is `SCOPE_ADMIN`-only, but `caliber.admin` is *not*
    an implicit project role (`resource_access.py::project_role`'s
    documented `P1-B` removal) -- `routes/skills.py::update_skill`, itself
    `SCOPE_ADMIN`-only, already establishes that an admin-gated mutation on
    a project-scoped resource still needs its own `resource.write.runtime`
    check. Grant `caliber.admin` here (the route's own scope floor) so the
    request reaches the project-role check under test rather than being
    refused earlier by the global-scope gate.
    """
    _seed_project(db_session)
    created = client.post(
        PRICING_LIST_PATH, json=_PRICING_BODY, headers={"X-CALIBER-Project": PROJECT_ID}
    )
    assert created.status_code == 201, created.text
    pricing_id = created.json()["data"]["pricing_id"]
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        PRICING_DETAIL_PATH.replace("{pricing_id}", pricing_id),
        json={"prompt_price": 0.005},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_pricing_allows_a_project_editor_with_admin_scope(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    created = client.post(
        PRICING_LIST_PATH, json=_PRICING_BODY, headers={"X-CALIBER-Project": PROJECT_ID}
    )
    assert created.status_code == 201, created.text
    pricing_id = created.json()["data"]["pricing_id"]
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        PRICING_DETAIL_PATH.replace("{pricing_id}", pricing_id),
        json={"prompt_price": 0.005},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_update_pricing_still_works_for_an_unscoped_pricing_row(
    client: TestClient,
) -> None:
    created = client.post(
        PRICING_LIST_PATH,
        json={
            "provider": "personal-provider-2",
            "model_id": "personal-model-2",
            "prompt_price": 0.001,
            "completion_price": 0.002,
        },
    )
    assert created.status_code == 201, created.text
    pricing_id = created.json()["data"]["pricing_id"]

    resp = client.patch(
        PRICING_DETAIL_PATH.replace("{pricing_id}", pricing_id),
        json={"prompt_price": 0.005},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# mcp_servers.py -- `resource.write.runtime` / `resource.execute` (`P2-A`)
# ---------------------------------------------------------------------------
#
# `docs/workspace-plan.md`'s `P2-A` row named this file's entire admin-only
# create/update/delete/test-connection/discover-tools/invoke-tool/update-
# tool-policy surface as a deliberately-deferred gap: `CaliberMcpServer`
# already carries `project_id`/`visibility`/owner (it *is* a
# `db/resource_inventory.py::SCOPING_VISIBILITY` model, and its list/detail/
# history routes already resolve through `apply_visibility_filter`/
# `get_visible`), but every one of these seven routes previously checked
# only the global `caliber.admin` scope, with no project-role check on the
# resource's own project at all -- the identical `update_skill`/
# `update_pricing` shape: `caliber.admin` is not an implicit project role
# (`resource_access.py::project_role`'s documented `P1-B` removal), so a
# project viewer (visible via membership, not by role) with `caliber.admin`
# could still create/update/delete a server, or trigger a live
# test-connection/discover-tools/invoke-tool/tool-policy action against one,
# in any project they merely belong to.
#
# `create_mcp_server`/`update_mcp_server`/`delete_mcp_server`/
# `update_tool_policy` use `resource.write.runtime` -- persisted-config
# writes on the registry row itself, the same action `update_pricing`/
# `update_knowledge_base` already use. `test_connection`/`discover_tools`/
# `invoke_tool` use `resource.execute` instead -- each reaches out to the
# server's live configured transport (a real outbound call, cached
# connection-state side effects aside), the same "operate on a live
# resource" shape `resource.execute` already covers for workflow runs/
# evaluations/Aria plan execution.
#
# `save_mcp_tool_test_cases`/`calibrate_mcp_tool` (the `.../tools/{tool}/
# test-cases` and `.../calibrate` routes) are deliberately left open here,
# matching this same row's "child-mutation routes ... deliberately scoped
# out to keep this slice to 'root routes'" precedent for
# `knowledge_bases.py`/`openapi_integrations.py`'s own child-mutation
# routes -- they are `SCOPE_OPERATOR`-gated tool sub-resources nested two
# levels under the server root, not part of the named admin-only surface.

_MCP_CREATE_BODY = {
    "name": "p2-wiring-mcp",
    "transport": "stdio",
    "command": "npx mcp-server",
    "discovered_tools": [{"name": "search", "description": "Search"}],
}


def _mock_mcp_gateway_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal success-mocking gateway, matching `test_mcp_servers.py`'s
    own `_mock_mcp_gateway` fixture -- the allow-path assertions below reach
    a real `discover_tools_via_gateway`/`invoke_tool_via_gateway` call once
    the project-role check under test lets the request through, and must
    not attempt a real subprocess/network connection."""

    async def _discover(server: object, *, timeout_seconds: float = 20.0) -> list[dict[str, Any]]:
        return [{"name": "search", "description": "Search"}]

    async def _invoke(
        server: object, *, tool_name: str, arguments: dict[str, Any], timeout_seconds: float = 45.0
    ) -> Any:
        return {"ok": True}

    monkeypatch.setattr(mcp_routes, "discover_tools_via_gateway", _discover)
    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _invoke)


def _create_project_mcp_server(client: TestClient, name: str = "p2-scoped-mcp") -> str:
    created = client.post(
        MCP_LIST_PATH,
        json={**_MCP_CREATE_BODY, "name": name},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["server_id"]


def test_create_mcp_server_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        MCP_LIST_PATH,
        json=_MCP_CREATE_BODY,
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_mcp_server_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        MCP_LIST_PATH,
        json=_MCP_CREATE_BODY,
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_mcp_server_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        MCP_LIST_PATH,
        json=_MCP_CREATE_BODY,
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_create_mcp_server_still_works_for_an_unscoped_server(client: TestClient) -> None:
    resp = client.post(MCP_LIST_PATH, json={**_MCP_CREATE_BODY, "name": "personal-mcp"})
    assert resp.status_code == 201, resp.text


def test_update_mcp_server_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-update-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        MCP_DETAIL_PATH.replace("{server_id}", server_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_mcp_server_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-update-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        MCP_DETAIL_PATH.replace("{server_id}", server_id),
        json={"description": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_delete_mcp_server_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-delete-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.delete(
        MCP_DETAIL_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_delete_mcp_server_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-delete-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.delete(
        MCP_DETAIL_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 204, resp.text


def test_test_connection_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-test-conn-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        MCP_TEST_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_test_connection_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-test-conn-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        MCP_TEST_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["success"] is True


def test_discover_tools_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-discover-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        MCP_DISCOVER_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_discover_tools_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-discover-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        MCP_DISCOVER_PATH.replace("{server_id}", server_id),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_invoke_tool_denies_a_project_viewer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-invoke-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.post(
        MCP_INVOKE_PATH.replace("{server_id}", server_id),
        json={"tool_name": "search", "arguments": {}},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_invoke_tool_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_mcp_gateway_ok(monkeypatch)
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-invoke-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.post(
        MCP_INVOKE_PATH.replace("{server_id}", server_id),
        json={"tool_name": "search", "arguments": {}},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_update_tool_policy_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-policy-mcp")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.patch(
        MCP_TOOL_POLICY_PATH.replace("{server_id}", server_id).replace("{tool_name}", "search"),
        json={"allowed": True, "side_effect_level": "read", "requires_approval": False},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_tool_policy_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    server_id = _create_project_mcp_server(client, name="p2-policy-mcp2")
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.patch(
        MCP_TOOL_POLICY_PATH.replace("{server_id}", server_id).replace("{tool_name}", "search"),
        json={"allowed": True, "side_effect_level": "read", "requires_approval": False},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


def test_update_mcp_server_still_works_for_an_unscoped_server(client: TestClient) -> None:
    created = client.post(MCP_LIST_PATH, json={**_MCP_CREATE_BODY, "name": "personal-mcp-2"})
    assert created.status_code == 201, created.text
    server_id = created.json()["data"]["server_id"]

    resp = client.patch(
        MCP_DETAIL_PATH.replace("{server_id}", server_id),
        json={"description": "edited"},
    )
    assert resp.status_code == 200, resp.text
