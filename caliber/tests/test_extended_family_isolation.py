"""Project isolation and role enforcement for the extended families (alpha gap P1-24).

The two-project probes that found P0-1 covered prompts, jobs, test sets, judges,
evaluations, projects and members. Skills, tools, workflows and knowledge bases were not
probed. This file sweeps **every mutating route** of those families from four callers and
asserts what the project/role promise says each may do:

* an **outsider**: an operator who is not a member of the project, even when the request
  names the project in ``X-CALIBER-Project``. Every route answers 404, so a forbidden id is
  indistinguishable from a missing one (403 only where a global admin scope is demanded
  first);
* a **viewer** member: read-only, so every mutating route answers 403 naming the role;
* a **reviewer** member: may record evidence, run, calibrate and decide approvals, but not
  author or publish;
* an **editor** member: the positive control. Every route that is theirs by role gets
  through the gate (the outcome may still be a 400 or 409 from the route's own checks,
  but never a role refusal and never a visibility 404).

The expected outcome for each caller is derived from ``PROJECT_ACTIONS``, not restated, so
the table below only has to name the project action each route is meant to require.

Before this file, a *viewer* could publish a workflow version, move a production alias,
publish a workflow as a service and mint a bearer token for it, run a tool, replace its
fixtures, record a skill baseline and queue a calibration. Only the workflow, run and
knowledge-base roots checked a project role.

Deliberately out of the sweep, with the reason:

* ``/gate-verdicts`` keys a verdict by a free-form ``version_key`` that names no artifact
  (the prompt promote stores ``str(version)``), so no project can be derived from it. See
  the P2 backlog in ``docs/caliber-alpha-gaps.md``.
* ``/mcp-servers``: not one of the four extended families; admin-only and already
  visibility- and role-checked.
* Read-shaped POSTs (``test-render``, ``test-selection``, ``validate``, import preview):
  they persist nothing, so a viewer may use them.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.candidate_digest import candidate_digest
from caliber.db.models import (
    CaliberCalibrationJob,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberProjectMember,
    CaliberSkillTestRun,
    CaliberWorkflowPromotion,
)
from caliber.resource_access import (
    ACTION_APPROVE,
    ACTION_EXECUTE,
    ACTION_PUBLISH,
    ACTION_WRITE_EVIDENCE,
    ACTION_WRITE_RUNTIME,
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
)
from tests.workflow_helpers import (
    make_manifest,
    make_support_manifest,
    make_tool_payload,
    relax_release_graded_executor,
    relax_release_quality_gate,
    relax_tool_isolation_gate,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"

OWNER = "@test"  # the suite's default admin user; creates the world
OUTSIDER = "@outsider-ext"
VIEWER = "@viewer-ext"
REVIEWER = "@reviewer-ext"
EDITOR = "@editor-ext"
#: In the suite's admin list (``conftest._PERMISSIVE_TEST_USERS``) but not a member of the project.
NON_MEMBER_ADMIN = "@admin"
_MEMBER_ROLES = {VIEWER: ROLE_VIEWER, REVIEWER: ROLE_REVIEWER, EDITOR: ROLE_EDITOR}

#: Substring of every project-role refusal (``resource_access.require_project_access``).
_ROLE_REFUSAL = "cannot perform"


@dataclass(frozen=True)
class Route:
    """One mutating route and the project action it is meant to require."""

    family: str
    method: str
    path: str
    action: str
    body: dict[str, Any] | None = None
    params: dict[str, str] | None = None
    #: ``caliber.admin`` is demanded before the project is looked at, so a non-admin
    #: caller answers 403 from the scope gate rather than 404 or a role refusal.
    admin_scope: bool = False
    #: Marks a route that deletes or archives what later routes use; it runs last.
    destructive: bool = False
    #: Multipart upload: ``(field, filename, bytes, content_type)``.
    upload: tuple[str, str, bytes, str] | None = None
    #: Extra header or state notes for failure messages.
    note: str = field(default="", compare=False)


def _skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "probe-skill/SKILL.md",
            "---\nname: probe-skill\ndescription: A skill used to probe isolation\n---\n"
            "Do the thing for {{topic}}.\n",
        )
    return buffer.getvalue()


@dataclass
class World:
    project_id: str
    ids: dict[str, str]
    db_session: Session


def _build_world(  # noqa: PLR0915 - one linear fixture: it seeds one of everything
    client: TestClient, db_session: Session
) -> World:
    """A project that owns one of everything, created through the API as its owner."""
    owner = {"X-CALIBER-User": OWNER}
    project = client.post(f"{PREFIX}/projects", json={"name": "Extended Isolation"}, headers=owner)
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]
    headers = {**owner, "X-CALIBER-Project": project_id}
    ids: dict[str, str] = {"alias": "dev", "prod": "prod"}

    def post(path: str, body: dict[str, Any], expect: int = 201) -> dict[str, Any]:
        response = client.post(f"{PREFIX}{path}", json=body, headers=headers)
        assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
        return response.json()["data"]  # type: ignore[no-any-return]

    # Skill and a stored test run.
    ids["skill_id"] = post(
        "/skills",
        {
            "name": "iso-skill",
            "description": "d",
            "summary": "s",
            "content": "Do {{x}}.",
            "owner": OWNER,
        },
    )["skill_id"]
    # A second content version, so a rollback has something to restore and gets past its
    # own "no earlier version" check to the project gate.
    changed = client.patch(
        f"{PREFIX}/skills/{ids['skill_id']}", json={"content": "Do {{x}} again."}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    db_session.add(CaliberSkillTestRun(test_run_id="STR-ISO", skill_id=ids["skill_id"]))
    ids["skill_test_run_id"] = "STR-ISO"

    # Tools: the support manifest needs the three demo tools. The sweep addresses the last.
    for name in ("lookup_policy", "get_order", "escalate"):
        side_effect = "external_action" if name == "escalate" else "read"
        ids["tool_id"] = post(
            "/tools", make_tool_payload(name, allow_in_preview=True, side_effect_level=side_effect)
        )["tool_id"]
    db_session.add(
        CaliberCalibrationJob(
            job_id="CAL-ISO",
            tool_id=ids["tool_id"],
            status="running",
            created_at=_naive_now(),
        )
    )
    ids["tool_calibration_job_id"] = "CAL-ISO"

    # Workflow: a draft, a published version live on prod, a run, a service with a token.
    ids["workflow_id"] = post("/workflows", {"name": "iso-workflow", "owner": OWNER})["workflow_id"]
    wid = ids["workflow_id"]
    ids["version_id"] = post(
        f"/workflows/{wid}/versions", {"manifest": make_support_manifest(wid)}
    )["version_id"]
    published = client.post(
        f"{PREFIX}/workflow-versions/{ids['version_id']}/publish", headers=headers
    )
    assert published.status_code == 200, published.text
    relax_release_quality_gate(client)
    relax_release_graded_executor(client)
    relax_tool_isolation_gate(client)
    promoted = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/prod/promote",
        json={"version_id": ids["version_id"]},
        headers=headers,
    )
    assert promoted.status_code == 200, promoted.text
    # The draft that the authoring routes edit. A fresh draft each time the manifest hash
    # is needed would be heavier; a second version is enough for compile/patch/edit.
    ids["draft_version_id"] = post(
        f"/workflows/{wid}/versions", {"manifest": make_support_manifest(wid)}
    )["version_id"]
    run = client.post(
        f"{PREFIX}/workflow-versions/{ids['version_id']}/run",
        json={"input": {"message": "hi"}},
        headers=headers,
    )
    assert run.status_code == 200, run.text
    ids["run_id"] = run.json()["data"]["workflow_run_id"]
    _park_run_waiting_for_event(db_session, ids["run_id"], "iso.event")
    post(f"/workflows/{wid}/service", {}, expect=200)
    ids["token_id"] = post(f"/workflows/{wid}/service/tokens", {"name": "iso-token"})["token_id"]
    ids["report_id"] = post("/workflow-benchmark-reports", {"name": "iso-report", "worksheet": {}})[
        "report_id"
    ]

    # A workflow whose Start accepts an event trigger, live on prod.
    trigger_manifest = make_manifest("trigger_wf")
    trigger_manifest["nodes"]["start"]["trigger"] = {"mode": "event", "event_name": "iso.event"}
    ids["trigger_workflow_id"] = post("/workflows", {"name": "iso-trigger", "owner": OWNER})[
        "workflow_id"
    ]
    tid = ids["trigger_workflow_id"]
    trigger_version = post(f"/workflows/{tid}/versions", {"manifest": trigger_manifest})[
        "version_id"
    ]
    client.post(f"{PREFIX}/workflow-versions/{trigger_version}/publish", headers=headers)
    client.post(
        f"{PREFIX}/workflows/{tid}/deployments/prod/promote",
        json={"version_id": trigger_version},
        headers=headers,
    )

    # A pending production promotion and a knowledge base with one version, seeded directly:
    # the API paths to both need release policy and an object store this test does not own.
    db_session.add(
        CaliberWorkflowPromotion(
            promotion_id="PRM-ISO",
            workflow_id=wid,
            alias="prod",
            version_id=ids["version_id"],
        )
    )
    ids["promotion_id"] = "PRM-ISO"
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-ISO",
            name="iso-kb",
            owner=OWNER,
            source_bucket="iso-bucket",
            project_id=project_id,
            visibility="project",
        )
    )
    db_session.flush()
    db_session.add(
        CaliberKnowledgeBaseVersion(
            knowledge_base_version_id="KBV-ISO",
            knowledge_base_id="KB-ISO",
            version_number=1,
            chunking_strategy="recursive",
            embedding_model="BAAI/bge-m3",
            output_bucket="iso-bucket",
            output_prefix="iso/",
        )
    )
    ids["knowledge_base_id"] = "KB-ISO"
    ids["knowledge_base_version_id"] = "KBV-ISO"
    db_session.commit()

    # Who may do what. The members are real project members; the outsider is only an
    # operator. Everyone holds the approver scope so that the approval routes reach the
    # project-role gate instead of stopping at the scope gate: what separates them is the
    # project role (or, for the outsider, membership).
    config = client.app.state.config
    operators = ",".join([OUTSIDER, VIEWER, REVIEWER, EDITOR])
    client.app.state.config = config.model_copy(
        update={
            "operator_users": operators,
            "approver_users": operators,
            "workflow_run_queue_enabled": True,
            "workflow_run_checkpointing_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
        }
    )
    for user, role in _MEMBER_ROLES.items():
        db_session.add(
            CaliberProjectMember(
                member_id=f"M-{user.lstrip('@')}",
                project_id=project_id,
                user_id=user,
                role=role,
                created_by=OWNER,
            )
        )
    db_session.commit()
    return World(project_id=project_id, ids=ids, db_session=db_session)


def _park_run_waiting_for_event(db_session: Session, run_id: str, event_name: str) -> None:
    """Leave a run waiting for ``event_name`` so ``resume-by-event`` has a run to match.

    The role check on that route runs on the matched run, so a refusal can only be seen
    when one exists.
    """
    from caliber.db.models import (
        CaliberWorkflowRun,
        CaliberWorkflowRunCheckpoint,
    )
    from caliber.ids import new_workflow_run_checkpoint_id

    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    checkpoint_id = new_workflow_run_checkpoint_id()
    run.status = "waiting_event"
    run.current_node_id = "wait_gate"
    run.summary = {**dict(run.summary or {}), "resume_checkpoint_id": checkpoint_id}
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            project_id=run.project_id,
            sequence=1,
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "output": "resume output",
                "input_by_port": {"input": "resume output"},
                "expected_event_name": event_name,
            },
        )
    )
    db_session.commit()


def _naive_now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _routes(ids: dict[str, str]) -> list[Route]:
    """Every mutating route of the four families, with the action it must require."""
    wid = ids["workflow_id"]
    support = make_support_manifest(wid)
    kb_source = [{"kind": "folder", "path": "docs/"}]
    skill_run = {
        "skill_id": ids["skill_id"],
        "kind": "scenario",
        "skill_version": 1,
        "results": [
            {
                "name": "case-1",
                "input": {"user_message": "help"},
                "output": {"selected": True},
                "verdict": "pass",
                "score": 1.0,
                "duration_ms": 1.0,
            }
        ],
    }
    tool_run = {
        "tool_id": ids["tool_id"],
        "kind": "suite",
        "tool_version": "1.0",
        "results": [
            {
                "name": "case-1",
                "input": {},
                "output": {},
                "verdict": "pass",
                "score": 1.0,
                "duration_ms": 1.0,
                "reasoning": "ok",
            }
        ],
    }
    calibration_spec = {"agent_id": "iso-agent"}
    skill_file = {
        "owner": OWNER,
        "files": [
            {
                "path": "SKILL.md",
                "content": "---\nname: probe-import\ndescription: d\n---\nBody {{x}}",
            }
        ],
    }
    return [
        # ------------------------------------------------------------------ skills
        Route(
            "skill",
            "POST",
            "/skills",
            ACTION_WRITE_RUNTIME,
            {"name": "iso-new-skill", "content": "c", "owner": OWNER},
        ),
        Route("skill", "POST", "/skills/import-package", ACTION_WRITE_RUNTIME, skill_file),
        Route(
            "skill",
            "POST",
            "/skills/import-package.zip",
            ACTION_WRITE_RUNTIME,
            upload=("file", "probe-skill.zip", _skill_zip(), "application/zip"),
        ),
        Route("skill", "POST", "/skills/test-runs", ACTION_WRITE_EVIDENCE, skill_run),
        Route(
            "skill",
            "POST",
            "/skills/{skill_id}/baseline",
            ACTION_WRITE_EVIDENCE,
            {"test_run_id": ids["skill_test_run_id"]},
        ),
        Route(
            "skill", "POST", "/skills/{skill_id}/bind", ACTION_WRITE_RUNTIME, {"kind": "standalone"}
        ),
        Route("skill", "POST", "/skills/{skill_id}/calibrate", ACTION_EXECUTE, {}),
        Route(
            "skill",
            "PATCH",
            "/skills/{skill_id}",
            ACTION_WRITE_RUNTIME,
            {"description": "changed"},
            admin_scope=True,
        ),
        Route(
            "skill",
            "POST",
            "/skills/{skill_id}/rollback",
            ACTION_WRITE_RUNTIME,
            {},
            admin_scope=True,
        ),
        # ------------------------------------------------------------------- tools
        Route(
            "tool",
            "POST",
            "/tools",
            ACTION_WRITE_RUNTIME,
            make_tool_payload("iso_new_tool"),
            admin_scope=True,
        ),
        Route(
            "tool",
            "PATCH",
            "/tools/{tool_id}",
            ACTION_WRITE_RUNTIME,
            {"description": "changed"},
            admin_scope=True,
        ),
        Route(
            "tool",
            "POST",
            "/tools/{tool_id}/archive",
            ACTION_WRITE_RUNTIME,
            {},
            admin_scope=True,
            destructive=True,
        ),
        Route("tool", "POST", "/tools/test-runs", ACTION_WRITE_EVIDENCE, tool_run),
        Route("tool", "POST", "/tools/{tool_id}/test-run", ACTION_EXECUTE, {"input": {}}),
        Route(
            "tool", "PUT", "/tools/{tool_id}/test-cases", ACTION_WRITE_RUNTIME, {"test_cases": []}
        ),
        Route(
            "tool",
            "POST",
            "/tools/{tool_id}/baseline",
            ACTION_WRITE_EVIDENCE,
            {"test_run_id": "TTR-none"},
        ),
        Route("tool", "POST", "/tools/{tool_id}/calibrate", ACTION_EXECUTE, {}),
        Route("tool", "POST", "/tools/{tool_id}/calibration-jobs", ACTION_EXECUTE, {}),
        Route(
            "tool",
            "POST",
            "/tools/{tool_id}/calibration-jobs/{tool_calibration_job_id}/resolve",
            ACTION_EXECUTE,
            {"action": "abandon", "reason": "isolation sweep"},
        ),
        # --------------------------------------------------------------- workflows
        Route(
            "workflow",
            "POST",
            "/workflows",
            ACTION_WRITE_RUNTIME,
            {"name": "iso-new-workflow", "owner": OWNER},
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/import",
            ACTION_WRITE_RUNTIME,
            {"manifest": make_support_manifest("imported_wf")},
        ),
        Route(
            "workflow",
            "PATCH",
            "/workflows/{workflow_id}",
            ACTION_WRITE_RUNTIME,
            {"description": "changed"},
        ),
        Route(
            "workflow",
            "DELETE",
            "/workflows/{workflow_id}/session-memory",
            ACTION_WRITE_RUNTIME,
            params={"session_id": "s1"},
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/{workflow_id}/versions",
            ACTION_WRITE_RUNTIME,
            {"manifest": support},
        ),
        Route(
            "workflow",
            "PATCH",
            "/workflow-versions/{draft_version_id}",
            ACTION_WRITE_RUNTIME,
            {"manifest_hash": "stale", "manifest": support},
        ),
        Route(
            "workflow", "POST", "/workflow-versions/{version_id}/restore", ACTION_WRITE_RUNTIME, {}
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{draft_version_id}/compile",
            ACTION_WRITE_RUNTIME,
            {},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{draft_version_id}/propose-patch",
            ACTION_WRITE_RUNTIME,
            {},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{draft_version_id}/copilot-edit",
            ACTION_WRITE_RUNTIME,
            {"instruction": "rename the agent"},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{draft_version_id}/plan-build",
            ACTION_WRITE_RUNTIME,
            {"goal": "a support agent"},
        ),
        Route(
            "workflow", "POST", "/workflow-versions/{draft_version_id}/publish", ACTION_PUBLISH, {}
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{version_id}/preview-run",
            ACTION_EXECUTE,
            {"input": "hi"},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-versions/{version_id}/run",
            ACTION_EXECUTE,
            {"input": {"message": "hi"}},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-runs",
            ACTION_EXECUTE,
            {"workflow_version_id": ids["version_id"], "input": {"message": "hi"}},
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/{trigger_workflow_id}/trigger",
            ACTION_EXECUTE,
            {"input": {"message": "hi"}},
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-runs/resume-by-event",
            ACTION_EXECUTE,
            {"event_name": "iso.event", "workflow_id": wid},
        ),
        Route("workflow", "POST", "/workflow-runs/{run_id}/cancel", ACTION_EXECUTE, {}),
        Route("workflow", "POST", "/workflow-runs/{run_id}/retry", ACTION_EXECUTE, {}),
        Route("workflow", "POST", "/workflow-runs/{run_id}/resume", ACTION_EXECUTE, {}),
        Route("workflow", "POST", "/workflow-runs/{run_id}/approval/approve", ACTION_APPROVE, {}),
        Route("workflow", "POST", "/workflow-runs/{run_id}/approval/reject", ACTION_APPROVE, {}),
        Route(
            "workflow",
            "POST",
            "/workflow-runs/{run_id}/files",
            ACTION_EXECUTE,
            upload=("file", "in.txt", b"hello", "text/plain"),
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-runs/{run_id}/artifacts",
            ACTION_EXECUTE,
            {"file_id": "FILE-none"},
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/{workflow_id}/calibration/runs",
            ACTION_EXECUTE,
            calibration_spec,
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/{workflow_id}/deployments/{alias}/promote",
            ACTION_PUBLISH,
            {"version_id": ids["version_id"]},
        ),
        Route(
            "workflow",
            "POST",
            "/workflows/{workflow_id}/deployments/{alias}/rollback",
            ACTION_PUBLISH,
            {},
        ),
        Route(
            "workflow", "POST", "/workflow-promotions/{promotion_id}/approve", ACTION_APPROVE, {}
        ),
        Route("workflow", "POST", "/workflow-promotions/{promotion_id}/reject", ACTION_APPROVE, {}),
        Route("workflow", "POST", "/workflows/{workflow_id}/service", ACTION_PUBLISH, {}),
        Route(
            "workflow",
            "POST",
            "/workflows/{workflow_id}/service/tokens",
            ACTION_PUBLISH,
            {"name": "second"},
        ),
        Route(
            "workflow",
            "DELETE",
            "/workflows/{workflow_id}/service/tokens/{token_id}",
            ACTION_PUBLISH,
            destructive=True,
        ),
        Route(
            "workflow",
            "DELETE",
            "/workflows/{workflow_id}/service",
            ACTION_PUBLISH,
            destructive=True,
        ),
        Route(
            "workflow",
            "POST",
            "/workflow-benchmark-reports",
            ACTION_WRITE_EVIDENCE,
            {"name": "iso-report-2", "worksheet": {}},
        ),
        Route(
            "workflow",
            "PATCH",
            "/workflow-benchmark-reports/{report_id}",
            ACTION_WRITE_EVIDENCE,
            {"name": "renamed"},
        ),
        Route(
            "workflow",
            "DELETE",
            "/workflow-benchmark-reports/{report_id}",
            ACTION_WRITE_EVIDENCE,
            destructive=True,
        ),
        Route(
            "workflow", "DELETE", "/workflows/{workflow_id}", ACTION_WRITE_RUNTIME, destructive=True
        ),
        # ---------------------------------------------------------- knowledge bases
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases",
            ACTION_WRITE_RUNTIME,
            {
                "name": "iso-new-kb",
                "source_bucket": "iso-bucket",
                "sources": kb_source,
                "chunking_strategy": "recursive",
                "embedding_model": "BAAI/bge-m3",
            },
        ),
        Route(
            "knowledge",
            "PATCH",
            "/knowledge-bases/{knowledge_base_id}",
            ACTION_WRITE_RUNTIME,
            {"description": "changed"},
        ),
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases/{knowledge_base_id}/versions",
            ACTION_WRITE_RUNTIME,
            {"chunking_strategy": "recursive", "embedding_model": "BAAI/bge-m3"},
        ),
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases/{knowledge_base_id}/versions/{knowledge_base_version_id}/activate",
            ACTION_WRITE_RUNTIME,
            {},
        ),
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases/{knowledge_base_id}/baseline",
            ACTION_WRITE_RUNTIME,
            {"test_run_id": "x"},
        ),
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases/{knowledge_base_id}/calibrate",
            ACTION_EXECUTE,
            {"version_id": ids["knowledge_base_version_id"], "eval_dataset_id": "x"},
        ),
        Route(
            "knowledge",
            "POST",
            "/knowledge-bases/{knowledge_base_id}/rollback",
            ACTION_WRITE_RUNTIME,
            {},
        ),
        Route(
            "knowledge",
            "DELETE",
            "/knowledge-bases/{knowledge_base_id}",
            ACTION_WRITE_RUNTIME,
            destructive=True,
        ),
    ]


def _call(client: TestClient, route: Route, ids: dict[str, str], headers: dict[str, str]) -> Any:
    path = re.sub(r"\{(\w+)\}", lambda match: ids[match.group(1)], route.path)
    url = f"{PREFIX}{path}"
    if route.upload is not None:
        field_name, filename, content, content_type = route.upload
        return client.request(
            route.method,
            url,
            files={field_name: (filename, content, content_type)},
            headers=headers,
        )
    return client.request(
        route.method,
        url,
        json=route.body if route.body is not None else {},
        params=route.params,
        headers=headers,
    )


def _ordered(routes: list[Route]) -> list[Route]:
    return [r for r in routes if not r.destructive] + [r for r in routes if r.destructive]


def _label(route: Route) -> str:
    return f"{route.method} {route.path}"


def _sweep(client: TestClient, world: World, user: str) -> list[tuple[Route, Any]]:
    headers = {"X-CALIBER-User": user, "X-CALIBER-Project": world.project_id}
    return [
        (route, _call(client, route, world.ids, headers)) for route in _ordered(_routes(world.ids))
    ]


# --------------------------------------------------------------------------- the table


def _shape(path: str) -> str:
    """A path with every parameter name erased, so ``{draft_version_id}`` equals ``{version_id}``.

    The table names its parameters after the seeded id it substitutes; the route table
    names them after the handler's own convention. Only the shape has to match.
    """
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_the_route_table_covers_every_mutating_route_of_the_four_families(
    client: TestClient, db_session: Session
) -> None:
    """A new mutating route in these families fails here until it is classified.

    The sweeps below only prove the routes they list. This keeps the list complete: any
    ``POST``/``PUT``/``PATCH``/``DELETE`` under the family prefixes must either be in the
    table or be named, with a reason, in ``_NOT_SWEPT``.
    """
    world = _build_world(client, db_session)
    listed = {(route.method, _shape(f"{PREFIX}{route.path}")) for route in _routes(world.ids)}
    exempt = {(method, _shape(path)) for method, path in _NOT_SWEPT}
    family = re.compile(r"/skills|/tools|/workflow|/knowledge")

    seen: set[tuple[str, str]] = set()

    def walk(routes: Any, base: str = "") -> None:
        for route in routes:
            path = base + getattr(route, "path", "")
            if getattr(route, "routes", None):
                walk(route.routes, path)
                continue
            for method in getattr(route, "methods", None) or []:
                if method in {"POST", "PUT", "PATCH", "DELETE"} and family.search(path):
                    seen.add((method, _shape(path)))

    walk(client.app.routes)
    unclassified = sorted(
        f"{method} {path.removeprefix(PREFIX)}"
        for method, path in seen
        if (method, path) not in listed and (method, path) not in exempt
    )
    assert not unclassified, (
        "mutating routes in the extended families that the isolation sweep does not cover; "
        "add each to _routes() with the project action it must require, or to _NOT_SWEPT "
        f"with a reason: {unclassified}"
    )
    stale = sorted(f"{m} {p.removeprefix(PREFIX)}" for m, p in listed - seen)
    assert not stale, f"the table lists routes that no longer exist: {stale}"


#: Mutating routes deliberately not swept, each with the reason. Keyed by (method, full path).
_NOT_SWEPT: dict[tuple[str, str], str] = {
    (m, f"{PREFIX}{p}"): why
    for m, p, why in (
        ("POST", "/workflows/import/preview", "persists nothing; a viewer may preview an import"),
        ("POST", "/skills/{skill_id}/test-render", "persists nothing; a viewer may render"),
        ("POST", "/skills/{skill_id}/test-selection", "persists nothing; a viewer may test"),
        ("POST", "/workflow-versions/{version_id}/validate", "persists nothing; read-shaped"),
        ("POST", "/knowledge/query", "a read: queries a version the caller can see"),
        (
            "POST",
            "/gate-verdicts/{artifact_type}/{version_key}",
            "free-form key names no artifact, so no project can be derived (P2 backlog)",
        ),
        ("POST", "/workflow-files", "project-level staging upload; its own require_project_access"),
        (
            "POST",
            "/knowledge-base-versions/{version_id}/age-sync",
            "optional Apache AGE sync; resolves its version through the KB visibility check",
        ),
        ("POST", "/mcp-servers", "not one of the four families; admin-only and role-checked"),
        ("PATCH", "/mcp-servers/{server_id}", "not one of the four families"),
        ("DELETE", "/mcp-servers/{server_id}", "not one of the four families"),
        ("POST", "/mcp-servers/{server_id}/test-connection", "not one of the four families"),
        ("POST", "/mcp-servers/{server_id}/discover-tools", "not one of the four families"),
        ("POST", "/mcp-servers/{server_id}/invoke-tool", "not one of the four families"),
        (
            "PATCH",
            "/mcp-servers/{server_id}/tools/{tool_name}/policy",
            "not one of the four families",
        ),
        (
            "PUT",
            "/mcp-servers/{server_id}/tools/{tool_name}/test-cases",
            "not one of the four families",
        ),
        (
            "POST",
            "/mcp-servers/{server_id}/tools/{tool_name}/calibrate",
            "not one of the four families",
        ),
    )
}


# --------------------------------------------------------------------------- outsider


def test_an_outsider_operator_gets_404_on_every_mutating_route(
    client: TestClient, db_session: Session
) -> None:
    """Visibility: naming the project in the header does not unlock anything."""
    world = _build_world(client, db_session)
    wrong: list[str] = []
    for route, response in _sweep(client, world, OUTSIDER):
        expected = 403 if route.admin_scope else 404
        if response.status_code != expected:
            wrong.append(
                f"{_label(route)}: expected {expected}, got {response.status_code} "
                f"{response.text[:120]!r}"
            )
    if wrong:
        pytest.fail(
            "an operator who is not a project member got through:\n" + "\n".join(wrong),
            pytrace=False,
        )


def test_an_outsider_refusal_does_not_reveal_that_the_resource_exists(
    client: TestClient, db_session: Session
) -> None:
    """The 404 for a real hidden id reads exactly like the 404 for a made-up one."""
    world = _build_world(client, db_session)
    headers = {"X-CALIBER-User": OUTSIDER, "X-CALIBER-Project": world.project_id}
    real = client.post(
        f"{PREFIX}/workflows/{world.ids['workflow_id']}/versions",
        json={"manifest": make_support_manifest(world.ids["workflow_id"])},
        headers=headers,
    )
    missing = client.post(
        f"{PREFIX}/workflows/WF-does-not-exist/versions",
        json={"manifest": make_support_manifest("WF-does-not-exist")},
        headers=headers,
    )
    assert real.status_code == missing.status_code == 404
    assert _ROLE_REFUSAL not in real.text


def test_an_outsider_causes_no_side_effect(client: TestClient, db_session: Session) -> None:
    """Refused means refused: the world is exactly as the owner left it."""
    world = _build_world(client, db_session)
    owner_headers = {"X-CALIBER-User": OWNER, "X-CALIBER-Project": world.project_id}
    wid = world.ids["workflow_id"]

    def snapshot() -> dict[str, Any]:
        return {
            "versions": [
                v["version_id"]
                for v in client.get(
                    f"{PREFIX}/workflows/{wid}/versions", headers=owner_headers
                ).json()["data"]
            ],
            "deployments": client.get(
                f"{PREFIX}/workflows/{wid}/deployments", headers=owner_headers
            ).json()["data"],
            "workflow": client.get(f"{PREFIX}/workflows/{wid}", headers=owner_headers).json()[
                "data"
            ],
        }

    before = snapshot()
    _sweep(client, world, OUTSIDER)
    assert snapshot() == before


# ------------------------------------------------------------------------ role matrix


def _violations(
    client: TestClient, world: World, user: str, role: str, *, skip: set[str] | None = None
) -> list[str]:
    wrong: list[str] = []
    for route, response in _sweep(client, world, user):
        if route.admin_scope or _label(route) in (skip or set()):
            continue
        allowed = role in PROJECT_ACTIONS[route.action]
        refused = response.status_code == 403 and _ROLE_REFUSAL in response.text
        if allowed and (refused or (response.status_code == 404 and "project" in response.text)):
            wrong.append(
                f"{_label(route)}: {role} holds {route.action} but was refused "
                f"{response.status_code} {response.text[:100]!r}"
            )
        if not allowed and not refused:
            wrong.append(
                f"{_label(route)}: {role} lacks {route.action} yet got "
                f"{response.status_code} {response.text[:100]!r}"
            )
    return wrong


def test_a_viewer_is_refused_every_mutating_route_by_role(
    client: TestClient, db_session: Session
) -> None:
    world = _build_world(client, db_session)
    wrong = _violations(client, world, VIEWER, ROLE_VIEWER)
    if wrong:
        pytest.fail("a read-only member could mutate:\n" + "\n".join(wrong), pytrace=False)


def test_a_reviewer_may_record_evidence_run_and_decide_but_not_author_or_publish(
    client: TestClient, db_session: Session
) -> None:
    world = _build_world(client, db_session)
    wrong = _violations(client, world, REVIEWER, ROLE_REVIEWER)
    if wrong:
        pytest.fail("the reviewer role boundary is wrong:\n" + "\n".join(wrong), pytrace=False)


def test_an_editor_gets_through_the_gate_of_every_route_their_role_allows(
    client: TestClient, db_session: Session
) -> None:
    """The positive control. An editor holds every action except approving.

    Without it a gate that refused everyone would pass the other three sweeps.
    """
    world = _build_world(client, db_session)
    wrong = _violations(client, world, EDITOR, ROLE_EDITOR)
    if wrong:
        pytest.fail("the editor role boundary is wrong:\n" + "\n".join(wrong), pytrace=False)
    # And the editor really did change things, so the sweep ran against live state.
    owner_headers = {"X-CALIBER-User": OWNER, "X-CALIBER-Project": world.project_id}
    published = client.get(
        f"{PREFIX}/workflow-versions/{world.ids['draft_version_id']}", headers=owner_headers
    )
    assert published.json()["data"]["status"] == "published"


def test_the_owner_role_holds_every_action_the_sweep_names() -> None:
    """The table's actions all exist in the registry, and the owner holds each one."""
    named = {route.action for route in _routes({k: k for k in _ID_KEYS})}
    assert named <= set(PROJECT_ACTIONS), named - set(PROJECT_ACTIONS)
    for action in named:
        assert ROLE_OWNER in PROJECT_ACTIONS[action], action
    # The role vocabulary the issue is about: viewers hold none of the mutating actions.
    for action in named:
        assert ROLE_VIEWER not in PROJECT_ACTIONS[action], action


