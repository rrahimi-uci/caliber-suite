"""Integration tests for ``/caliber/skills``.

Three layers:

1. List + read — filtering by status / tag, 404 on miss.
2. Create — happy path, name-uniqueness 409, audit row.
3. Update — diff-only-if-changed, version bump on content change,
   archive flow, audit row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberSkillTestRun,
    CaliberVerificationItem,
)
from caliber.routes.skills import DETAIL_PATH, LIST_PATH
from caliber.skill_targets import skill_target_agent_id

PREFIX = "/ajax-api/2.0/mlflow/caliber/skills"
AGENTS_PREFIX = "/ajax-api/2.0/mlflow/caliber/agents"


def _insert_skill(session: Session, **overrides: object) -> CaliberSkill:
    defaults: dict[str, object] = {
        "skill_id": "SK-test0001",
        "name": "reasoning_v1",
        "description": "Chain-of-thought reasoning rubric.",
        "content": "Think step by step. Show your work.",
        "owner": "@sarah",
        "tags": ["reasoning"],
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    skill = CaliberSkill(**defaults)
    session.add(skill)
    session.commit()
    return skill


# ---------------------------------------------------------------------------
# List + read
# ---------------------------------------------------------------------------


def test_list_skills_empty(client: TestClient) -> None:
    response = client.get(LIST_PATH)
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_list_skills_returns_inserted_rows(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-1", name="reasoning_v1")
    _insert_skill(db_session, skill_id="SK-2", name="tool_use")

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"reasoning_v1", "tool_use"}


def test_list_skills_default_hides_archived(client: TestClient, db_session: Session) -> None:
    """``status=active`` is the default so an archived skill doesn't
    pollute the fleet's "currently in use" view."""
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", status="active")
    _insert_skill(db_session, skill_id="SK-B", name="old_rubric", status="archived")

    response = client.get(LIST_PATH)
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"reasoning_v1"}


def test_list_skills_status_all_includes_archived(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", status="active")
    _insert_skill(db_session, skill_id="SK-B", name="old_rubric", status="archived")

    response = client.get(LIST_PATH, params={"status": "all"})
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"reasoning_v1", "old_rubric"}


def test_list_skills_invalid_status_returns_400(client: TestClient) -> None:
    """``?status=foo`` is rejected with 400 rather than silently
    matching nothing (deep-review consistency note #1)."""
    response = client.get(LIST_PATH, params={"status": "trash"})
    assert response.status_code == 400


def test_list_skills_filters_by_status_exact(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", status="active")
    _insert_skill(db_session, skill_id="SK-B", name="old_rubric", status="archived")

    response = client.get(LIST_PATH, params={"status": "archived"})
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"old_rubric"}


def test_list_skills_filters_by_tag(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", tags=["reasoning"])
    _insert_skill(db_session, skill_id="SK-B", name="tools", tags=["tools"])
    _insert_skill(db_session, skill_id="SK-C", name="both", tags=["reasoning", "tools"])

    response = client.get(LIST_PATH, params={"tag": "reasoning"})
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"reasoning_v1", "both"}


