"""B09.2b (#791): a run completes only when the platform's own acceptance of its governed effects allows it.

Three layers, each asserting what is **persisted**:

* the pure helpers: the strict conversion of a proposal's version text, the case a resource names, the case read's
  configuration and the run-state vocabulary;
* :func:`caliber.acceptance.completion.evaluate_run` on a database migrated by Alembic (SQLite, and PostgreSQL where
  ``CALIBER_TEST_POSTGRES_URL`` is set): the observations the platform builds from its own records, one decision per
  candidate, rejection for a stale, wrong, missing or unreadable record, a repeat that stores nothing new, a conflict and a
  broken criterion that are raised;
* the worker's ``_complete_if_accepted``: ``completed`` only with an accepted (or no) acceptance, ``failed`` with
  ``acceptance_rejected`` / ``acceptance_error`` otherwise, the audit of an error, the decisions committing with the run's
  terminal status, and the development profile left alone. The public serializer carries the result.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.acceptance import HARBORLIGHT_V1, Observations, Predicate
from caliber.acceptance import completion as completion_module
from caliber.acceptance.completion import (
    CRITERIA_BY_CONNECTOR,
    MAX_LISTED_REQUESTS,
    CaseAnswer,
    Criteria,
    evaluate_run,
    is_proposal_candidate,
)
from caliber.acceptance.decision import (
    DECISION_CONFLICT,
    DECISION_CORRUPT,
    AcceptanceError,
    decisions_for_run,
    observation_digest,
)
from caliber.acceptance.predicates import PredicateError
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberAuditLog,
    CaliberWorkflowRun,
)
from caliber.effects.connectors import document_request
from caliber.effects.receipts import ReceiptFacts, record_receipt
from caliber.effects.records import COMMITTED, proposal_state, proposal_view, transition
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.schemas import WorkflowRunSchema
from caliber.workflows.run_state import (
    ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_ERROR,
    ACCEPTANCE_FAILURE_CODES,
    ACCEPTANCE_REJECTED,
    ACCEPTANCE_STATUSES,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    can_transition_run_status,
    status_after_acceptance,
)
from caliber.workflows.runtime import WorkflowRunResult
from tests.dialect_helpers import DIALECTS
from tests.effect_helpers import leased
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _proposal
from tests.test_effect_records import AT, ATTEMPT, OWNER
from tests.test_event_outbox import _database

CASE = "MORT-2026-000184"
DOC = "pay_statement"
RUN = "RUN-ep"
PROJECT = "PRJ-ep"
BASE_URL = "http://downstream.invalid"
TARGETS = {(document_request.CONNECTOR, PROJECT): BASE_URL}
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def _commit(session: Session, proposal_id: str, request_id: str) -> None:
    """Settle a dispatched proposal ``committed`` on a verified receipt that names ``request_id``."""
    view = proposal_view(session, proposal_id)
    receipt = record_receipt(
        session,
        proposal_id,
        ReceiptFacts(
            attempt=view.attempt or 1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=request_id,
            response_digest="sha256:" + "b" * 64,
            resource_version="2",
        ),
        now=AT,
    )
    transition(
        session,
        proposal_id,
        COMMITTED,
        expected_version=proposal_state(session, proposal_id).version,
        owner_id=OWNER,
        attempt=ATTEMPT,
        lease=leased(session, OWNER),
        receipt_id=receipt.receipt_id,
        now=AT,
    )


def _case(*requests: dict[str, Any], **extra: Any) -> CaseAnswer:
    return CaseAnswer(
        200, {"case_id": CASE, "version": 9, "document_requests": list(requests), **extra}
    )


def _request(request_id: str = "DR-1", version: int = 2, **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "document_type": DOC, "case_version": version, **extra}


class Reader:
    """A scripted read of the case that records what it was asked."""

    def __init__(self, answer: CaseAnswer | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, base_url: str, case_id: str) -> CaseAnswer:
        self.calls.append((base_url, case_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@contextmanager
def _world(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        yield factory


def _evaluate(
    factory: sessionmaker[Session],
    reader: Callable[[str, str], CaseAnswer],
    *,
    targets: Any = TARGETS,
) -> Any:
    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        outcome = evaluate_run(session, run, targets=targets, reader=reader, now=NOW)
        session.commit()
        return outcome


def _stored(factory: sessionmaker[Session]) -> list[CaliberAcceptanceDecision]:
    with factory() as session:
        return list(
            session.execute(
                select(CaliberAcceptanceDecision).order_by(CaliberAcceptanceDecision.decision_id)
            ).scalars()
        )


def _committed_proposal(
    factory: sessionmaker[Session],
    *,
    expected: str | None = "1",
    occurrence: str = "occ-1",
    request_id: str = "DR-1",
) -> str:
    with factory() as session:
        proposal_id = _proposal(session, occurrence=occurrence, expected=expected)
        _commit(session, proposal_id, request_id)
        session.commit()
    return proposal_id


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1", 1),
        ("42", 42),
        ("999999999999999999", 999999999999999999),
        ("01", None),
        ("00", None),
        (" 1", None),
        ("1 ", None),
        ("1\n", None),
        ("+1", None),
        ("-1", None),
        ("1.0", None),
        ("1e3", None),
        ("\N{ARABIC-INDIC DIGIT ONE}", None),  # str.isdigit() is true, but the version is not ASCII
        ("²", None),
        ("", None),
        ("1000000000000000000", None),  # 19 digits
        (None, None),
        (1, None),
        (True, None),
    ],
)
def test_a_version_text_converts_only_when_it_is_canonical(
    text: object, expected: int | None
) -> None:
    assert document_request.expected_version_of(text) == expected


@pytest.mark.parametrize(
    ("resource", "case"),
    [
        (f"cases/{CASE}/document-requests", CASE),
        ("cases/a/document-requests", "a"),
        ("cases/a.b_c-d/document-requests", "a.b_c-d"),
        ("cases/" + "a" * 128 + "/document-requests", "a" * 128),
        ("cases/" + "a" * 129 + "/document-requests", None),
        ("cases//document-requests", None),
        ("cases/../document-requests", None),
        ("cases/.hidden/document-requests", None),
        ("cases/a/b/document-requests", None),
        ("cases/a/document-requests/", None),
        ("/cases/a/document-requests", None),
        ("cases/a/document-requests\n", None),
        ("cases/a?x=1/document-requests", None),
        ("other/a/document-requests", None),
        (None, None),
        (7, None),
    ],
)
def test_the_case_comes_from_exactly_the_recorded_resource(
    resource: object, case: str | None
) -> None:
    assert document_request.case_id_of_resource(resource) == case


def test_the_case_read_is_one_short_get_of_one_safe_path_segment() -> None:
    config = document_request.case_config(BASE_URL)
    assert config["method"] == "GET" and config["path"] == "/cases/{case_id}"
    assert config["max_attempts"] == 1 and config["server_url"] == BASE_URL
    assert config["timeout_seconds"] == document_request.CASE_READ_TIMEOUT_SECONDS == 5.0
    assert config["auth_binding"] is None
    assert document_request.case_input(CASE) == {"path_params": {"case_id": CASE}}
    for bad in ("", "../x", "a/b", ".x", "a b", None, 1):
        with pytest.raises(ValueError, match="case_id must be"):
            document_request.case_input(bad)  # type: ignore[arg-type]


def test_the_registry_pins_harborlight_for_the_document_request_connector_only() -> None:
    assert set(CRITERIA_BY_CONNECTOR) == {"document-request"}
    criteria = CRITERIA_BY_CONNECTOR["document-request"]
    assert criteria.version == "harborlight-v1"
    assert criteria.predicates == tuple(HARBORLIGHT_V1)
    assert criteria.predicate_ids == ("document_request_exists", "attached_to_expected_version")
    with pytest.raises(TypeError):
        CRITERIA_BY_CONNECTOR["http"] = criteria  # type: ignore[index]


def test_a_candidate_is_a_recorded_proposal_only_by_its_prefix() -> None:
    assert is_proposal_candidate("prop-" + "a" * 32)
    assert not is_proposal_candidate("cf-" + "a" * 32)
    assert not is_proposal_candidate("PROP-x") and not is_proposal_candidate(None)  # type: ignore[arg-type]


def test_a_run_is_completed_only_with_an_accepted_or_no_acceptance() -> None:
    assert {"accepted", "rejected", "error"} == ACCEPTANCE_STATUSES
    assert status_after_acceptance(RUN_STATUS_COMPLETED, None) == RUN_STATUS_COMPLETED
    assert (
        status_after_acceptance(RUN_STATUS_COMPLETED, ACCEPTANCE_ACCEPTED) == RUN_STATUS_COMPLETED
    )
    for refused in (ACCEPTANCE_REJECTED, ACCEPTANCE_ERROR, "", "unheard-of", "ACCEPTED"):
        assert status_after_acceptance(RUN_STATUS_COMPLETED, refused) == RUN_STATUS_FAILED
    # only a completion is gated: every other status is returned as it is
    assert status_after_acceptance(RUN_STATUS_FAILED, ACCEPTANCE_ACCEPTED) == RUN_STATUS_FAILED
    assert status_after_acceptance(RUN_STATUS_RUNNING, ACCEPTANCE_REJECTED) == RUN_STATUS_RUNNING
    assert ACCEPTANCE_FAILURE_CODES == {
        "rejected": "acceptance_rejected",
        "error": "acceptance_error",
    }
    # a rejection fails a *running* run through the transition that already exists: no new edge, no new status
    assert can_transition_run_status(RUN_STATUS_RUNNING, RUN_STATUS_FAILED)


# ---------------------------------------------------------------- evaluate_run


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_committed_effect_that_the_case_record_confirms_is_accepted_and_persisted(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world(dialect, tmp_path, monkeypatch) as factory:
        proposal_id = _committed_proposal(factory)
        reader = Reader(_case(_request(), _request("DR-other", 5), junk={"a": 1}))

        outcome = _evaluate(factory, reader)

        assert outcome is not None and outcome.accepted and outcome.status == ACCEPTANCE_ACCEPTED
        assert reader.calls == [(BASE_URL, CASE)]
        (row,) = _stored(factory)
        assert (row.workflow_run_id, row.candidate_id) == (RUN, proposal_id)
        assert (row.outcome, row.criteria_version) == ("accepted", "harborlight-v1")
        assert (row.criteria_total, row.criteria_failed) == (2, 0)
        assert [c["evidence_ref"] for c in row.criteria] == [
            "/case_record/document_requests/0",
            "/case_record/document_requests/0/case_version",
        ]
        # the observation is what the platform read: its own proposal and receipt, the case projected to the keys judged
        assert row.observation_digest == observation_digest(
            Observations(
                {
                    "proposal": {"case_id": CASE, "document_type": DOC, "expected_version": 1},
                    "receipt": {"downstream_request_id": "DR-1"},
                    "case_record": {
                        "case_id": CASE,
                        "document_requests": [_request(), _request("DR-other", 5)],
                    },
                }
            )
        )
        assert outcome.decisions[0].decision_id == row.decision_id
        assert outcome.criteria_version == "harborlight-v1"
        assert outcome.as_summary() == {
            "status": "accepted",
            "criteria_version": "harborlight-v1",
            "error_code": None,
            "decisions": [
                {
                    "decision_id": row.decision_id,
                    "candidate_id": proposal_id,
                    "outcome": "accepted",
                    "observation_digest": row.observation_digest,
                    "failed_criteria": [],
                }
            ],
        }


@pytest.mark.parametrize(
    ("answer", "failed"),
    [
        pytest.param(
            _case(_request(version=1)),
            ["attached_to_expected_version"],
            id="stale-the-downstream-did-not-advance-the-case",
        ),
        pytest.param(
            _case(_request(version=3)),
            ["attached_to_expected_version"],
            id="advanced-by-more-than-the-effect",
        ),
        pytest.param(
            _case(),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-case-lists-no-request",
        ),
        pytest.param(
            _case(_request("DR-other")),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-receipts-request-is-not-listed",
        ),
        pytest.param(
            _case(_request(document_type="bank_statement")),
            ["document_request_exists", "attached_to_expected_version"],
            id="a-different-document-type",
        ),
        pytest.param(
            _case(_request(), _request()),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-request-id-is-listed-twice",
        ),
        pytest.param(
            CaseAnswer(200, {"case_id": "OTHER-CASE", "document_requests": [_request()]}),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-record-is-another-cases",
        ),
        pytest.param(
            CaseAnswer(200, {"document_requests": [_request()]}),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-record-names-no-case",
        ),
        pytest.param(
            CaseAnswer(200, {"case_id": CASE}),
            ["document_request_exists", "attached_to_expected_version"],
            id="the-record-has-no-request-list",
        ),
    ],
)
@pytest.mark.parametrize("dialect", DIALECTS[:1])
def test_a_2xx_whose_recorded_state_is_wrong_is_a_persisted_rejection(
    dialect: str,
    answer: CaseAnswer,
    failed: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _world(dialect, tmp_path, monkeypatch) as factory:
        proposal_id = _committed_proposal(factory)

        outcome = _evaluate(factory, Reader(answer))

        assert outcome.status == ACCEPTANCE_REJECTED and not outcome.accepted
        (row,) = _stored(factory)
        assert (row.outcome, row.candidate_id) == ("rejected", proposal_id)
        assert [c["predicate_id"] for c in row.criteria if not c["passed"]] == failed
        assert outcome.decisions[0].failed_criteria == tuple(failed)
        assert outcome.as_summary()["decisions"][0]["failed_criteria"] == failed


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(Reader(OSError("connection refused")), id="unreachable"),
        pytest.param(Reader(CaseAnswer(500, {"error": "boom"})), id="server-error"),
        pytest.param(Reader(CaseAnswer(404, {"error": "case_not_found"})), id="no-such-case"),
        pytest.param(
            Reader(CaseAnswer(500, _case(_request()).body)),
            id="a-500-that-carries-a-perfect-looking-case",
        ),
        pytest.param(
            Reader(CaseAnswer(201, _case(_request()).body)), id="not-a-200-though-the-body-matches"
        ),
        pytest.param(Reader(CaseAnswer(200, [1, 2])), id="a-list-is-not-a-case"),
        pytest.param(Reader(CaseAnswer(200, None)), id="no-json-body"),
        pytest.param(Reader(CaseAnswer(200, "text")), id="a-string-body"),
        pytest.param(
            Reader(
                CaseAnswer(200, {"document_requests": [_request()] * (MAX_LISTED_REQUESTS + 1)})
            ),
            id="an-implausibly-long-list",
        ),
    ],
)
def test_a_case_that_cannot_be_read_is_a_recorded_rejection_not_a_crash_or_a_wait(
    reader: Reader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)

        outcome = _evaluate(factory, reader)

        assert outcome.status == ACCEPTANCE_REJECTED
        (row,) = _stored(factory)
        assert row.outcome == "rejected" and row.criteria_failed == 2
        assert [c["evidence_ref"] for c in row.criteria] == [
            "/case_record/case_id",
            "/case_record/case_id",
        ]
        assert reader.calls == [(BASE_URL, CASE)], "asked once, not retried and not awaited"


@pytest.mark.parametrize(
    ("extra", "accepted"), [(MAX_LISTED_REQUESTS - 1, True), (MAX_LISTED_REQUESTS, False)]
)
def test_the_list_bound_is_exactly_the_documented_one(
    extra: int, accepted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case lists the effect and ``extra`` other requests: the bound is on the length of the list, inclusive."""
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        others = [_request(f"DR-x{n}", 3) for n in range(extra)]
        outcome = _evaluate(factory, Reader(_case(_request(), *others)))
        assert outcome.accepted is accepted