_ID_KEYS = (
    "skill_id",
    "skill_test_run_id",
    "tool_id",
    "tool_calibration_job_id",
    "workflow_id",
    "version_id",
    "draft_version_id",
    "run_id",
    "token_id",
    "report_id",
    "trigger_workflow_id",
    "promotion_id",
    "knowledge_base_id",
    "knowledge_base_version_id",
    "alias",
)


@pytest.mark.parametrize(
    ("method", "path", "action"),
    [
        ("POST", "/workflow-versions/{draft_version_id}/publish", ACTION_PUBLISH),
        ("POST", "/workflows/{workflow_id}/deployments/{alias}/promote", ACTION_PUBLISH),
        ("POST", "/workflows/{workflow_id}/service", ACTION_PUBLISH),
        ("POST", "/workflows/{workflow_id}/service/tokens", ACTION_PUBLISH),
    ],
)
def test_the_four_routes_a_viewer_could_use_to_ship_a_workflow_name_their_action(
    method: str, path: str, action: str
) -> None:
    """The regression that motivated the file, pinned by name in the table."""
    routes = {(r.method, r.path): r.action for r in _routes({k: k for k in _ID_KEYS})}
    assert routes[(method, path)] == action


def test_a_non_member_global_admin_is_not_an_implicit_member(
    client: TestClient, db_session: Session
) -> None:
    """``caliber.admin`` sees every project's resources but holds no project role in it.

    The admin routes (change a skill, register or archive a tool, roll a skill back) demand
    the global scope first; once past it they must still honour the project, exactly as the
    docs promise ("not an implicit member").
    """
    world = _build_world(client, db_session)
    wrong: list[str] = []
    for route, response in _sweep(client, world, NON_MEMBER_ADMIN):
        refused = response.status_code in {403, 404}
        if not refused:
            wrong.append(
                f"{_label(route)}: a non-member admin got {response.status_code} "
                f"{response.text[:100]!r}"
            )
    if wrong:
        pytest.fail(
            "a global admin who is not a project member could mutate it:\n" + "\n".join(wrong),
            pytrace=False,
        )