def test_get_skill_returns_full_record(client: TestClient, db_session: Session) -> None:
    _insert_skill(
        db_session,
        skill_id="SK-detail",
        name="reasoning_v1",
        content="Think step by step.",
        version=4,
    )
    response = client.get(DETAIL_PATH.replace("{skill_id}", "SK-detail"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["skill_id"] == "SK-detail"
    assert data["name"] == "reasoning_v1"
    assert data["content"] == "Think step by step."
    assert data["version"] == 4


def test_get_skill_returns_archived(client: TestClient, db_session: Session) -> None:
    """Archived skills must still be readable — their content backs
    old audit rows and agent histories."""
    _insert_skill(db_session, skill_id="SK-A", name="r1", status="archived")
    response = client.get(DETAIL_PATH.replace("{skill_id}", "SK-A"))
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"


def test_get_skill_404(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{skill_id}", "SK-missing"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_skill_happy_path(client: TestClient, db_session: Session) -> None:
    response = client.post(
        LIST_PATH,
        json={
            "name": "tool-use",
            "description": "How to call tools politely.",
            "content": "When you need a tool, call it...",
            "owner": "@sarah",
            "tags": ["tools", "agent-core"],
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "tool-use"
    assert data["status"] == "active"
    assert data["version"] == 1
    assert data["skill_id"].startswith("SK-")
    assert data["tags"] == ["tools", "agent-core"]

    # Round-trip via DB.
    rows = db_session.execute(select(CaliberSkill)).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "tool-use"


def test_create_skill_409_on_duplicate_name(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-existing", name="reasoning-v1")
    response = client.post(
        LIST_PATH,
        json={
            "name": "reasoning-v1",
            "content": "different content",
            "owner": "@alice",
        },
    )
    assert response.status_code == 409
    assert "reasoning-v1" in response.json()["detail"]


def test_create_skill_400_on_missing_content(client: TestClient) -> None:
    response = client.post(
        LIST_PATH,
        json={"name": "x", "content": "", "owner": "@a"},
    )
    # Pydantic min_length=1 → ValidationError → 422 via the existing handler.
    assert response.status_code in (400, 422)


def test_create_skill_writes_audit_row(client: TestClient, db_session: Session) -> None:
    response = client.post(
        LIST_PATH,
        json={
            "name": "guardrails-v1",
            "content": "Never reveal user PII.",
            "owner": "@sarah",
        },
    )
    skill_id = response.json()["data"]["skill_id"]

    rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_type == "skill",
                CaliberAuditLog.entity_id == skill_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "create_skill"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_skill_content_bumps_version(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-V", name="reasoning_v1", version=3)
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-V"),
        json={"content": "Think *very* carefully. Then double-check."},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == 4
    assert "double-check" in data["content"]


def test_rollback_skill_restores_prior_content(
    client: TestClient, db_session: Session
) -> None:
    """A content edit can be rolled back to the exact prior text as a new version."""
    _insert_skill(db_session, skill_id="SK-RB", name="reasoning_v1", content="orig text", version=1)
    edited = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-RB"),
        json={"content": "edited text"},
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["version"] == 2

    rolled = client.post(f"{PREFIX}/SK-RB/rollback")
    assert rolled.status_code == 200
    data = rolled.json()["data"]
    assert data["content"] == "orig text"  # exact prior content restored
    assert data["version"] == 3  # forward-only counter keeps climbing


def test_rollback_skill_409_without_prior_edit(
    client: TestClient, db_session: Session
) -> None:
    """A skill that was never content-edited has nothing to roll back to."""
    _insert_skill(db_session, skill_id="SK-RB2", name="guardrails_v1", content="only", version=1)
    resp = client.post(f"{PREFIX}/SK-RB2/rollback")
    assert resp.status_code == 409


def test_update_skill_non_content_does_not_bump_version(
    client: TestClient, db_session: Session
) -> None:
    """Tag tweaks and owner reassignments don't invalidate references."""
    _insert_skill(db_session, skill_id="SK-V", name="reasoning_v1", version=3)
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-V"),
        json={"owner": "@new-owner", "tags": ["reasoning", "rebranded"]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"] == 3


def test_update_skill_archive_flow(client: TestClient, db_session: Session) -> None:
    """Soft-delete: status flips to archived, list query no longer
    returns it by default, but detail GET still works."""
    _insert_skill(db_session, skill_id="SK-arch", name="old_rubric")
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-arch"),
        json={"status": "archived"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"

    list_response = client.get(LIST_PATH)
    names = {item["name"] for item in list_response.json()["data"]}
    assert "old_rubric" not in names


def test_update_skill_rejects_invalid_status(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1")
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-A"),
        json={"status": "trash"},
    )
    assert response.status_code in (400, 422)


def test_update_skill_400_on_empty_body(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1")
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-A"),
        json={},
    )
    assert response.status_code == 400


def test_update_skill_404(client: TestClient) -> None:
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-missing"),
        json={"content": "foo"},
    )
    assert response.status_code == 404


def test_update_skill_writes_audit_row(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", version=1)
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-A"),
        json={"content": "v2", "tags": ["reasoning", "core"]},
    )
    assert response.status_code == 200

    rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_id == "SK-A",
                CaliberAuditLog.action == "update_skill",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    details = rows[0].details or {}
    assert "content" in details["changes"]
    assert details["version_bumped"] is True


def test_update_skill_noop_when_values_match(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-A", name="reasoning_v1", owner="@sarah")
    response = client.patch(
        DETAIL_PATH.replace("{skill_id}", "SK-A"),
        json={"owner": "@sarah"},
    )
    assert response.status_code == 200
    # No audit row written because nothing actually changed.
    rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_id == "SK-A",
                CaliberAuditLog.action == "update_skill",
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Durable skill-test runs (S1) — POST/list/detail, recompute, filters, scope.
# ---------------------------------------------------------------------------


def _make_skill(client: TestClient, name: str = "scenario-skill") -> str:
    """Create a skill via the API and return its skill_id."""
    body = {
        "name": name,
        "description": "A testable skill.",
        "summary": "when scenarios matter",
        "content": "Do the thing for {{topic}}.",
        "owner": "@test",
    }
    return client.post(PREFIX, json=body).json()["data"]["skill_id"]


def _skill_run_body(skill_id: str, **overrides: Any) -> dict[str, Any]:
    """Minimal POST body for ``/skills/test-runs`` with two pass + one fail."""
    body: dict[str, Any] = {
        "skill_id": skill_id,
        "kind": "scenario",
        "skill_version": 1,
        "results": [
            {
                "name": "case-1",
                "input": {"user_message": "help with refunds"},
                "output": {"selected": True},
                "verdict": "pass",
                "score": 1.0,
                "duration_ms": 1.2,
                "reasoning": "ok",
            },
            {
                "name": "case-2",
                "input": {"user_message": "hello"},
                "output": {"selected": True},
                "verdict": "pass",
                "score": 0.8,
                "reasoning": "ok",
            },
            {
                "name": "case-3",
                "input": {"user_message": "boom"},
                "output": None,
                "verdict": "fail",
                "score": 0.0,
                "reasoning": "missed",
            },
        ],
    }
    body.update(overrides)
    return body


def test_create_list_and_get_skill_test_run_roundtrip(
    client: TestClient, db_session: Session
) -> None:
    """POST a run → list shows summary (no results) → detail returns per-case data."""
    sid = _make_skill(client)
    create = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid))
    assert create.status_code == 201
    saved = create.json()["data"]
    test_run_id = saved["test_run_id"]
    assert test_run_id.startswith("SKR-")
    # Server-recomputed aggregates (2 pass + 1 fail, mean of 1.0/0.8/0.0 = 0.6).
    assert saved["test_set_size"] == 3
    assert saved["passed_count"] == 2
    assert saved["failed_count"] == 1
    assert saved["partial_count"] == 0
    assert saved["overall_score"] == 0.6
    assert saved["kind"] == "scenario"
    assert saved["skill_version"] == 1
    assert "results" not in saved
    assert saved["created_by"] == "@test"

    row = db_session.get(CaliberSkillTestRun, test_run_id)
    assert row is not None
    assert len(row.results) == 3
    assert row.completed_at is not None

    listing = client.get(f"{PREFIX}/test-runs", params={"skill_id": sid})
    assert listing.status_code == 200
    rows = listing.json()["data"]
    assert len(rows) == 1
    assert rows[0]["test_run_id"] == test_run_id
    assert "results" not in rows[0]

    detail = client.get(f"{PREFIX}/test-runs/{test_run_id}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert len(detail_data["results"]) == 3
    assert detail_data["results"][0]["name"] == "case-1"
    assert detail_data["results"][2]["verdict"] == "fail"


def test_create_skill_test_run_recomputes_and_ignores_client_aggregates(
    client: TestClient,
) -> None:
    """The server computes counts/score from ``results`` and rejects stray aggregates."""
    sid = _make_skill(client, "recompute-skill")
    rejected = client.post(
        f"{PREFIX}/test-runs",
        json=_skill_run_body(sid, passed_count=999, overall_score=0.99),
    )
    assert rejected.status_code == 400

    body = _skill_run_body(
        sid,
        results=[
            {"name": "a", "input": {}, "verdict": "partial", "score": 0.5},
            {"name": "b", "input": {}, "verdict": "pass", "score": 1.0},
        ],
    )
    saved = client.post(f"{PREFIX}/test-runs", json=body).json()["data"]
    assert saved["test_set_size"] == 2
    assert saved["passed_count"] == 1
    assert saved["partial_count"] == 1
    assert saved["failed_count"] == 0
    assert saved["overall_score"] == 0.75


def test_create_skill_test_run_rejects_empty_results(client: TestClient) -> None:
    sid = _make_skill(client, "empty-skill")
    r = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid, results=[]))
    assert r.status_code == 400


def test_create_skill_test_run_unknown_skill_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/test-runs", json=_skill_run_body("SK-nonexistent"))
    assert r.status_code == 404


def test_create_skill_test_run_rejects_invalid_verdict_and_kind(client: TestClient) -> None:
    sid = _make_skill(client, "bad-input-skill")
    bad_verdict = client.post(
        f"{PREFIX}/test-runs",
        json=_skill_run_body(
            sid, results=[{"name": "x", "input": {}, "verdict": "maybe", "score": 0.5}]
        ),
    )
    assert bad_verdict.status_code == 400
    bad_kind = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid, kind="bogus"))
    assert bad_kind.status_code == 400


def test_get_skill_test_run_unknown_id_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/test-runs/SKR-nope").status_code == 404


def test_create_skill_test_run_requires_operator_scope(client: TestClient) -> None:
    sid = _make_skill(client, "scope-skill")
    r = client.post(
        f"{PREFIX}/test-runs",
        json=_skill_run_body(sid),
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_list_skill_test_runs_kind_filter_order_and_limit(client: TestClient) -> None:
    sid = _make_skill(client, "filter-skill")
    other = _make_skill(client, "filter-skill-other")
    client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid, kind="scenario"))
    client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid, kind="scenario"))
    client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid, kind="selection"))
    client.post(f"{PREFIX}/test-runs", json=_skill_run_body(other, kind="scenario"))

    sid_rows = client.get(f"{PREFIX}/test-runs", params={"skill_id": sid}).json()["data"]
    assert len(sid_rows) == 3
    assert all(r["skill_id"] == sid for r in sid_rows)

    selection_rows = client.get(
        f"{PREFIX}/test-runs", params={"skill_id": sid, "kind": "selection"}
    ).json()["data"]
    assert len(selection_rows) == 1
    assert selection_rows[0]["kind"] == "selection"

    limited = client.get(f"{PREFIX}/test-runs", params={"skill_id": sid, "limit": 1}).json()["data"]
    assert len(limited) == 1

    assert client.get(f"{PREFIX}/test-runs", params={"limit": 9999}).status_code == 200
    assert client.get(f"{PREFIX}/test-runs", params={"limit": "0"}).status_code == 400
    assert client.get(f"{PREFIX}/test-runs", params={"limit": "abc"}).status_code == 400


