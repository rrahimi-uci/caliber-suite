"""The Apply claim rechecks everything it rests on, inside its own transaction (B06.4, #753).

``POST /jobs/{id}/apply`` used to authorise at the start of the request and then claim. The claim now locks the job and
the rows that decide the caller's access, and rechecks on what it locked: the caller's current project permission, the
distinct requester (the shared :func:`caliber.apply.apply_separation_refusal`), the job status and the exact digest of
the candidate that cleared the eval gate. Every refusal is typed and payload-free, rolls back before any write, and is
audited in its own transaction (#1050). These tests drive the real route on SQLite and inspect the persisted state, the
promoter and the audit log; ``tests/egi_scenarios/test_b06_apply_claim_recheck.py`` races the same claim on PostgreSQL.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.apply import (
    APPLY_BUSY,
    APPLY_CANDIDATE_CHANGED,
    APPLY_CANDIDATE_UNVERIFIED,
    APPLY_CREATOR_UNKNOWN,
    APPLY_PERMISSION_REVOKED,
    APPLY_RECHECK_DETAIL,
    APPLY_SELF_APPROVAL,
    apply_candidate_refusal,
)
from caliber.candidate_digest import CANDIDATE_DIGEST_LENGTH, candidate_digest
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberRefinementJob,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_VIEWER
from caliber.routes._job_access import is_lock_timeout, lock_job_and_authority
from tests.test_apply_distinct_approver import (
    JOB_ID,
    OTHER,
    PROJECT_ID,
    REQUESTER,
    _apply,
    _configure,
    _count,
    _job_status,
    _set_requester,
)
from tests.test_routes_jobs_apply import _seed_candidate_ready_job

ORIGINAL = {
    "artifact_type": "prompt",
    "content": "rewritten prompt body",
    "rationale": "applies tool-use directive",
    "diff_summary": "+5 / -3",
}
EDITED = {**ORIGINAL, "content": "SECRET-EDITED-AFTER-THE-GATE"}


# ------------------------------------------------------------------ helpers


def _refusals(session: Session) -> list[CaliberAuditLog]:
    session.expire_all()
    return list(
        session.execute(select(CaliberAuditLog).where(CaliberAuditLog.action == "apply_refused"))
        .scalars()
        .all()
    )


def _assert_refused_and_nothing_changed(
    client: TestClient, session: Session, *, code: str, actor: str = OTHER
) -> None:
    """The job is still claimable and no approval, promotion, apply audit row or release effect exists.

    Exactly one ``apply_refused`` row exists, and it holds the reason code and nothing else.
    """
    assert _job_status(session) == "candidate_ready"
    assert client.app.state.promoter.calls == []  # type: ignore[attr-defined]
    assert _count(session, CaliberApprovalRequest) == 0
    actions = set(session.execute(select(CaliberAuditLog.action)).scalars())
    assert not actions & {"apply_candidate", "promote", "apply_failed"}
    (row,) = _refusals(session)
    assert row.actor == actor
    assert row.entity_type == "refinement_job"
    assert row.entity_id == JOB_ID
    assert row.severity == "high"
    assert row.details == {"reason_code": code}


def _assert_payload_free(response: Any) -> None:
    body = response.text
    for secret in (
        "SECRET-EDITED-AFTER-THE-GATE",
        "rewritten prompt body",
        "sha256:",
        REQUESTER,
        OTHER,
        JOB_ID,
    ):
        assert secret not in body, secret


@pytest.fixture
def job(client: TestClient, db_session: Session) -> TestClient:
    """A ``candidate_ready`` job whose digest was recorded by the gate, requested by ``@requester``, rule on."""
    _seed_candidate_ready_job(db_session)
    db_session.execute(
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(visibility="public")
    )
    db_session.commit()
    _set_requester(db_session, REQUESTER)
    _configure(client, apply_require_distinct_approver=True)
    return client


@pytest.fixture
def project_job(job: TestClient, db_session: Session) -> TestClient:
    """The job's agent belongs to a project in which ``@other-approver`` is an editor."""
    db_session.add(CaliberProject(project_id=PROJECT_ID, name="Apply recheck", owner="@owner"))
    db_session.commit()
    db_session.add(
        CaliberProjectMember(
            member_id="M-other",
            project_id=PROJECT_ID,
            user_id=OTHER,
            role=ROLE_EDITOR,
            created_by="@owner",
        )
    )
    db_session.execute(
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(project_id=PROJECT_ID, visibility="project")
    )
    db_session.commit()
    return job


