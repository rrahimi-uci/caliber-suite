# ruff: noqa: F811 - the world fixture is the one imported from the governed-worker scenarios
"""V11 / B09.2b (#791): a downstream's 2xx is not success; a run completes only when the persisted decision accepts it.

The reference-profile app and the real downstream (the EGI scenarios' disposable system of record), driven by the worker the way
a queued run is: a scripted model proposes the protected action, a different person approves it, the operator resumes the run,
the dispatcher sends it exactly once and reads the receipt back by key. **Then, as the run would complete, the platform reads the
downstream's record of the case itself and judges it** (``caliber.acceptance.completion``). Every case asserts what is
**persisted** (the run, its summary, the decision rows, the audit log) and what the **public API** serves, and counts mutations at
the downstream:

* the downstream committed and the case record confirms it: the run ``completed``, one ``accepted`` decision, the API names it;
* the downstream answered 2xx and committed, but the record the platform reads shows the **wrong state** (a stale version, another
  case, the effect gone, an unreadable answer): the run is ``failed`` with ``acceptance_rejected``, never ``completed``, with a
  recorded ``rejected`` decision and no retry of the effect;
* a different verdict already stored under the same key is refused and **audited**, and the run fails closed;
* a criterion that breaks fails the run closed with nothing recorded as a verdict;
* the decision is idempotent across a repeat (restart/replay) and across concurrent workers: one row.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from caliber.acceptance import Observations
from caliber.acceptance import completion as completion_module
from caliber.acceptance.completion import CaseAnswer, evaluate_run
from caliber.acceptance.decision import observation_digest
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberAuditLog,
    CaliberEffectReceipt,
    CaliberWorkflowRun,
)
from caliber.effects.callbacks import parse_bindings
from caliber.effects.reconcile import targets_from_bindings
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b05_governed_worker_wiring import (
    CASE,
    DOCUMENT,
    Owner,
    _bindings,
    admit_run,
    bound_requests,
    build_worker,
    decide,
    proposals,
    resume,
    run_row,
    world,  # noqa: F401 - the fixture
)


class Reading:
    """The platform's read of the case: the real executor against the real downstream, optionally altered on the way back."""

    def __init__(
        self,
        *,
        alter: Callable[[CaseAnswer], CaseAnswer] | None = None,
        before: Callable[[], None] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.alter, self.before, self.fail = alter, before, fail
        self.calls = 0

    def __call__(self, base_url: str, case_id: str) -> CaseAnswer:
        self.calls += 1
        if self.before is not None:
            self.before()
        if self.fail is not None:
            raise self.fail
        answer = completion_module.read_case(base_url, case_id)
        return self.alter(answer) if self.alter is not None else answer


def drive(
    app: ReferenceApp,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[str, str], CaseAnswer],
) -> str:
    """Admit a run, approve and resume it, and let the worker dispatch it and try to complete it; returns the run id."""
    owner = Owner(app)
    owner.take()
    run_id = admit_run(app, downstream)
    worker = build_worker(
        app, downstream, monkeypatch, lease_provider=owner.provider, case_reader=reader
    )
    worker._tick()
    assert run_row(app, run_id).status == "waiting_approval"
    (request,) = bound_requests(app)
    assert decide(app, run_id, "approve", request.runtime_approval_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    worker._tick()
    return run_id


def decisions(app: ReferenceApp) -> list[CaliberAcceptanceDecision]:
    with app.session_factory() as session:
        rows = list(
            session.execute(
                select(CaliberAcceptanceDecision).order_by(CaliberAcceptanceDecision.decision_id)
            ).scalars()
        )
        session.expunge_all()
        return rows


def public_run(app: ReferenceApp, run_id: str) -> dict[str, Any]:
    response = _call(app, "GET", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def audit_entries(app: ReferenceApp, action: str) -> list[CaliberAuditLog]:
    with app.session_factory() as session:
        rows = list(
            session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == action)
            ).scalars()
        )
        session.expunge_all()
        return rows


def _stale(answer: CaseAnswer) -> CaseAnswer:
    """The case as an effect that did not advance it would leave it: every listed request carries the old version."""
    body = copy.deepcopy(answer.body)
    for request in body["document_requests"]:
        request["case_version"] = 1
    return CaseAnswer(answer.status_code, body)


def _another_case(answer: CaseAnswer) -> CaseAnswer:
    body = copy.deepcopy(answer.body)
    body["case_id"] = "MORT-2026-999999"
    return CaseAnswer(answer.status_code, body)