# ---------------------------------------------------------------------------
# Hidden skill harness — agent-free calibrate front door.
# ---------------------------------------------------------------------------


def test_calibrate_skill_provisions_hidden_target_and_queues_job(
    client: TestClient, db_session: Session
) -> None:
    """POST /skills/{id}/calibrate provisions a skill_target and queues a skill job."""
    sid = _make_skill(client, "calibrate-skill")

    response = client.post(f"{PREFIX}/{sid}/calibrate", json={})
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["job"]["artifact_type"] == "skill"
    assert data["job"]["status"] == "queued"
    assert data["job"]["optimizer_type"] == "SkillMetaPrompt"
    assert data["item"]["category"] == "skill_calibration"

    # The job is keyed on the HIDDEN skill target, not a visible agent.
    target_agent_id = skill_target_agent_id("calibrate-skill")
    job = db_session.get(CaliberRefinementJob, data["job"]["job_id"])
    assert job is not None
    assert job.agent_id == target_agent_id
    assert job.skill_name == "calibrate-skill"

    # The provisioned row is a skill target (marker on optimizer_config), NOT an agent.
    target = db_session.get(CaliberAgentConfig, target_agent_id)
    assert target is not None
    assert target.optimizer_config["source_type"] == "skill_target"
    assert target.optimizer_config["skill_name"] == "calibrate-skill"

    item = db_session.get(CaliberVerificationItem, data["item"]["item_id"])
    assert item is not None
    assert item.artifact_type_hint == "skill"
    assert item.artifact_ref == "calibrate-skill"
    assert item.agent_id == target_agent_id


