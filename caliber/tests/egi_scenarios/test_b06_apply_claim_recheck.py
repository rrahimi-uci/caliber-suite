"""B06.4 (#753) public path: the Apply claim rechecks permission, separation and the exact candidate, race-safely.

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile). Two things are proved:

* **What is rechecked.** A candidate edited after the eval gate cleared is refused (typed, payload-free), as is a caller
  whose project grant went away. Both refusals leave the job ``candidate_ready`` with no approval anchor, no promotion
  and exactly one ``apply_refused`` audit row.
* **That there is no gap.** On PostgreSQL each race is made real with independent connections and clients: a revocation
  or a candidate edit that is mid-transaction when Apply reaches its claim makes the claim *wait* for it and then refuse,
  and one that arrives while the claim is in flight waits for the claim, so the apply that passed its checks is the apply
  that was authorised. Waiting is observed in ``pg_stat_activity``, not assumed. Each race client is its own
  ``TestClient`` (own event loop) because the shared fixture client serialises requests on one loop.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from sqlalchemy import select, text, update
from starlette.testclient import TestClient

from caliber.apply import APPLY_CANDIDATE_CHANGED, APPLY_PERMISSION_REVOKED
from caliber.candidate_digest import candidate_digest
from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberProjectMember,
    CaliberRefinementJob,
)
from tests.egi_scenarios.conftest import EDITOR_A, EDITOR_B, ReferenceApp, Workspaces
from tests.egi_scenarios.test_b06_apply_distinct_approver import (
    CANDIDATE,
    PREFIX,
    _apply,
    _headers,
    _make_editor_b_a_member_of_project_a,
    _promotions,
    _seed_job,
    _state,
)

EDITED = {**CANDIDATE, "content": "b064 EDITED after the gate"}
MEMBER_ID = "egi-m-editor-b-in-a"
WAIT_SECONDS = 20.0


def _require_postgresql(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip("a cross-connection lock race needs PostgreSQL row locks")


def _refusals(app: ReferenceApp, job_id: str) -> list[CaliberAuditLog]:
    with app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberAuditLog).where(
                    CaliberAuditLog.action == "apply_refused", CaliberAuditLog.entity_id == job_id
                )
            )
            .scalars()
            .all()
        )


def _assert_refused_cleanly(app: ReferenceApp, job_id: str, *, code: str, actor: str) -> None:
    assert _state(app, job_id) == ("candidate_ready", 0)
    assert _promotions(app) == 0
    (row,) = _refusals(app, job_id)
    assert row.actor == actor
    assert row.details == {"reason_code": code}
    assert row.severity == "high"


def _wait_until_blocked(app: ReferenceApp, *needles: str) -> None:
    """Block until some backend is waiting on a lock while running a statement containing every needle."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with app.session_factory() as session:
            queries = (
                session.execute(
                    text(
                        "SELECT query FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
                .scalars()
                .all()
            )
        if any(all(n.lower() in q.lower() for n in needles) for q in queries):
            return
        time.sleep(0.05)
    raise AssertionError(f"nothing was ever blocked on a lock running {needles!r}; saw {queries!r}")


class _Request:
    """One Apply on its own thread, client and event loop."""

    def __init__(
        self, app: ReferenceApp, user: str, job_id: str, barrier: threading.Barrier | None = None
    ) -> None:
        self._barrier = barrier
        self.response: Any = None
        self.error: BaseException | None = None
        self._headers = _headers(app, user)
        self._client = TestClient(app.client.app)  # no ``with``: a fresh loop per request
        self._path = f"{PREFIX}/jobs/{job_id}/apply"
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            if self._barrier is not None:
                self._barrier.wait()
            self.response = self._client.post(self._path, headers=self._headers)
        except BaseException as exc:
            self.error = exc

    def start(self) -> _Request:
        self.thread.start()
        return self

    def result(self) -> Any:
        self.thread.join(timeout=WAIT_SECONDS * 2)
        assert not self.thread.is_alive(), "the apply request never finished"
        assert self.error is None, self.error
        return self.response


def _revoke_editor_b(session: Any) -> None:
    session.execute(
        update(CaliberProjectMember)
        .where(CaliberProjectMember.member_id == MEMBER_ID)
        .values(status="deactivated")
    )


def _hold_the_claim(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Make Apply pause once its claim recheck has passed, still holding the locks it took.

    Returns ``(in_claim, release)``: the first is set when the claim is paused, the second lets it go on.
    """
    import caliber.routes.jobs as jobs_route

    in_claim, release = threading.Event(), threading.Event()
    real = jobs_route._recheck_claim

    def paused(session: Any, **kwargs: Any) -> CaliberRefinementJob:
        locked = real(session, **kwargs)
        in_claim.set()
        assert release.wait(WAIT_SECONDS), "the claim was never released"
        return locked

    monkeypatch.setattr(jobs_route, "_recheck_claim", paused)
    return in_claim, release


# ------------------------------------------------------------------ both dialects


def test_a_candidate_edited_after_the_gate_is_refused_and_leaves_no_effect(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-EDIT", EDITOR_A)
    with reference_app.session_factory() as session:
        session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == "RFN-B064-EDIT")
            .values(candidate=EDITED)
        )
        session.commit()
    refused = _apply(reference_app, EDITOR_B, "RFN-B064-EDIT")
    assert refused.status_code == 409
    assert refused.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    assert EDITED["content"] not in refused.text and "sha256:" not in refused.text
    _assert_refused_cleanly(
        reference_app, "RFN-B064-EDIT", code=APPLY_CANDIDATE_CHANGED, actor=EDITOR_B
    )


def test_a_grant_revoked_before_apply_is_refused_with_the_project_permission_check(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The baseline: a deactivated member never reaches the claim (404, the existing early check)."""
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-GONE", EDITOR_A)
    with reference_app.session_factory() as session:
        _revoke_editor_b(session)
        session.commit()
    assert _apply(reference_app, EDITOR_B, "RFN-B064-GONE").status_code == 404
    assert _state(reference_app, "RFN-B064-GONE") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 0


def test_the_applied_content_is_the_content_the_gate_cleared(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-OK", EDITOR_A)
    assert _apply(reference_app, EDITOR_B, "RFN-B064-OK").status_code == 200
    assert _state(reference_app, "RFN-B064-OK") == ("applied", 1)
    calls = reference_app.client.app.state.promoter.calls  # type: ignore[attr-defined]
    assert [c.new_content for c in calls] == [CANDIDATE["content"]]
    with reference_app.session_factory() as session:
        job = session.get(CaliberRefinementJob, "RFN-B064-OK")
        approval = session.execute(
            select(CaliberApprovalRequest).where(CaliberApprovalRequest.job_id == "RFN-B064-OK")
        ).scalar_one()
        assert job is not None
        assert job.candidate_digest == candidate_digest(CANDIDATE)
        assert candidate_digest(approval.candidate_snapshot) == job.candidate_digest
    assert _refusals(reference_app, "RFN-B064-OK") == []


# ------------------------------------------------------------------ PostgreSQL: the claim waits for what races it


def test_apply_waits_for_an_uncommitted_revocation_and_then_refuses(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The grant is being revoked (not yet committed) when Apply reaches its claim: Apply waits, sees it, refuses.

    The early check reads the committed (still active) membership and passes; without a lock on the membership row
    the claim would read the same and proceed, then the revocation would commit after a promotion it was meant to stop.
    """
    _require_postgresql(reference_app)
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-REVOKE", EDITOR_A)
    with reference_app.session_factory() as revoker:
        _revoke_editor_b(revoker)
        revoker.flush()  # the row lock is now held, the revocation is not committed
        apply = _Request(reference_app, EDITOR_B, "RFN-B064-REVOKE").start()
        _wait_until_blocked(reference_app, "caliber_project_members", "FOR SHARE")
        assert apply.thread.is_alive()  # waiting for the revocation, not racing past it
        revoker.commit()
    response = apply.result()
    assert response.status_code == 403, response.text
    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
    _assert_refused_cleanly(
        reference_app, "RFN-B064-REVOKE", code=APPLY_PERMISSION_REVOKED, actor=EDITOR_B
    )


def test_a_revocation_that_arrives_mid_claim_waits_for_the_claim_it_raced(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim has passed its checks and holds the locks: the revocation waits, so the claim stays authorised.

    One allowed apply (it was authorised at the instant it claimed); the revocation then takes effect, and the next
    apply by the same caller is refused: zero unauthorized mutation after the revocation.
    """
    _require_postgresql(reference_app)
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-MIDCLAIM", EDITOR_A)
    _seed_job(reference_app, "RFN-B064-AFTER", EDITOR_A)
    in_claim, release = _hold_the_claim(monkeypatch)
    apply = _Request(reference_app, EDITOR_B, "RFN-B064-MIDCLAIM").start()
    assert in_claim.wait(WAIT_SECONDS)

    def revoke() -> None:
        with reference_app.session_factory() as session:
            _revoke_editor_b(session)
            session.commit()

    revoker = threading.Thread(target=revoke, daemon=True)
    revoker.start()
    _wait_until_blocked(reference_app, "update caliber_project_members")
    assert revoker.is_alive()  # held back by the claim's lock on the membership row
    release.set()
    first = apply.result()
    revoker.join(timeout=WAIT_SECONDS)
    assert not revoker.is_alive()
    assert first.status_code == 200, first.text
    assert _state(reference_app, "RFN-B064-MIDCLAIM") == ("applied", 1)
    assert _promotions(reference_app) == 1
    # From here the grant is gone: nothing more can be applied by that caller.
    assert _apply(reference_app, EDITOR_B, "RFN-B064-AFTER").status_code == 404
    assert _state(reference_app, "RFN-B064-AFTER") == ("candidate_ready", 0)
    assert _promotions(reference_app) == 1


def test_apply_waits_for_an_uncommitted_candidate_edit_and_then_refuses(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """A candidate edit is in flight when Apply reaches its claim: Apply waits for it and refuses the edited content.

    Without the row lock the claim's UPDATE would wait for the edit, then match (the status and the recorded digest
    did not change) and promote content nobody evaluated.
    """
    _require_postgresql(reference_app)
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-RACEEDIT", EDITOR_A)
    with reference_app.session_factory() as editor:
        editor.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == "RFN-B064-RACEEDIT")
            .values(candidate=EDITED)
        )
        editor.flush()
        apply = _Request(reference_app, EDITOR_B, "RFN-B064-RACEEDIT").start()
        # The SELECT lists every job column, so ``pg_stat_activity`` truncates it before ``FOR UPDATE``; only the
        # apply request reads this table while the edit is open, and it is the one waiting.
        _wait_until_blocked(reference_app, "select caliber_refinement_jobs.job_id")
        assert apply.thread.is_alive()
        editor.commit()
    response = apply.result()
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == APPLY_CANDIDATE_CHANGED
    assert EDITED["content"] not in response.text
    _assert_refused_cleanly(
        reference_app, "RFN-B064-RACEEDIT", code=APPLY_CANDIDATE_CHANGED, actor=EDITOR_B
    )


def test_a_candidate_edit_that_arrives_mid_claim_cannot_change_what_is_promoted(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim holds the job row: an edit waits for it, and the promotion is of the evaluated content."""
    _require_postgresql(reference_app)
    _make_editor_b_a_member_of_project_a(reference_app)
    _seed_job(reference_app, "RFN-B064-MIDEDIT", EDITOR_A)
    in_claim, release = _hold_the_claim(monkeypatch)
    apply = _Request(reference_app, EDITOR_B, "RFN-B064-MIDEDIT").start()
    assert in_claim.wait(WAIT_SECONDS)

    def edit() -> None:
        with reference_app.session_factory() as session:
            session.execute(
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == "RFN-B064-MIDEDIT")
                .values(candidate=EDITED)
            )
            session.commit()

    editor = threading.Thread(target=edit, daemon=True)
    editor.start()
    _wait_until_blocked(reference_app, "update caliber_refinement_jobs")
    assert editor.is_alive()
    release.set()
    response = apply.result()
    editor.join(timeout=WAIT_SECONDS)
    assert response.status_code == 200, response.text
    calls = reference_app.client.app.state.promoter.calls  # type: ignore[attr-defined]
    assert [c.new_content for c in calls] == [CANDIDATE["content"]]
    with reference_app.session_factory() as session:
        approval = session.execute(
            select(CaliberApprovalRequest).where(
                CaliberApprovalRequest.job_id == "RFN-B064-MIDEDIT"
            )
        ).scalar_one()
    assert approval.candidate_snapshot["content"] == CANDIDATE["content"]


def test_racing_applies_and_a_revocation_never_promote_without_authority(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """Two applies and a revocation released together, five rounds: at most one promotion, always an authorised one.

    Each round the outcome is one of two interleavings (apply first, then the revocation; or the revocation first), and
    the persisted state must match the response: a 200 means exactly one promotion and one approval anchor; otherwise
    nothing at all happened, every refusal is typed, and the job is untouched.
    """
    _require_postgresql(reference_app)
    _make_editor_b_a_member_of_project_a(reference_app)
    promoted_total = 0
    for round_number in range(5):
        job_id = f"RFN-B064-RACE{round_number}"
        _seed_job(reference_app, job_id, EDITOR_A)
        with reference_app.session_factory() as session:
            session.execute(
                update(CaliberProjectMember)
                .where(CaliberProjectMember.member_id == MEMBER_ID)
                .values(status="active")
            )
            session.commit()
        barrier = threading.Barrier(3)
        applies = [_Request(reference_app, EDITOR_B, job_id, barrier) for _ in range(2)]

        def revoke(barrier: threading.Barrier = barrier) -> None:
            with reference_app.session_factory() as session:
                barrier.wait()
                _revoke_editor_b(session)
                session.commit()

        revoker = threading.Thread(target=revoke, daemon=True)
        revoker.start()
        for request in applies:
            request.start()
        responses = [request.result() for request in applies]
        revoker.join(timeout=WAIT_SECONDS)
        codes = sorted(r.status_code for r in responses)
        status, approvals = _state(reference_app, job_id)
        assert codes.count(200) <= 1, codes
        for response in responses:
            if response.status_code != 200:
                assert response.status_code in {403, 404, 409}, response.text
                if response.status_code == 403:
                    assert response.json()["reason_code"] == APPLY_PERMISSION_REVOKED
        if 200 in codes:
            assert (status, approvals) == ("applied", 1)
            promoted_total += 1
        else:
            assert (status, approvals) == ("candidate_ready", 0), (codes, status)
    assert _promotions(reference_app) == promoted_total
