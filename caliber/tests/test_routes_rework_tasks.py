"""Integration tests for ``/caliber/rework-tasks``.

Owned, recoverable work auto-created when a refinement job is terminally
rejected (see ``orchestrator/eval_stage.py``). These tests cover the CRUD-ish
lifecycle routes directly (seeding rows by hand); ``test_auto_creation_*``
below drives the real rejection path end to end to prove the route surface
actually sees what ``eval_stage.py`` creates.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberReworkTask,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import ScoreSet
from caliber.orchestrator.eval_stage import run_eval
from caliber.routes.rework_tasks import (
    CLAIM_PATH,
    DETAIL_PATH,
    LIST_PATH,
    REASSIGN_PATH,
    RESOLVE_PATH,
)

# Distinct from the default admin test user so assignee/admin branches can be
# told apart. Not in the default app_config's admin list.
OPERATOR_A = "@op-a"
OPERATOR_B = "@op-b"


def _seed_agent_and_job(session: Session, job_id: str = "RFN-1") -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-1",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="rejected",
            current_stage="done",
            bundle_targets=[],
        )
    )
    session.commit()


def _seed_task(
    session: Session,
    *,
    task_id: str = "RWT-1",
    job_id: str = "RFN-1",
    status: str = "open",
    assigned_to: str | None = None,
    failure_kind: str = "machine_gate",
) -> None:
    if session.get(CaliberAgentConfig, "support-agent") is None:
        _seed_agent_and_job(session, job_id=job_id)
    elif session.get(CaliberRefinementJob, job_id) is None:
        session.add(
            CaliberRefinementJob(
                job_id=job_id,
                agent_id="support-agent",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="rejected",
                current_stage="done",
                bundle_targets=[],
            )
        )
        session.commit()
    session.add(
        CaliberReworkTask(
            task_id=task_id,
            job_id=job_id,
            agent_id="support-agent",
            failure_kind=failure_kind,
            reason="regression gate failed: factual dropped 0.38",
            gate_evidence={"reasons": ["factual dropped"]},
            status=status,
            assigned_to=assigned_to,
            created_by="@system",
        )
    )
    session.commit()


def _operator_client(client: TestClient, app_config: CaliberConfig) -> None:
    """Rewire the shared test client to grant only ``@op-a``/``@op-b``
    operator scope (not admin), so assignee-vs-admin branches are testable."""
    client.app.state.config = app_config.model_copy(
        update={"operator_users": f"{OPERATOR_A},{OPERATOR_B}"}
    )


# ---------------------------------------------------------------------------
# GET /rework-tasks, /rework-tasks/{id}
# ---------------------------------------------------------------------------


def test_list_tasks_defaults_to_open_status(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, task_id="RWT-OPEN", job_id="RFN-1", status="open")
    _seed_task(db_session, task_id="RWT-RESOLVED", job_id="RFN-2", status="resolved")

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    ids = {row["task_id"] for row in response.json()["data"]}
    assert ids == {"RWT-OPEN"}


def test_list_tasks_status_all_returns_every_status(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, task_id="RWT-OPEN", job_id="RFN-1", status="open")
    _seed_task(db_session, task_id="RWT-RESOLVED", job_id="RFN-2", status="resolved")

    response = client.get(LIST_PATH, params={"status": "all"})
    assert response.status_code == 200
    ids = {row["task_id"] for row in response.json()["data"]}
    assert ids == {"RWT-OPEN", "RWT-RESOLVED"}


def test_list_tasks_filters_by_assigned_to(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, task_id="RWT-MINE", job_id="RFN-1", status="in_progress", assigned_to="@sarah")
    _seed_task(db_session, task_id="RWT-THEIRS", job_id="RFN-2", status="in_progress", assigned_to="@alex")

    response = client.get(LIST_PATH, params={"status": "all", "assigned_to": "@sarah"})
    assert response.status_code == 200
    ids = {row["task_id"] for row in response.json()["data"]}
    assert ids == {"RWT-MINE"}


def test_list_tasks_invalid_status_returns_400(client: TestClient) -> None:
    response = client.get(LIST_PATH, params={"status": "bogus"})
    assert response.status_code == 400


def test_get_task_returns_record(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session)
    response = client.get(DETAIL_PATH.replace("{task_id}", "RWT-1"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task_id"] == "RWT-1"
    assert data["job_id"] == "RFN-1"
    assert data["failure_kind"] == "machine_gate"
    assert data["status"] == "open"


def test_get_task_404_when_missing(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{task_id}", "RWT-GHOST"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /rework-tasks/{id}/claim
# ---------------------------------------------------------------------------


def test_claim_transitions_open_to_in_progress_and_assigns_caller(
    client: TestClient, db_session: Session
) -> None:
    _seed_task(db_session)
    response = client.post(
        CLAIM_PATH.replace("{task_id}", "RWT-1"), headers={"X-CALIBER-User": "@reza"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "in_progress"
    assert data["assigned_to"] == "@reza"


def test_claim_conflicts_when_not_open(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, status="in_progress", assigned_to="@sarah")
    response = client.post(CLAIM_PATH.replace("{task_id}", "RWT-1"))
    assert response.status_code == 409


def test_claim_404_when_missing(client: TestClient) -> None:
    response = client.post(CLAIM_PATH.replace("{task_id}", "RWT-GHOST"))
    assert response.status_code == 404


def test_claim_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session)
    response = client.post(
        CLAIM_PATH.replace("{task_id}", "RWT-1"), headers={"X-CALIBER-User": ""}
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /rework-tasks/{id}/resolve
# ---------------------------------------------------------------------------


def test_resolve_by_assignee_succeeds(
    client: TestClient, db_session: Session, app_config: CaliberConfig
) -> None:
    _seed_task(db_session, status="in_progress", assigned_to=OPERATOR_A)
    _operator_client(client, app_config)

    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={"resolution_notes": "fixed via manual prompt edit"},
        headers={"X-CALIBER-User": OPERATOR_A},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "resolved"
    assert data["resolved_by"] == OPERATOR_A
    assert data["resolution_notes"] == "fixed via manual prompt edit"
    assert data["resolved_at"] is not None


def test_resolve_by_non_assignee_operator_returns_403(
    client: TestClient, db_session: Session, app_config: CaliberConfig
) -> None:
    _seed_task(db_session, status="in_progress", assigned_to=OPERATOR_A)
    _operator_client(client, app_config)

    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={},
        headers={"X-CALIBER-User": OPERATOR_B},
    )
    assert response.status_code == 403


def test_resolve_by_admin_succeeds_even_if_not_assignee(
    client: TestClient, db_session: Session
) -> None:
    """The default test client's user is admin (see conftest); an admin may
    resolve a task assigned to someone else."""
    _seed_task(db_session, status="in_progress", assigned_to=OPERATOR_A)
    response = client.post(RESOLVE_PATH.replace("{task_id}", "RWT-1"), json={})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "resolved"


def test_resolve_conflicts_when_not_in_progress(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, status="open")
    response = client.post(RESOLVE_PATH.replace("{task_id}", "RWT-1"), json={})
    assert response.status_code == 409


def test_resolve_with_resolution_job_id_records_it(
    client: TestClient, db_session: Session
) -> None:
    _seed_task(db_session, status="in_progress", assigned_to="@reza")
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-2",
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="applied",
            current_stage="done",
            bundle_targets=[],
        )
    )
    db_session.commit()

    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={"resolution_job_id": "RFN-2"},
        headers={"X-CALIBER-User": "@reza"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["resolution_job_id"] == "RFN-2"


def test_resolve_resolution_job_id_missing_returns_404(
    client: TestClient, db_session: Session
) -> None:
    _seed_task(db_session, status="in_progress", assigned_to="@reza")
    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={"resolution_job_id": "RFN-GHOST"},
        headers={"X-CALIBER-User": "@reza"},
    )
    assert response.status_code == 404


def test_resolve_resolution_job_id_wrong_agent_returns_400(
    client: TestClient, db_session: Session
) -> None:
    _seed_task(db_session, status="in_progress", assigned_to="@reza")
    db_session.add(
        CaliberAgentConfig(
            agent_id="other-agent",
            experiment_id="exp2",
            name="Other",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.flush()
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-2",
            agent_id="other-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-OTHER-AGENT",
            agent_id="other-agent",
            primary_item_id="FB-2",
            artifact_type="prompt",
            status="applied",
            current_stage="done",
            bundle_targets=[],
        )
    )
    db_session.commit()

    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={"resolution_job_id": "RFN-OTHER-AGENT"},
        headers={"X-CALIBER-User": "@reza"},
    )
    assert response.status_code == 400


def test_resolve_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, status="in_progress", assigned_to="@reza")
    response = client.post(
        RESOLVE_PATH.replace("{task_id}", "RWT-1"),
        json={},
        headers={"X-CALIBER-User": ""},
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /rework-tasks/{id}/reassign
# ---------------------------------------------------------------------------


def test_reassign_changes_assignee_and_forces_in_progress(
    client: TestClient, db_session: Session
) -> None:
    _seed_task(db_session, status="open")
    response = client.post(
        REASSIGN_PATH.replace("{task_id}", "RWT-1"),
        json={"assigned_to": "@marcus"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["assigned_to"] == "@marcus"
    assert data["status"] == "in_progress"


def test_reassign_conflicts_once_resolved(client: TestClient, db_session: Session) -> None:
    _seed_task(db_session, status="resolved", assigned_to="@sarah")
    response = client.post(
        REASSIGN_PATH.replace("{task_id}", "RWT-1"),
        json={"assigned_to": "@marcus"},
    )
    assert response.status_code == 409


def test_reassign_requires_admin_scope(
    client: TestClient, db_session: Session, app_config: CaliberConfig
) -> None:
    """An operator (not admin) cannot reassign — admin-only policy."""
    _seed_task(db_session, status="open")
    _operator_client(client, app_config)

    response = client.post(
        REASSIGN_PATH.replace("{task_id}", "RWT-1"),
        json={"assigned_to": "@marcus"},
        headers={"X-CALIBER-User": OPERATOR_A},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Auto-creation on rejection (eval_stage.py) is visible through this route.
# ---------------------------------------------------------------------------


def _seed_eval_job(session: Session) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-E",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id="RFN-E",
            agent_id="support-agent",
            primary_item_id="FB-E",
            artifact_type="prompt",
            status="running",
            current_stage="eval",
            bundle_targets=[],
            diagnosis={"root_cause": "x", "confidence": 0.8, "alternatives": []},
            candidate={
                "artifact_type": "prompt",
                "content": "rewritten prompt body",
                "rationale": "addresses tool skip",
                "diff_summary": "+5 / -3 lines",
            },
        )
    )
    session.commit()


def test_auto_created_task_is_visible_and_actionable_through_the_route(
    client: TestClient, session_factory: object
) -> None:
    """A real terminal rejection (via ``run_eval``) creates a rework task that
    the route surface can list, claim, and resolve -- not just a row inserted
    by a test fixture."""
    with session_factory() as session:  # type: ignore[operator]
        _seed_eval_job(session)
        provider = FakeEvalProvider(
            candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5}),
            baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88}),
        )
        run_eval(session, "RFN-E", provider)

    listed = client.get(LIST_PATH)
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1
    task = rows[0]
    assert task["job_id"] == "RFN-E"
    assert task["agent_id"] == "support-agent"
    assert task["failure_kind"] == "machine_gate"
    assert task["status"] == "open"
    assert task["gate_evidence"] is not None

    claimed = client.post(
        CLAIM_PATH.replace("{task_id}", task["task_id"]),
        headers={"X-CALIBER-User": "@reza"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["status"] == "in_progress"

    resolved = client.post(
        RESOLVE_PATH.replace("{task_id}", task["task_id"]),
        json={"resolution_notes": "reran with an updated golden dataset"},
        headers={"X-CALIBER-User": "@reza"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["status"] == "resolved"