def test_calibrate_skill_reuses_hidden_target_on_second_call(
    client: TestClient, db_session: Session
) -> None:
    """A second calibrate reuses the same hidden target (idempotent provisioning)."""
    sid = _make_skill(client, "reuse-skill")
    first = client.post(f"{PREFIX}/{sid}/calibrate", json={})
    second = client.post(f"{PREFIX}/{sid}/calibrate", json={"optimizer_type": "GEPA"})
    assert first.status_code == 201
    assert second.status_code == 201

    target_agent_id = skill_target_agent_id("reuse-skill")
    targets = (
        db_session.execute(
            select(CaliberAgentConfig).where(CaliberAgentConfig.agent_id == target_agent_id)
        )
        .scalars()
        .all()
    )
    assert len(targets) == 1
    # Two distinct jobs both reference the single shared target.
    jobs = (
        db_session.execute(
            select(CaliberRefinementJob).where(CaliberRefinementJob.agent_id == target_agent_id)
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 2
    assert second.json()["data"]["job"]["optimizer_type"] == "GEPA"


def test_calibrate_skill_unknown_skill_404(client: TestClient) -> None:
    assert client.post(f"{PREFIX}/SK-nope/calibrate", json={}).status_code == 404


def test_calibrate_skill_requires_operator_scope(client: TestClient) -> None:
    sid = _make_skill(client, "calibrate-scope-skill")
    r = client.post(f"{PREFIX}/{sid}/calibrate", json={}, headers={"X-CALIBER-User": "@viewer"})
    assert r.status_code == 403


def test_skill_target_absent_from_agents_listing(client: TestClient) -> None:
    """The provisioned hidden skill target never appears in GET /agents."""
    sid = _make_skill(client, "ghost-skill")
    assert client.post(f"{PREFIX}/{sid}/calibrate", json={}).status_code == 201

    agents = client.get(AGENTS_PREFIX).json()["data"]
    target_agent_id = skill_target_agent_id("ghost-skill")
    assert all(a["agent_id"] != target_agent_id for a in agents)


# ---------------------------------------------------------------------------
# Skill workspace + baseline + bind (S1).
# ---------------------------------------------------------------------------


def test_skill_workspace_lifecycle_draft_tested_bound(client: TestClient) -> None:
    """Draft → Tested (scenario run) → Bound (bind), with version/category/last_run."""
    sid = _make_skill(client, "ws-skill")

    # Draft: skill exists, no run, no bind, no target row yet.
    ws = client.get(f"{PREFIX}/{sid}/workspace")
    assert ws.status_code == 200
    data = ws.json()["data"]
    assert data["status"] == "Draft"
    assert data["lifecycle"] == "Draft"
    assert data["version"] == 1
    assert data["category"] == "custom"
    assert data["bound_to"] is None
    assert data["last_run"] is None

    # Tested: a scenario run flips status to Tested and surfaces last_run.
    run = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid))
    assert run.status_code == 201
    ws_tested = client.get(f"{PREFIX}/{sid}/workspace").json()["data"]
    assert ws_tested["status"] == "Tested"
    assert ws_tested["last_run"] is not None
    assert ws_tested["last_run"]["test_set_size"] == 3
    assert ws_tested["last_run"]["passed_count"] == 2

    # Bound: bind takes precedence over Tested.
    bind = client.post(f"{PREFIX}/{sid}/bind", json={"kind": "standalone"})
    assert bind.status_code == 200
    assert bind.json()["data"]["status"] == "Bound"
    ws_bound = client.get(f"{PREFIX}/{sid}/workspace").json()["data"]
    assert ws_bound["status"] == "Bound"
    assert ws_bound["bound_to"] == {"kind": "standalone"}