def _assert_rejected(app: ReferenceApp, downstream: Downstream, run_id: str) -> None:
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("failed", "acceptance_rejected"), (
        run.error_code,
        run.error_summary,
    )
    assert run.completed_at is not None
    assert downstream.mutations() == 1, "the effect was sent exactly once and never again"
    (proposal,) = proposals(app)
    assert proposal.state == "committed", (
        "the downstream did commit: the platform judged it, it did not undo it"
    )
    (row,) = decisions(app)
    assert (row.outcome, row.candidate_id, row.workflow_run_id) == (
        "rejected",
        proposal.proposal_id,
        run_id,
    )
    public = public_run(app, run_id)
    assert public["status"] == "failed" and public["error_code"] == "acceptance_rejected"
    assert public["acceptance"]["status"] == "rejected"
    assert [d["decision_id"] for d in public["acceptance"]["decisions"]] == [row.decision_id]


def test_a_committed_effect_the_case_record_confirms_completes_the_run_with_an_accepted_decision(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    reading = Reading()

    run_id = drive(app, downstream, monkeypatch, reading)

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("completed", None), (run.error_code, run.error_summary)
    assert downstream.mutations() == 1 and reading.calls == 1
    (proposal,) = proposals(app)
    (row,) = decisions(app)
    assert (row.outcome, row.candidate_id, row.criteria_version) == (
        "accepted",
        proposal.proposal_id,
        "harborlight-v1",
    )
    assert (row.criteria_total, row.criteria_failed) == (2, 0)
    public = public_run(app, run_id)
    assert public["status"] == "completed"
    assert public["acceptance"] == {
        "status": "accepted",
        "criteria_version": "harborlight-v1",
        "error_code": None,
        "decisions": [
            {
                "decision_id": row.decision_id,
                "candidate_id": proposal.proposal_id,
                "outcome": "accepted",
                "observation_digest": row.observation_digest,
                "failed_criteria": [],
            }
        ],
    }
    # the observation judged is the one the platform built from its own records and its own read of the downstream
    with app.session_factory() as session:
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
    case = downstream.client.get(f"/cases/{CASE}").json()
    assert row.observation_digest == observation_digest(
        Observations(
            {
                "proposal": {"case_id": CASE, "document_type": DOCUMENT, "expected_version": 1},
                "receipt": {"downstream_request_id": receipt.downstream_request_id},
                "case_record": {"case_id": CASE, "document_requests": case["document_requests"]},
            }
        )
    )


def test_wrong_state_fails_acceptance(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V11: the downstream answered 2xx and committed, the receipt reads back, and the recorded state is still wrong."""
    app, downstream = world

    run_id = drive(app, downstream, monkeypatch, Reading(alter=_stale))

    _assert_rejected(app, downstream, run_id)
    (row,) = decisions(app)
    assert [c["predicate_id"] for c in row.criteria if not c["passed"]] == [
        "attached_to_expected_version"
    ]
    assert run_row(app, run_id).status != "completed"
    assert "attached_to_expected_version" in (run_row(app, run_id).error_summary or "")


def test_a_record_of_another_case_is_not_the_proposals_effect(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world

    run_id = drive(app, downstream, monkeypatch, Reading(alter=_another_case))

    _assert_rejected(app, downstream, run_id)
    (row,) = decisions(app)
    assert row.criteria_failed == 2


def test_an_effect_the_downstream_no_longer_shows_is_rejected_even_though_it_was_committed_and_received(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real downstream loses the state between the dispatch and the completion (it is reset): there is nothing to confirm."""
    app, downstream = world

    run_id = drive(app, downstream, monkeypatch, Reading(before=downstream.reset))

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("failed", "acceptance_rejected")
    (row,) = decisions(app)
    assert row.outcome == "rejected" and row.criteria_failed == 2
    assert downstream.mutations() == 0, (
        "the downstream was reset: the one mutation it counted is gone with its state"
    )


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(Reading(fail=OSError("connection refused")), id="unreachable"),
        pytest.param(
            Reading(alter=lambda a: CaseAnswer(503, a.body)),
            id="a-503-that-carries-the-real-case",
        ),
        pytest.param(
            Reading(alter=lambda a: CaseAnswer(200, ["not", "a", "case"])), id="not-a-case"
        ),
    ],
)
def test_an_unreadable_observation_is_a_recorded_rejection_and_the_run_does_not_wait_or_succeed(
    reader: Reading, world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world

    run_id = drive(app, downstream, monkeypatch, reader)

    _assert_rejected(app, downstream, run_id)
    assert reader.calls == 1, "one read, not a retry loop"


def test_a_different_verdict_under_a_stored_key_is_refused_audited_and_fails_the_run_closed(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world

    def plant_a_conflicting_decision() -> None:
        """Another writer already stored a ``rejected`` verdict under the key this very observation will have."""
        (proposal,) = proposals(app)
        with app.session_factory() as session:
            receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        case = downstream.client.get(f"/cases/{CASE}").json()
        digest = observation_digest(
            Observations(
                {
                    "proposal": {"case_id": CASE, "document_type": DOCUMENT, "expected_version": 1},
                    "receipt": {"downstream_request_id": receipt.downstream_request_id},
                    "case_record": {
                        "case_id": CASE,
                        "document_requests": case["document_requests"],
                    },
                }
            )
        )
        with app.session_factory() as session, session.begin():
            session.add(
                CaliberAcceptanceDecision(
                    decision_id="acc-planted",
                    workflow_run_id=proposal.workflow_run_id,
                    candidate_id=proposal.proposal_id,
                    criteria_version="harborlight-v1",
                    observation_digest=digest,
                    outcome="rejected",
                    criteria=[
                        {
                            "predicate_id": "document_request_exists",
                            "passed": False,
                            "evidence_ref": "/x",
                            "rule_ids": [],
                        }
                    ],
                    criteria_total=1,
                    criteria_failed=1,
                    decided_at=datetime(2026, 1, 1),
                )
            )

    run_id = drive(app, downstream, monkeypatch, Reading(before=plant_a_conflicting_decision))

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("failed", "acceptance_error")
    assert [(d.decision_id, d.outcome) for d in decisions(app)] == [("acc-planted", "rejected")], (
        "nothing was replaced and nothing was added"
    )
    (entry,) = audit_entries(app, "acceptance_error")
    assert (entry.entity_id, entry.details["code"]) == (run_id, "decision_conflict")
    public = public_run(app, run_id)
    assert public["acceptance"] == {
        "status": "error",
        "criteria_version": None,
        "error_code": "decision_conflict",
        "decisions": [],
    }
    assert downstream.mutations() == 1


def test_a_criterion_that_breaks_fails_the_run_closed_and_records_no_verdict(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    from caliber.acceptance import Predicate

    def broken(_: Observations) -> Any:
        raise RuntimeError("a bug in the criterion")

    bug = Predicate(
        id="broken",
        criteria_version="harborlight-v1",
        rule_ids=["harborlight.document-request-recorded"],
        evaluate=broken,
    )
    monkeypatch.setattr(
        completion_module,
        "CRITERIA_BY_CONNECTOR",
        {"document-request": completion_module.Criteria("document-request", (bug,))},
    )

    run_id = drive(app, downstream, monkeypatch, Reading())

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("failed", "acceptance_error")
    assert decisions(app) == []
    (entry,) = audit_entries(app, "acceptance_error")
    assert entry.details == {"code": "predicate_failed", "error_type": "PredicateError"}
    assert public_run(app, run_id)["acceptance"]["error_code"] == "predicate_failed"
    assert downstream.mutations() == 1


def test_judging_again_after_a_restart_names_the_same_decision_and_stores_nothing_new(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    (first,) = decisions(app)
    targets = targets_from_bindings(parse_bindings(_bindings(downstream)))

    for _ in range(3):  # a new process, a new session each time
        with app.session_factory() as session:
            run = session.get(CaliberWorkflowRun, run_id)
            assert run is not None
            outcome = evaluate_run(session, run, targets=targets)
            session.commit()
        assert outcome is not None and outcome.accepted
        assert [d.decision_id for d in outcome.decisions] == [first.decision_id]

    assert [d.decision_id for d in decisions(app)] == [first.decision_id]
    assert downstream.mutations() == 1


def test_concurrent_workers_judging_the_same_run_store_one_decision(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    targets = targets_from_bindings(parse_bindings(_bindings(downstream)))
    barrier = threading.Barrier(6)
    seen: list[str] = []
    failures: list[BaseException] = []

    def judge_once() -> None:
        try:
            barrier.wait(timeout=30)
            with app.session_factory() as session:
                run = session.get(CaliberWorkflowRun, run_id)
                assert run is not None
                outcome = evaluate_run(session, run, targets=targets)
                session.commit()
            assert outcome is not None and outcome.accepted
            seen.append(outcome.decisions[0].decision_id)
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=judge_once) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not failures, failures
    assert len(seen) == 6 and len(set(seen)) == 1
    assert len(decisions(app)) == 1
    assert downstream.mutations() == 1
