"""Project isolation for the refinement-job routes (alpha gap P0-1).

``CaliberRefinementJob`` carries no project column; a job belongs to the agent it
refines. These tests pin the contract that ``/jobs`` list, detail, targets, Apply,
request-changes and ``/quality-reviews`` follow that agent's visibility and project
role:

* a non-member answers 404 on every route, even with the global operator scope and
  even when the request names the project in ``X-CALIBER-Project``;
* a member without the needed role answers 403;
* a refused caller causes no side effect: the promoter is never called, the job's
  status is unchanged, and no approval, quality-review or audit row is written;
* owners/editors, reviewers (for review actions) and the personal-agent owner keep
  working.

Before this contract, an operator who was not a member of project A could Apply A's
candidate to production, and a viewer-role member of A could do the same.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberQualityReview,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER

PREFIX = "/ajax-api/2.0/mlflow/caliber"
PROJECT_ID = "P-jobs-isolation"
JOB_ID = "RFN-ISO-1"
AGENT_ID = "iso-agent"

_PROJECT_HEADER = {"X-CALIBER-Project": PROJECT_ID}


def _as(user: str, *, project: bool = True) -> dict[str, str]:
    headers = {"X-CALIBER-User": user}
    if project:
        headers.update(_PROJECT_HEADER)
    return headers


def _grant_operator(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.operator_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(
        update={"operator_users": ",".join(sorted(existing))}
    )


def _add_member(session: Session, user_id: str, role: str) -> None:
    session.add(
        CaliberProjectMember(
            member_id=f"M-{user_id.lstrip('@')}",
            project_id=PROJECT_ID,
            user_id=user_id,
            role=role,
            created_by="@owner",
        )
    )


def _seed_job(
    session: Session,
    *,
    job_id: str = JOB_ID,
    agent_id: str = AGENT_ID,
    project_id: str | None = PROJECT_ID,
    visibility: str = "project",
    owner: str = "@owner",
    create_agent: bool = True,
) -> None:
    if create_agent:
        session.add(
            CaliberAgentConfig(
                agent_id=agent_id,
                experiment_id=f"exp-{agent_id}",
                name=agent_id,
                owner=owner,
                project_id=project_id,
                visibility=visibility,
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
            )
        )
        session.flush()
    session.add(
        CaliberVerificationItem(
            item_id=f"FB-{job_id}",
            agent_id=agent_id,
            category="hallucination",
            free_text="secret evidence",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id=agent_id,
            primary_item_id=f"FB-{job_id}",
            artifact_type="prompt",
            status="candidate_ready",
            current_stage="done",
            attempt_count=1,
            bundle_targets=[],
            diagnosis={"root_cause": "SECRET-DIAGNOSIS", "confidence": 0.9, "alternatives": []},
            eval_results={"gate": {"passed": True}},
            candidate={
                "artifact_type": "prompt",
                "content": "SECRET-CANDIDATE-BODY",
                "rationale": "r",
                "diff_summary": "+1 / -1",
            },
        )
    )
    session.commit()


@pytest.fixture
def isolated(client: TestClient, db_session: Session) -> TestClient:
    """Project ``P-jobs-isolation`` with an editor, a reviewer and a viewer member,
    a project-scoped candidate_ready job, and operator scope for every caller
    (including the stranger, so only project authorization can refuse them)."""
    db_session.add(CaliberProject(project_id=PROJECT_ID, name="Jobs isolation", owner="@owner"))
    db_session.commit()
    _add_member(db_session, "@editor", ROLE_EDITOR)
    _add_member(db_session, "@reviewer", ROLE_REVIEWER)
    _add_member(db_session, "@viewer", ROLE_VIEWER)
    db_session.commit()
    _seed_job(db_session)
    _grant_operator(client, "@editor", "@reviewer", "@viewer", "@stranger")
    return client


def _job_status(db_session: Session, job_id: str = JOB_ID) -> str:
    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    return job.status


def _count(db_session: Session, model: type) -> int:
    db_session.expire_all()
    return int(db_session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_untouched(isolated: TestClient, db_session: Session) -> None:
    assert _job_status(db_session) == "candidate_ready"
    assert isolated.app.state.promoter.calls == []  # type: ignore[attr-defined]
    assert _count(db_session, CaliberApprovalRequest) == 0
    assert _count(db_session, CaliberQualityReview) == 0
    actions = set(db_session.execute(select(CaliberAuditLog.action)).scalars())
    assert not actions & {"apply_candidate", "promote", "request_changes", "submit_quality_review"}


# ---------------------------------------------------------------------------
# A non-member: 404 everywhere, no side effects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names_project", [True, False], ids=["with-project-header", "no-header"])
def test_non_member_cannot_read_a_projects_job(isolated: TestClient, names_project: bool) -> None:
    headers = _as("@stranger", project=names_project)

    listing = isolated.get(f"{PREFIX}/jobs", headers=headers)
    assert listing.status_code == 200
    assert JOB_ID not in {row["job_id"] for row in listing.json()["data"]}

    for path in (
        f"/jobs/{JOB_ID}",
        f"/jobs/{JOB_ID}/targets",
        f"/jobs/{JOB_ID}/quality-reviews",
    ):
        resp = isolated.get(f"{PREFIX}{path}", headers=headers)
        assert resp.status_code == 404, (path, resp.text)
        assert "SECRET" not in resp.text


@pytest.mark.parametrize("names_project", [True, False], ids=["with-project-header", "no-header"])
def test_non_member_operator_cannot_apply_review_or_send_back_a_projects_job(
    isolated: TestClient, db_session: Session, names_project: bool
) -> None:
    headers = _as("@stranger", project=names_project)

    apply_resp = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=headers)
    assert apply_resp.status_code == 404, apply_resp.text

    changes = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/request-changes", json={"notes": "bob was here"}, headers=headers
    )
    assert changes.status_code == 404, changes.text

    for decision in ("go", "no_go"):
        review = isolated.post(
            f"{PREFIX}/jobs/{JOB_ID}/quality-reviews",
            json={"decision": decision, "rationale": "stranger opinion"},
            headers=headers,
        )
        assert review.status_code == 404, review.text

    _assert_untouched(isolated, db_session)
    job = db_session.get(CaliberRefinementJob, JOB_ID)
    assert job is not None and job.review_notes is None


def test_a_missing_job_is_still_404_for_everyone(isolated: TestClient) -> None:
    for user in ("@stranger", "@editor"):
        resp = isolated.post(f"{PREFIX}/jobs/RFN-NOPE/apply", json={}, headers=_as(user))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# A member without the role: reads work, mutations are 403
# ---------------------------------------------------------------------------


def test_viewer_member_can_read_but_not_apply(isolated: TestClient, db_session: Session) -> None:
    headers = _as("@viewer")
    listing = isolated.get(f"{PREFIX}/jobs", headers=headers)
    assert JOB_ID in {row["job_id"] for row in listing.json()["data"]}
    assert isolated.get(f"{PREFIX}/jobs/{JOB_ID}", headers=headers).status_code == 200
    assert isolated.get(f"{PREFIX}/jobs/{JOB_ID}/targets", headers=headers).status_code == 200
    assert (
        isolated.get(f"{PREFIX}/jobs/{JOB_ID}/quality-reviews", headers=headers).status_code == 200
    )

    apply_resp = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=headers)
    assert apply_resp.status_code == 403, apply_resp.text
    assert "release.apply" in apply_resp.json()["detail"]

    changes = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/request-changes", json={"notes": "n"}, headers=headers
    )
    assert changes.status_code == 403
    review = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/quality-reviews",
        json={"decision": "go", "rationale": "viewer opinion"},
        headers=headers,
    )
    assert review.status_code == 403
    _assert_untouched(isolated, db_session)


def test_reviewer_member_can_review_but_not_apply(
    isolated: TestClient, db_session: Session
) -> None:
    headers = _as("@reviewer")
    apply_resp = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=headers)
    assert apply_resp.status_code == 403
    assert _job_status(db_session) == "candidate_ready"
    assert isolated.app.state.promoter.calls == []  # type: ignore[attr-defined]

    review = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/quality-reviews",
        json={"decision": "go", "rationale": "looks right"},
        headers=headers,
    )
    assert review.status_code == 201, review.text
    assert _count(db_session, CaliberQualityReview) == 1


def test_reviewer_member_can_reject_a_candidate(isolated: TestClient, db_session: Session) -> None:
    review = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/quality-reviews",
        json={"decision": "no_go", "rationale": "regresses tone"},
        headers=_as("@reviewer"),
    )
    assert review.status_code == 201, review.text
    assert _job_status(db_session) == "rejected"


def test_reviewer_member_can_send_a_candidate_back(
    isolated: TestClient, db_session: Session
) -> None:
    resp = isolated.post(
        f"{PREFIX}/jobs/{JOB_ID}/request-changes",
        json={"notes": "shorter please"},
        headers=_as("@reviewer"),
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, JOB_ID)
    assert job is not None
    assert job.review_notes == "shorter please"
    assert job.status == "running"


# ---------------------------------------------------------------------------
# Members who hold the role keep working
# ---------------------------------------------------------------------------


def test_editor_member_can_apply(isolated: TestClient, db_session: Session) -> None:
    resp = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=_as("@editor"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "applied"
    assert _job_status(db_session) == "applied"
    promoter = isolated.app.state.promoter  # type: ignore[attr-defined]
    assert len(promoter.calls) == 1
    assert promoter.calls[0].new_content == "SECRET-CANDIDATE-BODY"


def test_an_applied_job_is_still_409_on_a_second_apply(isolated: TestClient) -> None:
    first = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=_as("@editor"))
    assert first.status_code == 200
    second = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=_as("@editor"))
    assert second.status_code == 409


def test_member_listing_filters_by_project_header(
    isolated: TestClient, db_session: Session
) -> None:
    """A member who names a *different* project (or none) does not see the job in
    the list, consistent with how prompts of a project are scoped."""
    other = isolated.get(f"{PREFIX}/jobs", headers=_as("@editor", project=False))
    assert JOB_ID not in {row["job_id"] for row in other.json()["data"]}
    mine = isolated.get(f"{PREFIX}/jobs", headers=_as("@editor"))
    assert JOB_ID in {row["job_id"] for row in mine.json()["data"]}


# ---------------------------------------------------------------------------
# Admins and personal agents
# ---------------------------------------------------------------------------


def test_admin_sees_every_job_but_needs_a_real_role_to_apply(
    isolated: TestClient, db_session: Session
) -> None:
    """``caliber.admin`` bypasses the visibility tier for reads (as for every other
    list) but is deliberately not an implicit project member, so it cannot apply a
    project's candidate without a role. That is ``resource_access``'s documented
    P1-B design; this pins that Apply follows it."""
    admin = {"X-CALIBER-User": "@test", **_PROJECT_HEADER}
    listing = isolated.get(f"{PREFIX}/jobs", headers=admin)
    assert JOB_ID in {row["job_id"] for row in listing.json()["data"]}
    assert isolated.get(f"{PREFIX}/jobs/{JOB_ID}", headers=admin).status_code == 200

    resp = isolated.post(f"{PREFIX}/jobs/{JOB_ID}/apply", json={}, headers=admin)
    assert resp.status_code == 404
    _assert_untouched(isolated, db_session)


def test_personal_agent_job_is_visible_only_to_its_owner(
    client: TestClient, db_session: Session
) -> None:
    _seed_job(
        db_session,
        job_id="RFN-PERSONAL",
        agent_id="personal-agent",
        project_id=None,
        visibility="user",
        owner="@alice",
    )
    _grant_operator(client, "@alice", "@bob")

    mine = client.get(f"{PREFIX}/jobs/RFN-PERSONAL", headers={"X-CALIBER-User": "@alice"})
    assert mine.status_code == 200
    theirs = client.get(f"{PREFIX}/jobs/RFN-PERSONAL", headers={"X-CALIBER-User": "@bob"})
    assert theirs.status_code == 404
    refused = client.post(
        f"{PREFIX}/jobs/RFN-PERSONAL/apply", json={}, headers={"X-CALIBER-User": "@bob"}
    )
    assert refused.status_code == 404
    assert _job_status(db_session, "RFN-PERSONAL") == "candidate_ready"

    owned = client.post(
        f"{PREFIX}/jobs/RFN-PERSONAL/apply", json={}, headers={"X-CALIBER-User": "@alice"}
    )
    assert owned.status_code == 200, owned.text


def test_public_agent_job_is_visible_to_any_operator(
    client: TestClient, db_session: Session
) -> None:
    _seed_job(
        db_session,
        job_id="RFN-PUBLIC",
        agent_id="public-agent",
        project_id=None,
        visibility="public",
        owner="@alice",
    )
    _grant_operator(client, "@bob")
    resp = client.post(
        f"{PREFIX}/jobs/RFN-PUBLIC/apply", json={}, headers={"X-CALIBER-User": "@bob"}
    )
    assert resp.status_code == 200, resp.text


def test_a_job_with_no_agent_row_is_treated_as_personal_global(
    client: TestClient, db_session: Session
) -> None:
    """Legacy/hand-seeded data only (a real database enforces the foreign key):
    with no agent row there is no project to protect, so the job stays listed and
    readable, matching how a bare provider-only prompt is treated."""
    db_session.execute(text("PRAGMA foreign_keys=OFF"))
    _seed_job(db_session, job_id="RFN-ORPHAN", agent_id="ghost-agent", create_agent=False)
    _grant_operator(client, "@bob")
    headers = {"X-CALIBER-User": "@bob"}
    listing = client.get(f"{PREFIX}/jobs", headers=headers)
    assert "RFN-ORPHAN" in {row["job_id"] for row in listing.json()["data"]}
    assert client.get(f"{PREFIX}/jobs/RFN-ORPHAN", headers=headers).status_code == 200