def test_skill_workspace_calibrated_when_job_applied(
    client: TestClient, db_session: Session
) -> None:
    """An ``applied`` skill refinement job → Calibrated (outranks Tested)."""
    from caliber.skill_targets import ensure_skill_target

    sid = _make_skill(client, "cal-skill")
    target = ensure_skill_target(db_session, "cal-skill", owner="@test")
    item = CaliberVerificationItem(
        item_id="FB-skcal01",
        agent_id=target.agent_id,
        category="skill_calibration",
        free_text="x",
        severity="standard",
        status="verified",
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-skcal01",
            agent_id=target.agent_id,
            primary_item_id=item.item_id,
            artifact_type="skill",
            skill_name="cal-skill",
            status="applied",
            current_stage="done",
        )
    )
    db_session.commit()
    # Also record a run, to prove Calibrated outranks Tested.
    assert client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid)).status_code == 201

    ws = client.get(f"{PREFIX}/{sid}/workspace").json()["data"]
    assert ws["status"] == "Calibrated"


def test_skill_workspace_missing_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/SK-nope/workspace").status_code == 404


def test_skill_baseline_set_and_reflected(client: TestClient) -> None:
    """Pinning a baseline records it on the target and the workspace reflects it."""
    sid = _make_skill(client, "baseline-skill")
    run = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(sid)).json()["data"]
    test_run_id = run["test_run_id"]

    set_resp = client.post(f"{PREFIX}/{sid}/baseline", json={"test_run_id": test_run_id})
    assert set_resp.status_code == 200
    assert set_resp.json()["data"]["baseline_run_id"] == test_run_id

    ws = client.get(f"{PREFIX}/{sid}/workspace").json()["data"]
    assert ws["baseline_run_id"] == test_run_id
    assert ws["baseline_run"] is not None
    assert ws["baseline_run"]["test_run_id"] == test_run_id