def _after_first_load(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> list[int]:
    """Run ``mutate(session)`` once, in the route's own transaction, right after the up-front permission check.

    That is the instant the request-start authorisation would have gone stale. The claim's recheck calls the same
    function again and must see the mutation.
    """
    import caliber.routes.jobs as jobs_route

    real_load = jobs_route.load_visible_job
    calls: list[int] = []

    def load(session: Session, *args: Any, **kwargs: Any) -> CaliberRefinementJob:
        calls.append(1)
        first = len(calls) == 1
        loaded = real_load(session, *args, **kwargs)
        if first:
            mutate(session)
        return loaded

    monkeypatch.setattr(jobs_route, "load_visible_job", load)
    return calls


def _core(session: Session, statement: Any) -> None:
    """Execute a write in the caller's transaction without refreshing objects already loaded."""
    session.execute(statement.execution_options(synchronize_session=False))


# ------------------------------------------------------------------ the digest


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}),
        ({"content": "café"}, {"content": "café"}),
        ({"nested": {"y": 1, "x": 2}}, {"nested": {"x": 2, "y": 1}}),
    ],
    ids=["key-order", "unicode", "nested-key-order"],
)
def test_the_digest_ignores_key_order_and_is_stable(
    left: dict[str, Any], right: dict[str, Any]
) -> None:
    assert candidate_digest(left) == candidate_digest(right)


@pytest.mark.parametrize(
    "other",
    [
        {**ORIGINAL, "content": ORIGINAL["content"] + " "},
        {**ORIGINAL, "content": ORIGINAL["content"].upper()},
        {**ORIGINAL, "rationale": "different"},
        {**ORIGINAL, "extra": None},
        {k: v for k, v in ORIGINAL.items() if k != "diff_summary"},
        {**ORIGINAL, "content": ORIGINAL["content"].replace("e", "\u0435", 1)},
    ],
    ids=["trailing-space", "case", "field-changed", "field-added", "field-removed", "homoglyph"],
)
def test_any_change_to_the_candidate_changes_the_digest(other: dict[str, Any]) -> None:
    assert candidate_digest(other) != candidate_digest(ORIGINAL)


def test_the_digest_has_the_recorded_shape() -> None:
    digest = candidate_digest(ORIGINAL)
    assert len(digest) == CANDIDATE_DIGEST_LENGTH == 71
    assert digest.startswith("sha256:")
    assert set(digest.removeprefix("sha256:")) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "bad",
    [None, "text", ["list"], {"n": math.nan}, {"n": math.inf}, {"o": object()}, {1: "a", "b": 2}],
    ids=["none", "str", "list", "nan", "inf", "object", "mixed-keys"],
)
def test_a_candidate_with_no_canonical_form_cannot_be_digested(bad: Any) -> None:
    with pytest.raises(ValueError):
        candidate_digest(bad)


def test_a_self_referencing_candidate_cannot_be_digested() -> None:
    loop: dict[str, Any] = {}
    loop["self"] = loop
    with pytest.raises(ValueError):
        candidate_digest(loop)


@pytest.mark.parametrize(
    ("recorded", "candidate", "expected"),
    [
        (candidate_digest(ORIGINAL), ORIGINAL, None),
        (candidate_digest(ORIGINAL), dict(reversed(list(ORIGINAL.items()))), None),
        (None, ORIGINAL, APPLY_CANDIDATE_UNVERIFIED),
        ("", ORIGINAL, APPLY_CANDIDATE_UNVERIFIED),
        (candidate_digest(ORIGINAL), EDITED, APPLY_CANDIDATE_CHANGED),
        (candidate_digest(ORIGINAL), None, APPLY_CANDIDATE_CHANGED),
        (candidate_digest(ORIGINAL), {}, APPLY_CANDIDATE_CHANGED),
        (candidate_digest(ORIGINAL), {"n": math.nan}, APPLY_CANDIDATE_CHANGED),
        ("not-a-digest", ORIGINAL, APPLY_CANDIDATE_CHANGED),
        ("sha256:é", ORIGINAL, APPLY_CANDIDATE_CHANGED),
        (candidate_digest(ORIGINAL).upper(), ORIGINAL, APPLY_CANDIDATE_CHANGED),
    ],
    ids=[
        "match",
        "match-reordered",
        "unrecorded-null",
        "unrecorded-blank",
        "edited",
        "no-candidate",
        "empty-candidate",
        "undigestable",
        "malformed-recorded",
        "non-ascii-recorded",
        "uppercase-recorded",
    ],
)
def test_the_candidate_decision(
    recorded: str | None, candidate: dict[str, Any] | None, expected: str | None
) -> None:
    assert apply_candidate_refusal(recorded_digest=recorded, candidate=candidate) == expected


