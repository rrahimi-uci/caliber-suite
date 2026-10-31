"""B06.3 (#747) public path: the reference profile applies a refinement candidate only by a distinct approver.

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with the same safe
settings, ``CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER=true``). The account that requested a job cannot apply its candidate,
a job with no recorded requester is refused rather than passed, and a different project member can apply it. A refused
Apply leaves the job ``candidate_ready`` with no approval anchor and no promotion; racing Applies by the requester and by
a second operator produce exactly one promotion, by the second operator.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import func, select, update

from caliber.candidate_digest import candidate_digest
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberProjectMember,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
AGENT_ID = "b063-agent"
CANDIDATE_BODY = "b063 candidate body"
CANDIDATE = {
    "artifact_type": "prompt",
    "content": CANDIDATE_BODY,
    "rationale": "r",
    "diff_summary": "+1 / -1",
}


def _seed_job(app: ReferenceApp, job_id: str, requester: str | None) -> None:
    """A ``candidate_ready`` prompt job in project A requested by ``requester`` (``None``: a legacy job)."""
    with app.session_factory() as session:
        if session.get(CaliberAgentConfig, AGENT_ID) is None:
            session.add(
                CaliberAgentConfig(
                    agent_id=AGENT_ID,
                    experiment_id="exp-b063",
                    name="B06.3 agent",
                    owner="@egi",
                    project_id=PROJECT_A,
                    visibility="project",
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
                agent_id=AGENT_ID,
                category="hallucination",
                free_text="evidence",
                severity="critical",
                status="verified",
            )
        )
        session.flush()
        session.add(
            CaliberRefinementJob(
                job_id=job_id,
                triggered_by=requester,
                agent_id=AGENT_ID,
                primary_item_id=f"FB-{job_id}",
                artifact_type="prompt",
                status="candidate_ready",
                current_stage="done",
                attempt_count=1,
                bundle_targets=[],
                diagnosis={"root_cause": "x", "confidence": 0.9, "alternatives": []},
                eval_results={"gate": {"passed": True}},
                candidate=CANDIDATE,
                candidate_digest=candidate_digest(CANDIDATE),
            )
        )
        session.commit()


def _make_editor_b_a_member_of_project_a(app: ReferenceApp) -> None:
    with app.session_factory() as session:
        session.add(
            CaliberProjectMember(
                member_id="egi-m-editor-b-in-a",
                project_id=PROJECT_A,
                user_id=EDITOR_B,
                role="editor",
                created_by="@egi",
            )
        )
        session.commit()


def _headers(app: ReferenceApp, user: str) -> dict[str, str]:
    headers = app.headers(user, PROJECT_A)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return {**headers, "X-CALIBER-CSRF": token}


def _apply(app: ReferenceApp, user: str, job_id: str) -> Any:
    return app.client.post(f"{PREFIX}/jobs/{job_id}/apply", headers=_headers(app, user))


def _state(app: ReferenceApp, job_id: str) -> tuple[str, int]:
    """The job's persisted status and how many approval anchors it has."""
    with app.session_factory() as session:
        status = session.execute(
            select(CaliberRefinementJob.status).where(CaliberRefinementJob.job_id == job_id)
        ).scalar_one()
        approvals = session.execute(
            select(func.count())
            .select_from(CaliberApprovalRequest)
            .where(CaliberApprovalRequest.job_id == job_id)
        ).scalar_one()
    return str(status), int(approvals)


def _promotions(app: ReferenceApp) -> int:
    return len(app.client.app.state.promoter.calls)  # type: ignore[attr-defined]