def test_a_receipt_that_cannot_be_read_back_is_not_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.effects.records import ProposalError

    def corrupt(*_: Any, **__: Any) -> Any:
        raise ProposalError("receipt_corrupt", "receipt does not match its recorded bytes")

    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        reader = Reader(_case(_request()))
        monkeypatch.setattr(completion_module, "read_receipt", corrupt)
        outcome = _evaluate(factory, reader)
        assert outcome.status == ACCEPTANCE_REJECTED and reader.calls == []
        monkeypatch.setattr(
            completion_module,
            "read_receipt",
            lambda *_a, **_k: (_ for _ in ()).throw(KeyError("gone")),
        )
        assert _evaluate(factory, reader).status == ACCEPTANCE_REJECTED and reader.calls == []


def test_a_case_with_no_binding_for_the_project_is_not_asked_and_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        reader = Reader(_case(_request()))
        for targets in ({}, {(document_request.CONNECTOR, "another-project"): BASE_URL}):
            outcome = _evaluate(factory, reader, targets=targets)
            assert outcome.status == ACCEPTANCE_REJECTED
        assert reader.calls == []
        assert len(_stored(factory)) == 1, "both passes saw the same observation"


def test_a_run_with_no_project_has_no_binding_and_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        reader = Reader(_case(_request()))
        with factory() as session:
            run = session.get(CaliberWorkflowRun, RUN)
            assert run is not None
            run.project_id = None
            outcome = evaluate_run(session, run, targets=TARGETS, reader=reader, now=NOW)
            session.rollback()
        assert outcome is not None and outcome.status == ACCEPTANCE_REJECTED
        assert reader.calls == []