def test_every_recheck_refusal_has_a_static_message_without_identifiers() -> None:
    assert set(APPLY_RECHECK_DETAIL) == {
        APPLY_PERMISSION_REVOKED,
        APPLY_CANDIDATE_CHANGED,
        APPLY_CANDIDATE_UNVERIFIED,
        APPLY_BUSY,
    }
    for message in APPLY_RECHECK_DETAIL.values():
        assert "@" not in message and "RFN" not in message and "sha256" not in message


# ------------------------------------------------------------------ the exact candidate digest


def test_the_candidate_that_cleared_the_gate_is_applied_and_bound_to_the_audit(
    job: TestClient, db_session: Session
) -> None:
    response = _apply(job, OTHER)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    stored = db_session.get(CaliberRefinementJob, JOB_ID)
    assert stored is not None and stored.status == "applied"
    approval = db_session.execute(select(CaliberApprovalRequest)).scalar_one()
    # What was promoted is the very content the recorded digest names.
    assert candidate_digest(approval.candidate_snapshot) == stored.candidate_digest
    promoted = job.app.state.promoter.calls  # type: ignore[attr-defined]
    assert [call.new_content for call in promoted] == [ORIGINAL["content"]]
    audit = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.action == "apply_candidate")
    ).scalar_one()
    assert audit.details["candidate_digest"] == stored.candidate_digest
    assert _refusals(db_session) == []


@pytest.mark.parametrize("profile_flag", [True, False], ids=["rule-on", "rule-off"])
def test_a_candidate_edited_after_the_gate_is_refused_without_a_trace_of_it(
    job: TestClient, db_session: Session, profile_flag: bool
) -> None:
    """The digest check is integrity, not policy: it applies whether or not the two-person rule is on."""
    _configure(job, apply_require_distinct_approver=profile_flag, profile="development")
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate=EDITED)
    )
    db_session.commit()
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    assert response.json()["detail"] == APPLY_RECHECK_DETAIL[APPLY_CANDIDATE_CHANGED]
    _assert_payload_free(response)
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


def test_re_recording_the_digest_after_a_fresh_evaluation_recovers(
    job: TestClient, db_session: Session
) -> None:
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate=EDITED)
    )
    db_session.commit()
    assert _apply(job, OTHER).status_code == 409
    # The eval stage re-ran on the new content and recorded its digest.
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate_digest=candidate_digest(EDITED))
    )
    db_session.commit()
    assert _apply(job, OTHER).status_code == 200
    assert [c.new_content for c in job.app.state.promoter.calls] == [EDITED["content"]]  # type: ignore[attr-defined]


