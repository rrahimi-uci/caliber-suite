"""B09.2a (#784): the acceptance decision about what the real downstream recorded is persisted, once, and a successful transport cannot override it.

The observations are read from the real downstream process (the EGI scenarios' disposable system of record), the way a caller
reads them after a dispatch, and judged by the Harborlight predicates; the decision is then stored in the reference app's own
database (PostgreSQL in the "EGI scenarios (PostgreSQL)" job, SQLite on a laptop). Every case asserts the **persisted** state:

* a committed request is an ``accepted`` decision, stored with the per-criterion evidence;
* an HTTP 2xx whose recorded state is wrong (the V11 false success) is a ``rejected`` decision, and the 2xx changes nothing;
* a refused proposal (412, no receipt) is ``rejected`` and judging it commits nothing downstream;
* an answer that cannot be read as observations is a recorded rejection, not a crashed run;
* the same decision recorded by concurrent workers, or again after a restart, is one row; a new observation is a new decision;
* judging calls nothing and changes nothing downstream, and recording does not touch the run.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from caliber.acceptance import HARBORLIGHT_V1, Observations
from caliber.acceptance.decision import (
    ACCEPTED,
    REJECTED,
    decisions_for_run,
    judge,
    read_decision,
    record_decision,
)
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from tests.egi_scenarios.conftest import Downstream, ReferenceApp
from tests.egi_scenarios.test_b08_token_reservation import _in_threads

CASE = "MORT-2026-000184"
DOCUMENT_TYPE = "pay_statement"
RUN = "egi-b09-run"
CANDIDATE = "prop-egi-b09"
WORKERS = 8


@pytest.fixture
def run_id(reference_app: ReferenceApp) -> str:
    """A running workflow run the decisions are about."""
    with reference_app.session_factory() as session, session.begin():
        session.add(CaliberProject(project_id="egi-b09-project", name="EGI B09", owner="@egi"))
        session.flush()
        session.add(
            CaliberWorkflow(
                workflow_id="egi-b09-wf", project_id="egi-b09-project", name="EGI B09", owner="@egi"
            )
        )
        session.flush()
        session.add(
            CaliberWorkflowRun(
                workflow_run_id=RUN,
                workflow_id="egi-b09-wf",
                project_id="egi-b09-project",
                status="running",
            )
        )
    return RUN


def _post(downstream: Downstream, key: str, if_match: int | None) -> httpx.Response:
    headers = {"Idempotency-Key": key}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    return downstream.client.post(
        f"/cases/{CASE}/document-requests", json={"document_type": DOCUMENT_TYPE}, headers=headers
    )


def _observed(
    downstream: Downstream, expected_version: int, request_id: str | None
) -> dict[str, Any]:
    """What a caller reads back after a dispatch proposed against ``expected_version``."""
    record = downstream.client.get(f"/cases/{CASE}")
    record.raise_for_status()
    observations: dict[str, Any] = {
        "proposal": {
            "case_id": CASE,
            "document_type": DOCUMENT_TYPE,
            "expected_version": expected_version,
        },
        "case_record": record.json(),
    }
    if request_id is not None:
        observations["receipt"] = {"downstream_request_id": request_id}
    return observations


def _record(app: ReferenceApp, observations: Any, *, candidate: str = CANDIDATE) -> Any:
    with app.session_factory() as session, session.begin():
        return record_decision(
            session,
            workflow_run_id=RUN,
            candidate_id=candidate,
            predicates=HARBORLIGHT_V1,
            observations=observations,
        )


def _stored(app: ReferenceApp) -> list[CaliberAcceptanceDecision]:
    with app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberAcceptanceDecision).where(
                    CaliberAcceptanceDecision.workflow_run_id == RUN
                )
            ).scalars()
        )


def _run_status(app: ReferenceApp) -> str:
    with app.session_factory() as session:
        return str(session.get(CaliberWorkflowRun, RUN).status)  # type: ignore[union-attr]


def test_a_committed_request_is_an_accepted_decision_with_its_evidence(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    response = _post(downstream, "b09-2a-accepted", if_match=1)
    assert response.status_code == 201
    observations = _observed(downstream, 1, response.json()["request_id"])

    recorded = _record(reference_app, observations)

    assert recorded.created and recorded.decision.outcome == ACCEPTED
    (row,) = _stored(reference_app)
    assert (row.outcome, row.criteria_total, row.criteria_failed) == (ACCEPTED, 2, 0)
    assert [c["evidence_ref"] for c in row.criteria] == [
        "/case_record/document_requests/0",
        "/case_record/document_requests/0/case_version",
    ]
    assert _run_status(reference_app) == "running", (
        "recording a decision does not complete or change the run"
    )
    assert downstream.mutations() == 1, "judging and recording changed nothing downstream"


def test_a_2xx_with_the_wrong_recorded_state_is_a_rejected_decision_the_transport_cannot_override(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    """The V11 false success: the transport said 201 and the model said complete; the case record says the version never moved."""
    response = _post(downstream, "b09-2a-false-success", if_match=1)
    assert response.status_code == 201
    observations = _observed(downstream, 1, response.json()["request_id"])
    observations["case_record"]["document_requests"][0]["case_version"] = (
        1  # the downstream stamped the old version
    )
    observations["transport"] = {"http_status": 201, "model_said": "complete"}

    recorded = _record(reference_app, observations)

    assert recorded.decision.outcome == REJECTED
    assert recorded.decision.failed_criteria == ("attached_to_expected_version",)
    (row,) = _stored(reference_app)
    assert (row.outcome, row.criteria_failed) == (REJECTED, 1)
    assert row.criteria[0]["passed"] is True, "the request exists; it is the version that is wrong"
    # the same facts without the transport's claims are the same verdict: nothing a transport says is an input
    assert judge(
        HARBORLIGHT_V1, {k: v for k, v in observations.items() if k != "transport"}
    ).criteria == tuple(recorded.decision.criteria)


def test_a_refused_proposal_has_no_receipt_and_is_a_rejected_decision(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    assert _post(downstream, "b09-2a-first", if_match=1).status_code == 201
    stale = _post(downstream, "b09-2a-stale", if_match=1)
    assert stale.status_code == 412
    mutations = downstream.mutations()

    recorded = _record(reference_app, _observed(downstream, 1, None), candidate="prop-egi-stale")

    assert recorded.decision.outcome == REJECTED
    assert recorded.decision.failed_criteria == (
        "document_request_exists",
        "attached_to_expected_version",
    )
    assert downstream.mutations() == mutations == 1, "judging a refused proposal commits nothing"


def test_an_answer_that_is_not_json_is_a_recorded_rejection_and_not_a_crash(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    response = _post(downstream, "b09-2a-unreadable", if_match=1)
    observations = _observed(downstream, 1, response.json()["request_id"])
    observations["case_record"]["version"] = float("nan")

    recorded = _record(reference_app, observations)

    assert recorded.decision.outcome == REJECTED
    assert recorded.decision.reason_code == "invalid_observations"
    assert recorded.decision.reason_detail == "/case_record/version is not a finite number"
    (row,) = _stored(reference_app)
    assert (row.criteria, row.criteria_total) == ([], 0)
    assert _run_status(reference_app) == "running"


def test_concurrent_workers_recording_one_decision_make_one_row_and_a_new_observation_a_new_one(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    response = _post(downstream, "b09-2a-race", if_match=1)
    observations = _observed(downstream, 1, response.json()["request_id"])

    outcomes = _in_threads(*[lambda: _record(reference_app, observations) for _ in range(WORKERS)])

    assert all(not isinstance(o, BaseException) for o in outcomes), outcomes
    assert sum(1 for o in outcomes if o.created) == 1, "exactly one worker made the decision"
    assert len({o.decision.decision_id for o in outcomes}) == 1
    assert len(_stored(reference_app)) == 1

    later = _observed(downstream, 1, response.json()["request_id"])
    later["case_record"]["version"] = 3  # the case moved on: other observations, another decision
    assert _record(reference_app, later).created
    assert len(_stored(reference_app)) == 2


def test_a_decision_survives_a_restart(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    response = _post(downstream, "b09-2a-restart", if_match=1)
    observations = _observed(downstream, 1, response.json()["request_id"])
    first = _record(reference_app, observations)

    engine = create_engine(
        reference_app.database_url
    )  # a new process: new connections to the same database
    try:
        with Session(engine) as session, session.begin():
            again = record_decision(
                session,
                workflow_run_id=RUN,
                candidate_id=CANDIDATE,
                predicates=HARBORLIGHT_V1,
                observations=observations,
            )
            assert again.created is False and again.decision == first.decision
            assert read_decision(session, first.decision.decision_id) == first.decision
            assert decisions_for_run(session, RUN) == [first.decision]
    finally:
        engine.dispose()
    assert len(_stored(reference_app)) == 1


def test_judging_calls_nothing_and_writes_nothing(
    reference_app: ReferenceApp, run_id: str, downstream: Downstream
) -> None:
    response = _post(downstream, "b09-2a-pure", if_match=1)
    observations = _observed(downstream, 1, response.json()["request_id"])
    before: dict[str, Any] = downstream.client.get(f"/cases/{CASE}").json()
    mutations = downstream.mutations()
    calls: list[httpx.Request] = []
    original = httpx.Client.send

    def spy(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        calls.append(request)
        return original(self, request, **kwargs)

    httpx.Client.send = spy  # type: ignore[method-assign]
    try:
        first = judge(HARBORLIGHT_V1, observations)
        second = judge(HARBORLIGHT_V1, Observations(observations))
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]

    assert first == second and first.outcome == ACCEPTED
    assert calls == [], "judging makes no downstream or provider call"
    assert downstream.mutations() == mutations
    assert downstream.client.get(f"/cases/{CASE}").json() == before
    assert _stored(reference_app) == [], "judging stores nothing"