def test_a_proposal_without_a_receipt_is_not_accepted_and_the_downstream_is_not_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The V11 refusal: a dispatch the downstream refused (or that never happened) has no receipt, and a 2xx elsewhere cannot stand in."""
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        with factory() as session:
            _proposal(session)  # dispatched, never settled: no receipt
            session.commit()
        reader = Reader(_case(_request()))

        outcome = _evaluate(factory, reader)

        assert outcome.status == ACCEPTANCE_REJECTED
        assert reader.calls == []
        (row,) = _stored(factory)
        assert row.outcome == "rejected"
        assert row.criteria_failed == 2, "neither criterion can pass without a receipt"
        assert [c["evidence_ref"] for c in row.criteria] == ["/case_record/case_id"] * 2


@pytest.mark.parametrize(
    "expected",
    [
        None,
        "01",
        " 1",
        "1 ",
        "-1",
        "+1",
        "1.0",
        "1e3",
        "\N{ARABIC-INDIC DIGIT ONE}",
        "x",
        "1000000000000000000",
    ],
)
def test_a_version_the_platform_cannot_convert_strictly_is_not_judged_as_a_number_and_fails_closed(
    expected: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory, expected=expected)

        outcome = _evaluate(factory, Reader(_case(_request(version=2))))

        assert outcome.status == ACCEPTANCE_REJECTED
        (row,) = _stored(factory)
        failed = {c["predicate_id"]: c["evidence_ref"] for c in row.criteria if not c["passed"]}
        assert failed == {"attached_to_expected_version": "/proposal/expected_version"}


@pytest.mark.parametrize(("expected", "version", "accepted"), [("0", 1, True), ("0", 0, False)])
def test_the_zero_version_is_a_real_version(
    expected: str, version: int, accepted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory, expected=expected)
        outcome = _evaluate(factory, Reader(_case(_request(version=version))))
        assert outcome.accepted is accepted


def test_a_run_with_no_effect_that_has_criteria_has_nothing_to_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        reader = Reader(_case(_request()))
        assert _evaluate(factory, reader) is None
        assert reader.calls == [] and _stored(factory) == []


def test_an_effect_of_a_connector_with_no_criteria_is_not_part_of_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        monkeypatch.setattr(completion_module, "CRITERIA_BY_CONNECTOR", {"some-other": object()})
        assert _evaluate(factory, Reader(_case(_request()))) is None
        assert _stored(factory) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_judging_again_over_the_same_observation_stores_nothing_new_and_names_the_same_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world(dialect, tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        reader = Reader(_case(_request()))
        first = _evaluate(factory, reader)
        again = _evaluate(factory, reader)
        assert first.decisions == again.decisions
        assert len(_stored(factory)) == 1

        # a downstream that now shows something else is a new observation, so a new decision; the first stays as evidence
        changed = _evaluate(factory, Reader(_case(_request(version=9))))
        assert changed.status == ACCEPTANCE_REJECTED
        assert changed.decisions[0].decision_id != first.decisions[0].decision_id
        assert len(_stored(factory)) == 2
        with factory() as session:
            assert len(decisions_for_run(session, RUN)) == 2


@pytest.mark.parametrize("dialect", DIALECTS)
def test_concurrent_passes_over_the_same_run_store_one_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world(dialect, tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        barrier = threading.Barrier(6)
        results: list[Any] = []
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=30)
                results.append(_evaluate(factory, Reader(_case(_request()))))
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not failures, failures
        assert len(results) == 6 and all(item.accepted for item in results)
        assert len({item.decisions[0].decision_id for item in results}) == 1
        assert len(_stored(factory)) == 1


def test_every_effect_gets_its_own_decision_and_one_rejection_rejects_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        good = _committed_proposal(factory, occurrence="occ-1", request_id="DR-1")
        bad = _committed_proposal(factory, occurrence="occ-2", request_id="DR-2")

        reader = Reader(_case(_request("DR-1", 2)))
        outcome = _evaluate(factory, reader)

        assert reader.calls == [(BASE_URL, CASE)], "two effects on one case are one read"
        assert outcome.status == ACCEPTANCE_REJECTED
        by_candidate = {d.candidate_id: d.outcome for d in outcome.decisions}
        assert by_candidate == {good: "accepted", bad: "rejected"}
        assert len(_stored(factory)) == 2, "a rejection does not stop the pass"
        assert {d.candidate_id for d in outcome.decisions} == {good, bad}
        assert all(is_proposal_candidate(d.candidate_id) for d in outcome.decisions)


def test_a_different_verdict_under_a_stored_key_is_a_raised_conflict_and_nothing_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        proposal_id = _committed_proposal(factory)
        digest = observation_digest(
            Observations(
                {
                    "proposal": {"case_id": CASE, "document_type": DOC, "expected_version": 1},
                    "receipt": {"downstream_request_id": "DR-1"},
                    "case_record": {"case_id": CASE, "document_requests": [_request()]},
                }
            )
        )
        with factory() as session:
            session.add(
                CaliberAcceptanceDecision(
                    decision_id="acc-planted",
                    workflow_run_id=RUN,
                    candidate_id=proposal_id,
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
            session.commit()

        with factory() as session:
            run = session.get(CaliberWorkflowRun, RUN)
            with pytest.raises(AcceptanceError) as caught:
                evaluate_run(
                    session, run, targets=TARGETS, reader=Reader(_case(_request())), now=NOW
                )  # type: ignore[arg-type]
            assert caught.value.code == DECISION_CONFLICT
            session.rollback()
        (row,) = _stored(factory)
        assert (row.decision_id, row.outcome) == ("acc-planted", "rejected")


def test_a_criterion_that_raises_is_raised_to_the_caller_and_nothing_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Observations) -> Any:
        raise RuntimeError("the criterion has a bug")

    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        bug = Predicate(
            id="broken",
            criteria_version="harborlight-v1",
            rule_ids=["harborlight.document-request-recorded"],
            evaluate=broken,
        )
        monkeypatch.setattr(
            completion_module,
            "CRITERIA_BY_CONNECTOR",
            {"document-request": Criteria("document-request", (bug,))},
        )
        with factory() as session:
            run = session.get(CaliberWorkflowRun, RUN)
            with pytest.raises(PredicateError) as caught:
                evaluate_run(
                    session, run, targets=TARGETS, reader=Reader(_case(_request())), now=NOW
                )  # type: ignore[arg-type]
            assert caught.value.code == "predicate_failed"
            session.rollback()
        assert _stored(factory) == [], "a bug is never recorded as a verdict"


def test_an_observation_that_cannot_be_frozen_is_a_recorded_rejection_never_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        body = {"case_id": CASE, "document_requests": [{"request_id": float("nan")}]}
        outcome = _evaluate(factory, Reader(CaseAnswer(200, body)))
        assert outcome.status == ACCEPTANCE_REJECTED
        (row,) = _stored(factory)
        assert (row.outcome, row.reason_code) == ("rejected", "invalid_observations")
        assert row.criteria == [] and "nan" not in (row.reason_detail or "").lower()


# ---------------------------------------------------------------- the worker's gate


def _worker(
    factory: sessionmaker[Session],
    reader: Callable[[str, str], CaseAnswer] | None,
    *,
    profile: str = "reference",
) -> WorkflowRunWorker:
    config = CaliberConfig().model_copy(
        update={
            "profile": profile,
            "effect_callback_connectors": (
                '[{"connector": "document-request", "project_id": "PRJ-ep", '
                f'"secret_source": "env://X", "base_url": "{BASE_URL}"}}]'
            ),
        }
    )
    return WorkflowRunWorker(factory, config=config, case_reader=reader)


def _complete(worker: WorkflowRunWorker, factory: sessionmaker[Session]) -> CaliberWorkflowRun:
    result = WorkflowRunResult(status="completed", output="all done")
    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        worker._complete_if_accepted(session, run, result)
    with factory() as session:
        stored = session.get(CaliberWorkflowRun, RUN)
        assert stored is not None
        session.expunge(stored)
        return stored


def test_an_accepted_run_completes_and_its_decision_commits_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        proposal_id = _committed_proposal(factory)
        run = _complete(_worker(factory, Reader(_case(_request()))), factory)

        assert (run.status, run.error_code, run.error_summary) == ("completed", None, None)
        (row,) = _stored(factory)
        acceptance = run.summary["acceptance"]
        assert acceptance["status"] == "accepted"
        assert [d["decision_id"] for d in acceptance["decisions"]] == [row.decision_id]
        assert acceptance["decisions"][0]["candidate_id"] == proposal_id
        public = WorkflowRunSchema.model_validate(run).model_dump()["acceptance"]
        assert public["status"] == "accepted" and public["decisions"][0]["outcome"] == "accepted"


def test_a_rejected_run_fails_with_the_decision_and_is_never_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        proposal_id = _committed_proposal(factory)
        run = _complete(_worker(factory, Reader(_case(_request(version=1)))), factory)

        assert run.status == "failed" and run.error_code == "acceptance_rejected"
        assert run.completed_at is not None
        assert proposal_id in (run.error_summary or "")
        assert "attached_to_expected_version" in (run.error_summary or "")
        assert (
            run.summary["status"] == "failed" and run.summary["acceptance"]["status"] == "rejected"
        )
        (row,) = _stored(factory)
        assert row.outcome == "rejected", "the rejection is recorded and committed with the failure"
        public = WorkflowRunSchema.model_validate(run).model_dump()["acceptance"]
        assert public["status"] == "rejected"
        assert public["decisions"][0]["failed_criteria"] == ["attached_to_expected_version"]


def test_an_unreadable_case_fails_the_run_with_a_recorded_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        run = _complete(_worker(factory, Reader(OSError("down"))), factory)
        assert (run.status, run.error_code) == ("failed", "acceptance_rejected")
        assert [r.outcome for r in _stored(factory)] == ["rejected"]


def test_a_run_with_nothing_to_judge_completes_with_no_acceptance_information(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        reader = Reader(_case(_request()))
        run = _complete(_worker(factory, reader), factory)
        assert (run.status, run.error_code) == ("completed", None)
        assert "acceptance" not in run.summary and reader.calls == []
        assert WorkflowRunSchema.model_validate(run).model_dump()["acceptance"] is None


def test_the_development_profile_keeps_its_own_completion_and_judges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)
        reader = Reader(_case(_request(version=1)))  # would be rejected if it were judged
        run = _complete(_worker(factory, reader, profile="development"), factory)
        assert (run.status, run.error_code) == ("completed", None)
        assert "acceptance" not in run.summary
        assert reader.calls == [] and _stored(factory) == []


def test_a_conflicting_decision_fails_the_run_closed_and_is_audited_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory)

        def conflict(*_: Any, **__: Any) -> Any:
            raise AcceptanceError(DECISION_CONFLICT, "decision acc-x already holds another verdict")

        monkeypatch.setattr(completion_module, "evaluate_run", conflict)
        run = _complete(_worker(factory, Reader(_case(_request()))), factory)

        assert (run.status, run.error_code) == ("failed", "acceptance_error")
        assert run.summary["acceptance"] == {
            "status": "error",
            "criteria_version": None,
            "error_code": DECISION_CONFLICT,
            "decisions": [],
        }
        with factory() as session:
            (entry,) = session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == "acceptance_error")
            ).scalars()
        assert (entry.entity_type, entry.entity_id) == ("workflow_run", RUN)
        assert entry.details == {"code": DECISION_CONFLICT, "error_type": "AcceptanceError"}
        assert entry.severity == "high"
        public = WorkflowRunSchema.model_validate(run).model_dump()["acceptance"]
        assert (public["status"], public["error_code"]) == ("error", DECISION_CONFLICT)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AcceptanceError(DECISION_CORRUPT, "decision acc-x is inconsistent"), DECISION_CORRUPT),
        (PredicateError("predicate_failed", "a predicate raised"), "predicate_failed"),
        (RuntimeError("something unforeseen"), "unexpected"),
    ],
)
def test_every_other_failure_to_judge_fails_the_run_and_is_audited_with_a_fixed_code(
    error: Exception, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world("sqlite", tmp_path, monkeypatch) as factory:

        def boom(*_: Any, **__: Any) -> Any:
            raise error

        monkeypatch.setattr(completion_module, "evaluate_run", boom)
        run = _complete(_worker(factory, None), factory)
        assert (run.status, run.error_code) == ("failed", "acceptance_error")
        assert run.summary["acceptance"]["error_code"] == code
        assert code in (run.error_summary or "") and "unforeseen" not in (run.error_summary or "")
        with factory() as session:
            (entry,) = session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == "acceptance_error")
            ).scalars()
        assert entry.details == {"code": code, "error_type": type(error).__name__}


def test_a_failure_to_judge_takes_back_the_decisions_of_the_pass_and_keeps_the_run_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second effect cannot be judged: the first's decision is not left behind, and the run's own pending changes survive."""
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        _committed_proposal(factory, occurrence="occ-1", request_id="DR-1")
        _committed_proposal(factory, occurrence="occ-2", request_id="DR-2")
        real = completion_module.record_decision
        calls: list[str] = []

        def second_fails(session: Session, **kwargs: Any) -> Any:
            calls.append(kwargs["candidate_id"])
            if len(calls) == 2:
                raise AcceptanceError("store_failed", "the decision could not be stored")
            return real(session, **kwargs)

        monkeypatch.setattr(completion_module, "record_decision", second_fails)
        worker = _worker(factory, Reader(_case(_request("DR-1"), _request("DR-2"))))
        result = WorkflowRunResult(status="completed", output="x", mlflow_trace_id="trace-1")
        with factory() as session:
            run = session.get(CaliberWorkflowRun, RUN)
            assert run is not None
            run.trace_id = (
                "trace-1"  # a pending change of the run row, as the worker sets before it gets here
            )
            worker._complete_if_accepted(session, run, result)
        with factory() as session:
            stored = session.get(CaliberWorkflowRun, RUN)
            assert stored is not None
            assert (stored.status, stored.error_code, stored.trace_id) == (
                "failed",
                "acceptance_error",
                "trace-1",
            )
        assert len(calls) == 2 and _stored(factory) == []


def test_the_serializer_never_reports_a_malformed_record_as_acceptance() -> None:
    base: dict[str, Any] = {
        "workflow_run_id": "WR-1",
        "workflow_id": "WF-1",
        "workflow_version_id": None,
        "deployment_alias": None,
        "mlflow_run_id": None,
        "trace_id": None,
        "session_id": None,
        "status": "completed",
        "started_at": None,
        "completed_at": None,
    }
    for summary, status in (
        ({"acceptance": {"status": "accepted", "decisions": [{"decision_id": 7}]}}, "error"),
        ({"acceptance": {"status": "approved"}}, "error"),
        ({"acceptance": "accepted"}, "error"),
        ({"acceptance": {"status": "accepted", "surprise": 1}}, "error"),
        ({"acceptance": {"status": "accepted"}}, "accepted"),
    ):
        dumped = WorkflowRunSchema(**base, summary=summary).model_dump()["acceptance"]
        assert dumped["status"] == status
        if status == "error":
            assert dumped["error_code"] == "acceptance_unreadable"
    for summary in (None, {}, {"acceptance": None}, {"output": "x"}):
        assert WorkflowRunSchema(**base, summary=summary).model_dump()["acceptance"] is None