def test_a_candidate_edited_between_the_early_checks_and_the_claim_is_refused(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit lands after the request-start checks: the claim reads the row as it is, under lock."""
    _after_first_load(
        monkeypatch,
        lambda session: _core(
            session,
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(candidate=EDITED),
        ),
    )
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    _assert_payload_free(response)
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


@pytest.mark.parametrize("stored", [None, ""], ids=["null", "blank"])
def test_a_job_with_no_recorded_digest_is_refused_not_trusted(
    job: TestClient, db_session: Session, stored: str | None
) -> None:
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate_digest=stored)
    )
    db_session.commit()
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_UNVERIFIED
    assert response.json()["detail"] == APPLY_RECHECK_DETAIL[APPLY_CANDIDATE_UNVERIFIED]
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_UNVERIFIED)


def test_a_wrong_digest_for_unchanged_content_is_refused(
    job: TestClient, db_session: Session
) -> None:
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate_digest=candidate_digest(EDITED))
    )
    db_session.commit()
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


def test_an_emptied_candidate_with_a_recorded_digest_is_refused(
    job: TestClient, db_session: Session
) -> None:
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(candidate={})
    )
    db_session.commit()
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


def test_the_status_conflict_is_answered_before_the_digest_one(
    job: TestClient, db_session: Session
) -> None:
    """A job already past ``candidate_ready`` is a plain 409 about status, whatever its candidate looks like."""
    db_session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(status="applying", candidate=EDITED)
    )
    db_session.commit()
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert "reason_code" not in response.json()
    assert "'applying'" in response.json()["detail"]
    assert _refusals(db_session) == []


# ------------------------------------------------------------------ the current project permission


def _deactivate(session: Session) -> None:
    _core(
        session,
        update(CaliberProjectMember)
        .where(CaliberProjectMember.member_id == "M-other")
        .values(status="deactivated"),
    )


def _lower_to_viewer(session: Session) -> None:
    _core(
        session,
        update(CaliberProjectMember)
        .where(CaliberProjectMember.member_id == "M-other")
        .values(role=ROLE_VIEWER),
    )


def _remove(session: Session) -> None:
    _core(session, delete(CaliberProjectMember).where(CaliberProjectMember.member_id == "M-other"))


def _move_agent_to_another_project(session: Session) -> None:
    session.add(CaliberProject(project_id="P-elsewhere", name="Elsewhere", owner="@owner"))
    session.flush()
    _core(
        session,
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(project_id="P-elsewhere"),
    )


@pytest.mark.parametrize(
    "revoke",
    [_deactivate, _lower_to_viewer, _remove, _move_agent_to_another_project],
    ids=["deactivated", "lowered-to-viewer", "removed", "agent-moved-away"],
)
def test_a_permission_revoked_after_the_early_checks_is_refused_at_the_claim(
    project_job: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    revoke: Any,
) -> None:
    """No stale identity: the early check passed, then the grant went away; the claim answers from the current rows."""
    calls = _after_first_load(monkeypatch, revoke)
    response = _apply(project_job, OTHER, project=True)
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
    assert response.json()["detail"] == APPLY_RECHECK_DETAIL[APPLY_PERMISSION_REVOKED]
    _assert_payload_free(response)
    assert len(calls) == 2  # the early check, then the claim's own
    _assert_refused_and_nothing_changed(project_job, db_session, code=APPLY_PERMISSION_REVOKED)


def test_a_permission_that_is_still_held_at_the_claim_applies(
    project_job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck is not over-broad: an unrelated membership change does not block the claim."""
    _after_first_load(
        monkeypatch,
        lambda session: session.add(
            CaliberProjectMember(
                member_id="M-new",
                project_id=PROJECT_ID,
                user_id="@new",
                role=ROLE_VIEWER,
                created_by="@owner",
            )
        ),
    )
    assert _apply(project_job, OTHER, project=True).status_code == 200
    assert _job_status(db_session) == "applied"
    assert _refusals(db_session) == []


def test_permission_is_rechecked_before_separation(
    project_job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller whose grant is gone gets the permission refusal, not a separation code that would name the requester."""

    def requester_becomes_the_caller_and_the_grant_goes(session: Session) -> None:
        _core(
            session,
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(triggered_by=OTHER),
        )
        _deactivate(session)

    _after_first_load(monkeypatch, requester_becomes_the_caller_and_the_grant_goes)
    response = _apply(project_job, OTHER, project=True)
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
    _assert_refused_and_nothing_changed(project_job, db_session, code=APPLY_PERMISSION_REVOKED)


def test_a_job_deleted_after_the_early_checks_is_a_404_and_audits_nothing(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _after_first_load(
        monkeypatch,
        lambda session: _core(
            session, delete(CaliberRefinementJob).where(CaliberRefinementJob.job_id == JOB_ID)
        ),
    )
    response = _apply(job, OTHER)
    assert response.status_code == 404
    assert "reason_code" not in response.json()
    assert _refusals(db_session) == []
    assert job.app.state.promoter.calls == []  # type: ignore[attr-defined]


# ------------------------------------------------------------------ the separation, through the shared helper


@pytest.mark.parametrize(
    ("requester", "caller", "code"),
    [
        (REQUESTER, REQUESTER, APPLY_SELF_APPROVAL),
        (REQUESTER, REQUESTER.upper(), APPLY_SELF_APPROVAL),
        (None, OTHER, APPLY_CREATOR_UNKNOWN),
        ("  ", OTHER, APPLY_CREATOR_UNKNOWN),
    ],
    ids=["self", "self-case-variant", "unknown", "blank"],
)
def test_a_separation_refusal_is_audited_with_its_reason_code_only(
    job: TestClient, db_session: Session, requester: str | None, caller: str, code: str
) -> None:
    """#1050: the early refusal records who tried and why, never the requester or the candidate."""
    _configure(job, operator_users=f"{REQUESTER},{OTHER},{caller}")
    _set_requester(db_session, requester)
    response = _apply(job, caller)
    assert response.status_code == 403
    assert response.json()["reason_code"] == code
    _assert_refused_and_nothing_changed(job, db_session, code=code, actor=caller)


@pytest.mark.parametrize(
    ("rewritten", "code"),
    [(OTHER, APPLY_SELF_APPROVAL), (None, APPLY_CREATOR_UNKNOWN)],
    ids=["now-the-caller", "now-unknown"],
)
def test_a_requester_rewritten_after_the_early_check_is_refused_by_the_claim_recheck_and_audited(
    job: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    rewritten: str | None,
    code: str,
) -> None:
    _after_first_load(
        monkeypatch,
        lambda session: _core(
            session,
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(triggered_by=rewritten),
        ),
    )
    response = _apply(job, OTHER)
    assert response.status_code == 403
    assert response.json()["reason_code"] == code
    _assert_refused_and_nothing_changed(job, db_session, code=code)


def test_separation_refusals_use_the_shared_helper(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One implementation: the claim's recheck and the early check both call ``apply_separation_refusal``."""
    import caliber.routes.jobs as jobs_route

    seen: list[tuple[str | None, str]] = []
    real = jobs_route.apply_separation_refusal

    def spy(*, requester: str | None, approver: str) -> str | None:
        seen.append((requester, approver))
        return real(requester=requester, approver=approver)

    monkeypatch.setattr(jobs_route, "apply_separation_refusal", spy)
    assert _apply(job, OTHER).status_code == 200
    assert seen == [(REQUESTER, OTHER), (REQUESTER, OTHER)]  # the early check, then the claim's


def test_with_the_rule_off_a_legacy_job_still_applies_but_the_digest_is_still_checked(
    job: TestClient, db_session: Session
) -> None:
    _configure(job, apply_require_distinct_approver=False, profile="development")
    _set_requester(db_session, None)
    assert _apply(job, OTHER).status_code == 200


# ------------------------------------------------------------------ the conditional write


def test_the_claim_update_carries_the_digest_so_a_row_that_changes_under_it_matches_nothing(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth on dialects without row locks: a digest rewritten after the recheck cannot be claimed."""
    import caliber.routes.jobs as jobs_route

    real_recheck = jobs_route._recheck_claim
    calls: list[int] = []

    def recheck_then_rewrite(session: Session, **kwargs: Any) -> CaliberRefinementJob:
        calls.append(1)
        first = len(calls) == 1
        locked = real_recheck(session, **kwargs)
        if first:  # the gap between the recheck and the UPDATE
            _core(
                session,
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == JOB_ID)
                .values(candidate_digest=candidate_digest(EDITED)),
            )
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", recheck_then_rewrite)
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    assert len(calls) == 2
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


def test_a_candidate_edited_after_the_recheck_is_caught_before_it_is_snapshotted(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim UPDATE compares the digest column, not JSON content, so the snapshot is verified too.

    The edit lands after the recheck and before the claim: the UPDATE still matches (status and recorded digest are
    unchanged), and only the check of the content about to be snapshotted for the promoter stops it. Nothing was
    committed, so the claim itself is rolled back with the refusal.
    """
    import caliber.routes.jobs as jobs_route

    real_recheck = jobs_route._recheck_claim

    def recheck_then_edit(session: Session, **kwargs: Any) -> CaliberRefinementJob:
        locked = real_recheck(session, **kwargs)
        _core(
            session,
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(candidate=EDITED),
        )
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", recheck_then_edit)
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    _assert_payload_free(response)
    _assert_refused_and_nothing_changed(job, db_session, code=APPLY_CANDIDATE_CHANGED)


@pytest.mark.parametrize(
    "revoke",
    [_deactivate, _lower_to_viewer, _remove],
    ids=["deactivated", "lowered-to-viewer", "removed"],
)
def test_a_permission_revoked_between_the_recheck_and_the_claim_update_is_refused(
    project_job: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    revoke: Any,
) -> None:
    """Without row locks (SQLite) the grant can go away after the recheck; the check after the UPDATE catches it.

    The revocation lands after ``_recheck_claim`` returned and before the claim UPDATE. The UPDATE still matches, so only
    the authority check that runs once the UPDATE holds the write lock refuses, and the claim rolls back with it.
    """
    import caliber.routes.jobs as jobs_route

    real_recheck = jobs_route._recheck_claim

    def recheck_then_revoke(session: Session, **kwargs: Any) -> CaliberRefinementJob:
        locked = real_recheck(session, **kwargs)
        revoke(session)
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", recheck_then_revoke)
    response = _apply(project_job, OTHER, project=True)
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
    _assert_payload_free(response)
    _assert_refused_and_nothing_changed(project_job, db_session, code=APPLY_PERMISSION_REVOKED)


def test_a_revocation_committed_by_another_connection_right_after_the_recheck_is_refused(
    project_job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe that found the gap: a membership deactivation committed on another connection after the recheck.

    On SQLite nothing holds the membership row, so the recheck's answer is stale by the time the claim UPDATE runs; the
    authority check after the UPDATE reads the committed revocation and refuses before anything is committed.
    """
    import caliber.routes.jobs as jobs_route

    real_recheck = jobs_route._recheck_claim

    def recheck_then_revoke_elsewhere(session: Session, **kwargs: Any) -> CaliberRefinementJob:
        locked = real_recheck(session, **kwargs)
        db_session.execute(
            update(CaliberProjectMember)
            .where(CaliberProjectMember.member_id == "M-other")
            .values(status="deactivated")
        )
        db_session.commit()
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", recheck_then_revoke_elsewhere)
    response = _apply(project_job, OTHER, project=True)
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
    db_session.expire_all()
    member = db_session.get(CaliberProjectMember, "M-other")
    assert member is not None and member.status == "deactivated"
    _assert_refused_and_nothing_changed(project_job, db_session, code=APPLY_PERMISSION_REVOKED)


def _lock_timeout_error(sqlstate: str) -> OperationalError:
    class _OrigError(Exception):
        pass

    orig = _OrigError("canceling statement due to lock timeout")
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return OperationalError("SELECT ... FOR UPDATE", {}, orig)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_lock_timeout_error("55P03"), True),
        (_lock_timeout_error("40001"), False),
        (_lock_timeout_error(""), False),
        (RuntimeError("55P03"), False),
    ],
    ids=["lock-not-available", "serialization-failure", "blank", "not-a-database-error"],
)
def test_only_a_postgresql_lock_timeout_is_recognised(error: BaseException, expected: bool) -> None:
    assert is_lock_timeout(error) is expected


def test_pgcode_is_also_recognised() -> None:
    class _LegacyError(Exception):
        pgcode = "55P03"

    assert is_lock_timeout(OperationalError("x", {}, _LegacyError()))


def test_a_lock_timeout_in_the_claim_is_a_payload_free_503_with_nothing_written(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import caliber.routes.jobs as jobs_route

    def timed_out(*args: Any, **kwargs: Any) -> None:
        raise _lock_timeout_error("55P03")

    monkeypatch.setattr(jobs_route, "lock_job_and_authority", timed_out)
    response = _apply(job, OTHER)
    assert response.status_code == 503
    assert response.json()["reason_code"] == APPLY_BUSY
    assert response.json()["detail"] == APPLY_RECHECK_DETAIL[APPLY_BUSY]
    _assert_payload_free(response)
    assert _job_status(db_session) == "candidate_ready"
    assert job.app.state.promoter.calls == []  # type: ignore[attr-defined]
    assert _count(db_session, CaliberApprovalRequest) == 0
    assert _refusals(db_session) == []  # nothing was decided, so nothing is a refusal
    monkeypatch.undo()
    assert _apply(job, OTHER).status_code == 200  # retryable


def test_any_other_database_error_in_the_claim_is_not_swallowed(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import caliber.routes.jobs as jobs_route

    def broken(*args: Any, **kwargs: Any) -> None:
        raise _lock_timeout_error("40001")

    monkeypatch.setattr(jobs_route, "lock_job_and_authority", broken)
    with pytest.raises(OperationalError):
        _apply(job, OTHER)
    assert _job_status(db_session) == "candidate_ready"


def test_a_claim_that_matches_nothing_yet_rechecks_clean_is_still_refused(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the conditional write loses and the recheck then finds nothing wrong, the request is a plain 409, not a claim."""
    import caliber.routes.jobs as jobs_route

    real_recheck = jobs_route._recheck_claim
    calls: list[int] = []

    def flaky(session: Session, **kwargs: Any) -> CaliberRefinementJob:
        calls.append(1)
        n = len(calls)
        if n == 2:  # undo the rewrite below before the second recheck, so it passes
            _core(
                session,
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == JOB_ID)
                .values(candidate_digest=candidate_digest(ORIGINAL)),
            )
        locked = real_recheck(session, **kwargs)
        if n == 1:
            _core(
                session,
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == JOB_ID)
                .values(candidate_digest=candidate_digest(EDITED)),
            )
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", flaky)
    response = _apply(job, OTHER)
    assert response.status_code == 409
    assert "reason_code" not in response.json()
    assert "changed while being claimed" in response.json()["detail"]
    assert _job_status(db_session) == "candidate_ready"
    assert job.app.state.promoter.calls == []  # type: ignore[attr-defined]
    assert _count(db_session, CaliberApprovalRequest) == 0


# ------------------------------------------------------------------ the audit of a refusal


def test_a_refusal_that_cannot_be_audited_is_still_the_typed_refusal(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit row is evidence of the attempt, not part of the decision: a failing audit write never turns a 403 into a 500."""
    import caliber.routes.jobs as jobs_route

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(jobs_route, "audit_record", broken)
    response = _apply(job, REQUESTER)
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_SELF_APPROVAL
    assert _job_status(db_session) == "candidate_ready"
    assert _refusals(db_session) == []
    assert _count(db_session, CaliberApprovalRequest) == 0


def test_each_refused_attempt_is_recorded_and_none_changes_the_job(
    job: TestClient, db_session: Session
) -> None:
    for _ in range(3):
        assert _apply(job, REQUESTER).status_code == 403
    assert len(_refusals(db_session)) == 3
    assert _job_status(db_session) == "candidate_ready"
    assert _apply(job, OTHER).status_code == 200


def test_a_success_writes_no_refusal_row(job: TestClient, db_session: Session) -> None:
    assert _apply(job, OTHER).status_code == 200
    assert (
        db_session.execute(
            select(func.count())
            .select_from(CaliberAuditLog)
            .where(CaliberAuditLog.action == "apply_refused")
        ).scalar_one()
        == 0
    )


# ------------------------------------------------------------------ the lock helper


def test_the_lock_helper_returns_none_for_a_missing_job(db_session: Session) -> None:
    assert lock_job_and_authority(db_session, "RFN-NOPE", user_id="@x") is None


def test_the_lock_helper_drops_state_loaded_before_the_lock(
    project_job: TestClient, db_session: Session
) -> None:
    """A role or an agent project loaded earlier in the transaction must not survive into the recheck."""
    agent = db_session.get(CaliberAgentConfig, "support-agent")
    assert agent is not None and agent.project_id == PROJECT_ID
    member = db_session.get(CaliberProjectMember, "M-other")
    assert member is not None and member.role == ROLE_EDITOR
    _core(
        db_session,
        update(CaliberProjectMember)
        .where(CaliberProjectMember.member_id == "M-other")
        .values(role=ROLE_VIEWER),
    )
    _core(
        db_session,
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(project_id=None),
    )
    assert member.role == ROLE_EDITOR  # still the stale object
    locked = lock_job_and_authority(db_session, JOB_ID, user_id=OTHER)
    assert locked is not None and locked.job_id == JOB_ID
    assert member.role == ROLE_VIEWER
    assert agent.project_id is None


def test_the_lock_helper_tolerates_an_agentless_job(db_session: Session) -> None:
    """A legacy job whose agent row is gone has no project to lock; the visibility check decides afterwards."""
    _seed_candidate_ready_job(db_session)
    db_session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
    db_session.execute(
        delete(CaliberAgentConfig).where(CaliberAgentConfig.agent_id == "support-agent")
    )
    locked = lock_job_and_authority(db_session, JOB_ID, user_id="@x")
    assert locked is not None