def _ready_skill_job(client: TestClient, db_session: Session, world: World) -> str:
    """A skill calibration job sitting at ``candidate_ready``, filed under the skill's project."""
    from caliber.db.models import CaliberRefinementJob

    headers = {"X-CALIBER-User": OWNER, "X-CALIBER-Project": world.project_id}
    queued = client.post(
        f"{PREFIX}/skills/{world.ids['skill_id']}/calibrate", json={}, headers=headers
    )
    assert queued.status_code == 201, queued.text
    job_id = queued.json()["data"]["job"]["job_id"]
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    job.status = "candidate_ready"
    job.current_stage = "done"
    job.candidate = {
        "artifact_type": "skill",
        "content": "Do {{x}} again, but better.",
        "baseline_content": "Do {{x}} again.",
        "rationale": "isolation test",
    }
    job.candidate_digest = candidate_digest(job.candidate)
    job.eval_results = {
        "candidate": {"overall": 0.9},
        "baseline": {"overall": 0.8},
        "deltas": {"overall": 0.1},
        "gate": {"passed": True, "reasons": []},
        "n_examples": 0,
        "provider": "fake",
        "simulated": True,
    }
    db_session.commit()
    return job_id


def test_applying_a_skill_calibration_candidate_follows_the_skills_project(
    client: TestClient, db_session: Session
) -> None:
    """The acceptance for skill jobs: Apply is a release, and a release needs the role.

    A skill's candidate is applied through the shared ``/jobs/{id}/apply`` route, which
    resolves the job to the skill's hidden target and so to the skill's project. A
    non-member cannot see the job (404); a viewer or a reviewer can see it but holds no
    ``release.apply`` (403); an editor can. A refused caller changes nothing.
    """
    world = _build_world(client, db_session)
    job_id = _ready_skill_job(client, db_session, world)
    skill_id = world.ids["skill_id"]

    def content() -> str:
        owner_headers = {"X-CALIBER-User": OWNER, "X-CALIBER-Project": world.project_id}
        return str(
            client.get(f"{PREFIX}/skills/{skill_id}", headers=owner_headers).json()["data"][
                "content"
            ]
        )

    before = content()
    for user, expected in ((OUTSIDER, 404), (VIEWER, 403), (REVIEWER, 403)):
        response = client.post(
            f"{PREFIX}/jobs/{job_id}/apply",
            headers={"X-CALIBER-User": user, "X-CALIBER-Project": world.project_id},
        )
        assert response.status_code == expected, f"{user}: {response.status_code} {response.text}"
        assert content() == before, f"{user} changed the skill"

    applied = client.post(
        f"{PREFIX}/jobs/{job_id}/apply",
        headers={"X-CALIBER-User": EDITOR, "X-CALIBER-Project": world.project_id},
    )
    assert applied.status_code == 200, applied.text
    assert content() == "Do {{x}} again, but better."


def test_a_skill_calibration_job_is_invisible_to_an_outsider(
    client: TestClient, db_session: Session
) -> None:
    """Reading a skill's candidate, evidence and diagnosis is as private as applying it."""
    world = _build_world(client, db_session)
    job_id = _ready_skill_job(client, db_session, world)
    headers = {"X-CALIBER-User": OUTSIDER, "X-CALIBER-Project": world.project_id}

    assert client.get(f"{PREFIX}/jobs/{job_id}", headers=headers).status_code == 404
    listed = client.get(f"{PREFIX}/jobs", headers=headers).json()["data"]
    assert job_id not in {row["job_id"] for row in listed}