def test_skill_baseline_run_belongs_to_other_skill_400(client: TestClient) -> None:
    owner = _make_skill(client, "baseline-owner-skill")
    other = _make_skill(client, "baseline-other-skill")
    run = client.post(f"{PREFIX}/test-runs", json=_skill_run_body(owner)).json()["data"]
    resp = client.post(f"{PREFIX}/{other}/baseline", json={"test_run_id": run["test_run_id"]})
    assert resp.status_code == 400


def test_skill_baseline_missing_run_404(client: TestClient) -> None:
    sid = _make_skill(client, "baseline-missing-skill")
    assert (
        client.post(f"{PREFIX}/{sid}/baseline", json={"test_run_id": "SKR-nope"}).status_code == 404
    )


def test_skill_baseline_unknown_skill_404(client: TestClient) -> None:
    assert (
        client.post(f"{PREFIX}/SK-nope/baseline", json={"test_run_id": "SKR-x"}).status_code == 404
    )


def test_skill_bind_agent_adds_skill_to_optimizer_config(
    client: TestClient, db_session: Session
) -> None:
    """Binding kind=agent records bound_to and adds the skill name to the agent."""
    sid = _make_skill(client, "bound-skill")
    db_session.add(
        CaliberAgentConfig(
            agent_id="real-agent",
            experiment_id="exp-real-bind",
            name="Real Agent",
            owner="@test",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.commit()

    bind = client.post(f"{PREFIX}/{sid}/bind", json={"kind": "agent", "agent_id": "real-agent"})
    assert bind.status_code == 200
    assert bind.json()["data"]["bound_to"] == {"kind": "agent", "agent_id": "real-agent"}

    db_session.expire_all()
    # bound_to recorded on the hidden skill target.
    target = db_session.get(CaliberAgentConfig, skill_target_agent_id("bound-skill"))
    assert target is not None
    assert target.optimizer_config["bound_to"] == {"kind": "agent", "agent_id": "real-agent"}
    # The real agent now references the skill by name.
    agent = db_session.get(CaliberAgentConfig, "real-agent")
    assert agent is not None
    assert "bound-skill" in agent.optimizer_config["skills"]


def test_skill_bind_rejects_invalid_kind_and_missing_ids(client: TestClient) -> None:
    sid = _make_skill(client, "bind-bad-skill")
    bad_kind = client.post(f"{PREFIX}/{sid}/bind", json={"kind": "nonsense"})
    assert bad_kind.status_code == 400
    missing_agent_id = client.post(f"{PREFIX}/{sid}/bind", json={"kind": "agent"})
    assert missing_agent_id.status_code == 400
    missing_node = client.post(
        f"{PREFIX}/{sid}/bind", json={"kind": "workflow_node", "workflow_id": "WF-1"}
    )
    assert missing_node.status_code == 400


def test_skill_bind_unknown_agent_404(client: TestClient) -> None:
    sid = _make_skill(client, "bind-unknown-agent-skill")
    resp = client.post(f"{PREFIX}/{sid}/bind", json={"kind": "agent", "agent_id": "no-such-agent"})
    assert resp.status_code == 404


def test_skill_bind_unknown_skill_404(client: TestClient) -> None:
    assert client.post(f"{PREFIX}/SK-nope/bind", json={"kind": "standalone"}).status_code == 404


def test_test_selection_and_test_render_still_reachable(client: TestClient) -> None:
    """The existing read-only test surfaces remain reachable as-is (S1 unchanged)."""
    sid = _make_skill(client, "reachable-skill")
    selection = client.post(f"{PREFIX}/{sid}/test-selection", json={"user_message": "do the thing"})
    assert selection.status_code == 200
    assert "is_selected" in selection.json()["data"]
    assert "selection_score" in selection.json()["data"]

    render = client.post(f"{PREFIX}/{sid}/test-render", json={"variables": {"topic": "X"}})
    assert render.status_code == 200
    assert "rendered_content" in render.json()["data"]
