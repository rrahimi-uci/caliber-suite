"""The two-person Apply rule: a distinct approver for the reference profile (B06.3, #747).

``POST /jobs/{id}/apply`` refuses the account that requested the job (its stored ``triggered_by``) and a job with no
recorded requester when the rule is in force (``CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER``, and always in the reference
profile). Every case goes through the real route and inspects the persisted state and the promoter, not only the status.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.apply import (
    APPLY_CREATOR_UNKNOWN,
    APPLY_SELF_APPROVAL,
    APPLY_SEPARATION_DETAIL,
    apply_separation_refusal,
)
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberRefinementJob,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_VIEWER
from tests.test_routes_jobs_apply import APPLY_PATH, _seed_candidate_ready_job

JOB_ID = "RFN-A"
REQUESTER = "@requester"
OTHER = "@other-approver"
PROJECT_ID = "P-apply-distinct"


def _configure(client: TestClient, **updates: Any) -> None:
    config = client.app.state.config  # type: ignore[attr-defined]
    operators = {u for u in config.operator_users.split(",") if u} | {REQUESTER, OTHER, "@viewer"}
    client.app.state.config = config.model_copy(  # type: ignore[attr-defined]
        update={"operator_users": ",".join(sorted(operators)), **updates}
    )


def _make_agent_public(session: Session) -> None:
    """The seeded agent is a personal one; the callers below are not its owner, so make it visible to them."""
    session.execute(
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(visibility="public")
    )
    session.commit()


def _set_requester(session: Session, requester: str | None) -> None:
    session.execute(
        update(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == JOB_ID)
        .values(triggered_by=requester)
    )
    session.commit()


def _apply(client: TestClient, user: str, *, project: bool = False) -> Any:
    headers = {"X-CALIBER-User": user}
    if project:
        headers["X-CALIBER-Project"] = PROJECT_ID
    return client.post(APPLY_PATH.format(job_id=JOB_ID), headers=headers)


def _job_status(session: Session) -> str:
    session.expire_all()
    job = session.get(CaliberRefinementJob, JOB_ID)
    assert job is not None
    return job.status


def _count(session: Session, model: type) -> int:
    session.expire_all()
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_nothing_happened(client: TestClient, session: Session) -> None:
    """A refused Apply left the job claimable and caused no effect."""
    assert _job_status(session) == "candidate_ready"
    assert client.app.state.promoter.calls == []  # type: ignore[attr-defined]
    assert _count(session, CaliberApprovalRequest) == 0
    actions = set(session.execute(select(CaliberAuditLog.action)).scalars())
    assert not actions & {"apply_candidate", "promote", "apply_failed"}


@pytest.fixture
def job(client: TestClient, db_session: Session) -> TestClient:
    """A ``candidate_ready`` job requested by ``@requester`` and the rule switched on."""
    _seed_candidate_ready_job(db_session)
    _make_agent_public(db_session)
    _set_requester(db_session, REQUESTER)
    _configure(client, apply_require_distinct_approver=True)
    return client


# ------------------------------------------------------------------ the pure decision


@pytest.mark.parametrize(
    ("requester", "approver", "expected"),
    [
        ("@alice", "@bob", None),
        ("@alice", "@alice", APPLY_SELF_APPROVAL),
        ("@alice", "@ALICE", APPLY_SELF_APPROVAL),
        ("@alice", "  @alice\t", APPLY_SELF_APPROVAL),
        ("  @Alice ", "@alice", APPLY_SELF_APPROVAL),
        ("@alice", "", APPLY_SELF_APPROVAL),
        ("@alice", "   ", APPLY_SELF_APPROVAL),
        (None, "@bob", APPLY_CREATOR_UNKNOWN),
        ("", "@bob", APPLY_CREATOR_UNKNOWN),
        ("   ", "@bob", APPLY_CREATOR_UNKNOWN),
        (None, "", APPLY_CREATOR_UNKNOWN),
    ],
)
def test_the_separation_decision(
    requester: str | None, approver: str, expected: str | None
) -> None:
    assert apply_separation_refusal(requester=requester, approver=approver) == expected


def test_every_refusal_has_a_static_message_without_identifiers() -> None:
    assert set(APPLY_SEPARATION_DETAIL) == {APPLY_SELF_APPROVAL, APPLY_CREATOR_UNKNOWN}
    for message in APPLY_SEPARATION_DETAIL.values():
        assert "@" not in message and "RFN" not in message


# ------------------------------------------------------------------ the rule on


def test_a_different_account_can_apply(job: TestClient, db_session: Session) -> None:
    response = _apply(job, OTHER)
    assert response.status_code == 200, response.text
    assert _job_status(db_session) == "applied"
    assert len(job.app.state.promoter.calls) == 1  # type: ignore[attr-defined]
    approval = db_session.query(CaliberApprovalRequest).one()
    assert approval.approved_by == OTHER


def test_the_requester_cannot_apply_their_own_candidate(
    job: TestClient, db_session: Session
) -> None:
    response = _apply(job, REQUESTER)
    assert response.status_code == 403
    error = response.json()
    assert error["reason_code"] == APPLY_SELF_APPROVAL
    assert error["detail"] == APPLY_SEPARATION_DETAIL[APPLY_SELF_APPROVAL]
    # Payload-free: neither account, the job id nor any candidate text is in the response.
    for secret in (REQUESTER, OTHER, JOB_ID, "rewritten prompt body"):
        assert secret not in response.text
    _assert_nothing_happened(job, db_session)


def test_the_refusal_is_recoverable_by_a_second_person(
    job: TestClient, db_session: Session
) -> None:
    assert _apply(job, REQUESTER).status_code == 403
    assert _apply(job, OTHER).status_code == 200
    assert _job_status(db_session) == "applied"
    assert len(job.app.state.promoter.calls) == 1  # type: ignore[attr-defined]


def test_a_case_variant_of_the_requester_is_not_a_second_person(
    job: TestClient, db_session: Session
) -> None:
    _configure(job, operator_users="@REQUESTER")
    response = _apply(job, "@REQUESTER")
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_SELF_APPROVAL
    _assert_nothing_happened(job, db_session)


@pytest.mark.parametrize("requester", [None, "", "   "], ids=["null", "empty", "blank"])
def test_a_job_with_no_recorded_requester_is_refused_never_passed(
    job: TestClient, db_session: Session, requester: str | None
) -> None:
    _set_requester(db_session, requester)
    response = _apply(job, OTHER)
    assert response.status_code == 403
    error = response.json()
    assert error["reason_code"] == APPLY_CREATOR_UNKNOWN
    assert error["detail"] == APPLY_SEPARATION_DETAIL[APPLY_CREATOR_UNKNOWN]
    _assert_nothing_happened(job, db_session)


def test_a_forged_creator_in_the_request_cannot_change_the_outcome(
    job: TestClient, db_session: Session
) -> None:
    """The requester is the stored column; a body, query or header that names someone else is ignored."""
    response = job.post(
        APPLY_PATH.format(job_id=JOB_ID) + "?triggered_by=@someone-else",
        headers={"X-CALIBER-User": REQUESTER, "X-CALIBER-Triggered-By": "@someone-else"},
        json={"triggered_by": "@someone-else", "requester": "@someone-else"},
    )
    assert response.status_code == 403
    assert response.json()["reason_code"] == APPLY_SELF_APPROVAL
    _assert_nothing_happened(job, db_session)


def test_a_forged_approver_cannot_make_the_requester_distinct(
    job: TestClient, db_session: Session
) -> None:
    response = job.post(
        APPLY_PATH.format(job_id=JOB_ID),
        headers={"X-CALIBER-User": REQUESTER},
        json={"approved_by": OTHER, "actor": OTHER},
    )
    assert response.status_code == 403
    _assert_nothing_happened(job, db_session)


def test_a_missing_job_is_still_404_and_a_wrong_state_still_409(
    job: TestClient, db_session: Session
) -> None:
    assert (
        job.post(
            APPLY_PATH.format(job_id="RFN-NONE"), headers={"X-CALIBER-User": OTHER}
        ).status_code
        == 404
    )
    assert _apply(job, OTHER).status_code == 200
    again = _apply(job, OTHER)
    assert again.status_code == 409
    assert "reason_code" not in again.json()


# ------------------------------------------------------------------ the rule off, and the reference profile


def test_flag_off_keeps_the_development_behaviour(client: TestClient, db_session: Session) -> None:
    """Declared development-only compatibility: the requester may apply their own candidate."""
    _seed_candidate_ready_job(db_session)
    _make_agent_public(db_session)
    _set_requester(db_session, REQUESTER)
    _configure(client, apply_require_distinct_approver=False, profile="development")
    assert _apply(client, REQUESTER).status_code == 200
    assert _job_status(db_session) == "applied"


def test_flag_off_still_applies_a_legacy_job_with_no_requester(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    _make_agent_public(db_session)
    _configure(client, apply_require_distinct_approver=False, profile="development")
    assert _apply(client, OTHER).status_code == 200
    assert _job_status(db_session) == "applied"


@pytest.mark.parametrize("flag", [True, False], ids=["flag-on", "flag-off"])
def test_the_reference_profile_enforces_the_rule_whatever_the_flag_says(
    client: TestClient, db_session: Session, flag: bool
) -> None:
    _seed_candidate_ready_job(db_session)
    _make_agent_public(db_session)
    _set_requester(db_session, REQUESTER)
    _configure(client, profile="reference", apply_require_distinct_approver=flag)
    refused = _apply(client, REQUESTER)
    assert refused.status_code == 403
    assert refused.json()["reason_code"] == APPLY_SELF_APPROVAL
    _assert_nothing_happened(client, db_session)
    _set_requester(db_session, None)
    unknown = _apply(client, OTHER)
    assert unknown.status_code == 403
    assert unknown.json()["reason_code"] == APPLY_CREATOR_UNKNOWN
    _assert_nothing_happened(client, db_session)
    _set_requester(db_session, REQUESTER)
    assert _apply(client, OTHER).status_code == 200


# ------------------------------------------------------------------ project permission still comes first


@pytest.fixture
def project_job(client: TestClient, db_session: Session) -> TestClient:
    """The job belongs to a project: ``@requester`` and ``@other-approver`` are editors, ``@viewer`` a viewer."""
    _seed_candidate_ready_job(db_session)
    db_session.add(CaliberProject(project_id=PROJECT_ID, name="Apply distinct", owner="@owner"))
    db_session.commit()
    for user, role in ((REQUESTER, ROLE_EDITOR), (OTHER, ROLE_EDITOR), ("@viewer", ROLE_VIEWER)):
        db_session.add(
            CaliberProjectMember(
                member_id=f"M-{user.lstrip('@')}",
                project_id=PROJECT_ID,
                user_id=user,
                role=role,
                created_by="@owner",
            )
        )
    db_session.execute(
        update(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == "support-agent")
        .values(project_id=PROJECT_ID, visibility="project")
    )
    db_session.commit()
    _set_requester(db_session, REQUESTER)
    _configure(client, apply_require_distinct_approver=True)
    return client


def test_a_non_member_is_404_before_any_separation_answer(
    project_job: TestClient, db_session: Session
) -> None:
    _configure(project_job, operator_users="@stranger,@requester,@other-approver,@viewer")
    response = _apply(project_job, "@stranger", project=True)
    assert response.status_code == 404
    assert "reason_code" not in response.json()
    _assert_nothing_happened(project_job, db_session)


def test_a_viewer_requester_gets_the_role_refusal_not_a_separation_code(
    project_job: TestClient, db_session: Session
) -> None:
    """The requester here is a viewer: the 403 is the missing role, so the response does not confirm who requested."""
    _set_requester(db_session, "@viewer")
    response = _apply(project_job, "@viewer", project=True)
    assert response.status_code == 403
    assert "reason_code" not in response.json()
    _assert_nothing_happened(project_job, db_session)


def test_a_member_editor_who_is_distinct_applies(
    project_job: TestClient, db_session: Session
) -> None:
    refused = _apply(project_job, REQUESTER, project=True)
    assert refused.status_code == 403
    assert refused.json()["reason_code"] == APPLY_SELF_APPROVAL
    assert _apply(project_job, OTHER, project=True).status_code == 200
    assert _job_status(db_session) == "applied"


# ------------------------------------------------------------------ the claim is atomic with the check


@pytest.mark.parametrize(
    ("rewritten", "code"),
    [
        (OTHER, APPLY_SELF_APPROVAL),
        (OTHER.upper(), APPLY_SELF_APPROVAL),
        (f"  {OTHER}  ", APPLY_SELF_APPROVAL),
        (f"\t{OTHER.upper()} ", APPLY_SELF_APPROVAL),
        ("", APPLY_CREATOR_UNKNOWN),
        ("   ", APPLY_CREATOR_UNKNOWN),
        (None, APPLY_CREATOR_UNKNOWN),
    ],
    ids=["exact", "upper", "padded", "padded-upper", "empty", "blank", "null"],
)
def test_a_requester_rewritten_after_the_check_cannot_claim(
    job: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    rewritten: str | None,
    code: str,
) -> None:
    """The route's up-front check read a distinct requester; the row then changed before the claim.

    The claim UPDATE carries the same normalised separation (trim and lower-case on both sides; NULL or blank
    matches nothing), so it matches no row: the route answers the typed refusal from the *current* row and nothing
    is claimed, approved or promoted. An exact-only predicate would let a case or whitespace variant of the caller
    claim.
    """
    import caliber.routes.jobs as jobs_route

    real_load = jobs_route.load_visible_job

    def load_then_rewrite(session: Session, *args: Any, **kwargs: Any) -> CaliberRefinementJob:
        loaded = real_load(session, *args, **kwargs)
        # Rewritten in the caller's own transaction, without refreshing the object the check will read.
        session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(triggered_by=rewritten)
            .execution_options(synchronize_session=False)
        )
        assert loaded.triggered_by == REQUESTER  # the stale value the up-front check will see
        return loaded

    monkeypatch.setattr(jobs_route, "load_visible_job", load_then_rewrite)
    response = _apply(job, OTHER)
    assert response.status_code == 403
    assert response.json()["reason_code"] == code
    _assert_nothing_happened(job, db_session)


def test_a_distinct_rewritten_requester_still_claims(
    job: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normalised predicate is not over-broad: a different account's variant does not block the claim."""
    import caliber.routes.jobs as jobs_route

    real_load = jobs_route.load_visible_job

    def load_then_rewrite(session: Session, *args: Any, **kwargs: Any) -> CaliberRefinementJob:
        loaded = real_load(session, *args, **kwargs)
        session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == JOB_ID)
            .values(triggered_by="  @Someone-Else ")
            .execution_options(synchronize_session=False)
        )
        return loaded

    monkeypatch.setattr(jobs_route, "load_visible_job", load_then_rewrite)
    assert _apply(job, OTHER).status_code == 200
    assert _job_status(db_session) == "applied"