def test_requester_cannot_apply_their_own_candidate(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _seed_job(reference_app, "RFN-B063-SELF", EDITOR_A)
    refused = _apply(reference_app, EDITOR_A, "RFN-B063-SELF")
    assert refused.status_code == 403
    assert refused.json()["reason_code"] == "apply_self_approval"
    assert EDITOR_A not in refused.text and CANDIDATE_BODY not in refused.text
    assert _state(reference_app, "RFN-B063-SELF") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 0


def test_a_legacy_job_with_no_requester_is_refused(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B063-LEGACY", None)
    refused = _apply(reference_app, EDITOR_B, "RFN-B063-LEGACY")
    assert refused.status_code == 403
    assert refused.json()["reason_code"] == "apply_creator_unknown"
    assert _state(reference_app, "RFN-B063-LEGACY") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 0


def test_a_distinct_project_member_can_apply(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B063-OK", EDITOR_A)
    assert _apply(reference_app, EDITOR_A, "RFN-B063-OK").status_code == 403
    applied = _apply(reference_app, EDITOR_B, "RFN-B063-OK")
    assert applied.status_code == 200, applied.text
    assert _state(reference_app, "RFN-B063-OK") == ("applied", 1)
    assert _promotions(reference_app) == 1
    with reference_app.session_factory() as session:
        approver = session.execute(
            select(CaliberApprovalRequest.approved_by).where(
                CaliberApprovalRequest.job_id == "RFN-B063-OK"
            )
        ).scalar_one()
    assert approver == EDITOR_B


def test_the_rule_cannot_be_bypassed_by_naming_another_requester_in_the_request(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _seed_job(reference_app, "RFN-B063-FORGE", EDITOR_A)
    response = reference_app.client.post(
        f"{PREFIX}/jobs/RFN-B063-FORGE/apply?triggered_by={EDITOR_B}",
        json={"triggered_by": EDITOR_B, "approved_by": EDITOR_B},
        headers=_headers(reference_app, EDITOR_A),
    )
    assert response.status_code == 403
    assert response.json()["reason_code"] == "apply_self_approval"
    assert _state(reference_app, "RFN-B063-FORGE") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 0


def test_racing_applies_promote_exactly_once_and_never_for_the_requester(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The requester and the second operator (twice) apply at the same instant: one promotion, by the second operator."""
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B063-RACE", EDITOR_A)
    users = [EDITOR_A, EDITOR_B, EDITOR_B]
    prepared = [_headers(reference_app, user) for user in users]
    barrier = threading.Barrier(len(users))
    codes: list[int] = [0] * len(users)

    def run(index: int) -> None:
        barrier.wait()
        codes[index] = reference_app.client.post(
            f"{PREFIX}/jobs/RFN-B063-RACE/apply", headers=prepared[index]
        ).status_code

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(users))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert codes[0] == 403, codes
    assert sorted(codes[1:]) == [200, 409], codes
    assert _state(reference_app, "RFN-B063-RACE") == ("applied", 1)
    assert _promotions(reference_app) == 1
    with reference_app.session_factory() as session:
        approver = session.execute(
            select(CaliberApprovalRequest.approved_by).where(
                CaliberApprovalRequest.job_id == "RFN-B063-RACE"
            )
        ).scalar_one()
    assert approver == EDITOR_B


def test_the_stored_requester_decides_not_who_it_was_earlier(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """Whatever the row says when Apply runs is what counts: once it names the caller, the caller is refused."""
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B063-REWRITE", EDITOR_A)
    with reference_app.session_factory() as session:
        session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == "RFN-B063-REWRITE")
            .values(triggered_by=EDITOR_B)
        )
        session.commit()
    assert _apply(reference_app, EDITOR_B, "RFN-B063-REWRITE").status_code == 403
    assert _state(reference_app, "RFN-B063-REWRITE") == ("candidate_ready", 0)
    assert _apply(reference_app, EDITOR_A, "RFN-B063-REWRITE").status_code == 200


@pytest.mark.parametrize(
    ("variant", "reason_code"),
    [
        (EDITOR_B.upper(), "apply_self_approval"),
        (f"  {EDITOR_B}\t", "apply_self_approval"),
        ("", "apply_creator_unknown"),
        (None, "apply_creator_unknown"),
    ],
    ids=["upper", "padded", "blank", "null"],
)
def test_a_requester_rewritten_after_the_check_cannot_claim_on_either_dialect(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
    variant: str | None,
    reason_code: str,
) -> None:
    """The claim UPDATE repeats the separation in SQL (normalised, null-safe) on SQLite and PostgreSQL.

    The up-front check reads a distinct requester; between it and the claim the row is rewritten, in the caller's own
    transaction, to a case/whitespace variant of the caller or to no requester. The claim then matches no row.
    """
    import caliber.routes.jobs as jobs_route

    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B063-TOCTOU", EDITOR_A)
    real_load = jobs_route.load_visible_job

    def load_then_rewrite(session: Any, *args: Any, **kwargs: Any) -> Any:
        loaded = real_load(session, *args, **kwargs)
        session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == "RFN-B063-TOCTOU")
            .values(triggered_by=variant)
            .execution_options(synchronize_session=False)
        )
        return loaded

    monkeypatch.setattr(jobs_route, "load_visible_job", load_then_rewrite)
    refused = _apply(reference_app, EDITOR_B, "RFN-B063-TOCTOU")
    assert refused.status_code == 403, refused.text
    assert refused.json()["reason_code"] == reason_code
    assert _state(reference_app, "RFN-B063-TOCTOU") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 0