# ------------------------------------------------------------------ a missing config fails closed


@pytest.mark.parametrize(
    ("requester", "code"),
    [(REQUESTER, APPLY_SELF_APPROVAL), (None, APPLY_CREATOR_UNKNOWN)],
    ids=["self", "unknown-requester"],
)
def test_a_missing_app_config_fails_closed_not_500(
    job: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    requester: str | None,
    code: str,
) -> None:
    """With no ``app.state.config`` the route applies the rule (as the reference profile) instead of raising.

    The HTTP stack needs the config to authenticate, so the handler is called directly with the resolved identity
    stubbed and an app state that has no ``config`` attribute.
    """
    import asyncio
    from types import SimpleNamespace

    import caliber.routes.jobs as jobs_route
    from caliber.auth import SCOPE_OPERATOR, CaliberIdentity
    from caliber.routes._errors import CaliberHTTPException

    _set_requester(db_session, requester)
    real_state = job.app.state  # type: ignore[attr-defined]
    state = SimpleNamespace(
        session_factory=real_state.session_factory, promoter=real_state.promoter
    )
    assert not hasattr(state, "config")
    request = SimpleNamespace(
        app=SimpleNamespace(state=state), path_params={"job_id": JOB_ID}, headers={}
    )
    identity = CaliberIdentity(user_id=REQUESTER, scopes=frozenset({SCOPE_OPERATOR}))
    monkeypatch.setattr(jobs_route, "require_scopes", lambda *_a, **_k: REQUESTER)
    monkeypatch.setattr(jobs_route, "resolve_identity", lambda *_a, **_k: identity)
    with pytest.raises(CaliberHTTPException) as refused:
        asyncio.run(jobs_route.apply_job(request))  # type: ignore[arg-type]
    assert refused.value.status_code == 403
    assert refused.value.reason_code == code
    _assert_nothing_happened(job, db_session)
