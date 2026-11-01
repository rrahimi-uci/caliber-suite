"""B09.2a (#784): acceptance decisions are judged purely, stored once, and keyed by run, candidate, criteria version and observation.

Three layers, each asserting what is **persisted** and not only what was returned:

* ``judge`` (no database): the verdict, the digest, the unreadable-observation rejection and the contract errors that are
  raised instead of judged;
* ``record_decision`` and the readers on a database migrated by Alembic, on SQLite and (where ``CALIBER_TEST_POSTGRES_URL`` is
  set) PostgreSQL: idempotence, a new candidate or observation being a new decision, the conflict a changed predicate causes,
  the caller's transaction, concurrent writers, restart, and a row edited around the application;
* the table itself, with raw SQL: the unique key, the CHECK constraints, the write-once triggers, and on PostgreSQL the foreign
  key, TRUNCATE and the privileges of a runtime role.
"""

from __future__ import annotations

import copy
import hashlib
import inspect as pyinspect
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from caliber.acceptance import (
    HARBORLIGHT_V1,
    CriterionResult,
    Observations,
    Predicate,
    PredicateError,
)
from caliber.acceptance import decision as decision_module
from caliber.acceptance.decision import (
    ACCEPTED,
    BAD_FIELD,
    CODES,
    DECISION_CONFLICT,
    DECISION_CORRUPT,
    REJECTED,
    STORE_FAILED,
    UNKNOWN_RUN,
    AcceptanceError,
    CriterionRecord,
    Verdict,
    decisions_for_run,
    find_decision,
    judge,
    observation_digest,
    read_decision,
    record_decision,
)
from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.run_manifest import canonical_bytes
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "acceptance_observations.json").read_text("utf-8")
)
RUN = "RUN-0135"
CANDIDATE = "prop-candidate-1"
EXISTS = "document_request_exists"
ATTACHED = "attached_to_expected_version"
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _observations(name: str = "happy_path") -> dict[str, Any]:
    return copy.deepcopy(FIXTURES[name])


def _pass_or_fail(passed: bool, *, version: str = "scripted-v1", pid: str = "p-one") -> Predicate:
    return Predicate(
        id=pid,
        criteria_version=version,
        rule_ids=[f"rule.{pid}"],
        evaluate=lambda _obs: CriterionResult(pid, passed, f"/{pid}"),
    )


# ------------------------------------------------------------------------------ database helpers


@contextmanager
def acceptance_db(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str = "head"
) -> Iterator[Engine]:
    """A migrated database with one run (``RUN``) in it, once per dialect."""
    with dialect_database_url(dialect, tmp_path, "acceptance") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        command.upgrade(Config(str(ALEMBIC_INI)), revision)
        engine = create_engine(url)
        try:
            seed_run(engine)
            yield engine
        finally:
            engine.dispose()


def seed_run(engine: Engine, run_id: str = RUN) -> None:
    suffix = run_id.lower()
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                CaliberProject(project_id=f"PRJ-{suffix}", name=f"P-{suffix}", owner="@t"),
                CaliberWorkflow(
                    workflow_id=f"wf-{suffix}",
                    project_id=f"PRJ-{suffix}",
                    name=f"W-{suffix}",
                    owner="@t",
                ),
            ]
        )
        session.flush()
        session.add(
            CaliberWorkflowRun(
                workflow_run_id=run_id,
                workflow_id=f"wf-{suffix}",
                project_id=f"PRJ-{suffix}",
                status="running",
            )
        )


def record(
    engine: Engine,
    observations: Any = None,
    *,
    candidate: str = CANDIDATE,
    predicates: Any = HARBORLIGHT_V1,
    run: str = RUN,
    now: datetime | None = NOW,
) -> Any:
    with Session(engine) as session, session.begin():
        return record_decision(
            session,
            workflow_run_id=run,
            candidate_id=candidate,
            predicates=predicates,
            observations=_observations() if observations is None else observations,
            now=now,
        )


def rows(engine: Engine, run: str = RUN) -> list[CaliberAcceptanceDecision]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(CaliberAcceptanceDecision).where(
                    CaliberAcceptanceDecision.workflow_run_id == run
                )
            ).scalars()
        )


def run_status(engine: Engine, run: str = RUN) -> str:
    with Session(engine) as session:
        return str(session.get(CaliberWorkflowRun, run).status)  # type: ignore[union-attr]


# ------------------------------------------------------------------------------ judge: pure


def test_judge_accepts_the_happy_path_with_per_criterion_evidence() -> None:
    verdict = judge(HARBORLIGHT_V1, _observations())
    assert verdict.outcome == ACCEPTED and verdict.accepted
    assert verdict.criteria_version == "harborlight-v1"
    assert verdict.reason_code is None and verdict.reason_detail is None
    assert verdict.criteria == (
        CriterionRecord(
            EXISTS,
            True,
            "/case_record/document_requests/0",
            ("harborlight.document-request-recorded",),
        ),
        CriterionRecord(
            ATTACHED,
            True,
            "/case_record/document_requests/0/case_version",
            ("harborlight.request-attached-to-expected-version",),
        ),
    )


def test_judge_rejects_a_stale_version_and_keeps_every_criterion() -> None:
    verdict = judge(HARBORLIGHT_V1, _observations("stale_case_version"))
    assert verdict.outcome == REJECTED and not verdict.accepted
    assert [(c.predicate_id, c.passed) for c in verdict.criteria] == [
        (EXISTS, True),
        (ATTACHED, False),
    ]
    assert verdict.criteria[1].evidence_ref == "/case_record/document_requests/0/case_version"
    assert verdict.reason_code is None


def test_judge_rejects_when_there_is_no_receipt() -> None:
    verdict = judge(HARBORLIGHT_V1, _observations("no_request"))
    assert verdict.outcome == REJECTED
    assert [c.passed for c in verdict.criteria] == [False, False]


def test_one_failed_critical_criterion_rejects_whatever_else_passed() -> None:
    predicates = [
        _pass_or_fail(True, pid="p-a"),
        _pass_or_fail(False, pid="p-b"),
        _pass_or_fail(True, pid="p-c"),
    ]
    verdict = judge(predicates, {})
    assert verdict.outcome == REJECTED
    assert [(c.predicate_id, c.passed) for c in verdict.criteria] == [
        ("p-a", True),
        ("p-b", False),
        ("p-c", True),
    ]
    assert judge([_pass_or_fail(True, pid="p-a"), _pass_or_fail(True, pid="p-c")], {}).accepted


def test_a_successful_transport_is_not_an_input() -> None:
    """Nothing in the API takes a transport status, and one inside the observations changes nothing."""
    for function in (judge, record_decision):
        names = set(pyinspect.signature(function).parameters)
        assert not names & {
            "status",
            "status_code",
            "http_status",
            "transport",
            "success",
            "override",
            "accepted",
        }
    stale = _observations("stale_case_version")
    plain = judge(HARBORLIGHT_V1, stale)
    with_transport = judge(
        HARBORLIGHT_V1,
        {**stale, "transport": {"http_status": 201, "ok": True, "model_said": "complete"}},
    )
    assert plain.outcome == with_transport.outcome == REJECTED
    assert [c.passed for c in with_transport.criteria] == [c.passed for c in plain.criteria]


def test_judge_is_deterministic_and_takes_a_mapping_or_observations() -> None:
    data = _observations()
    assert judge(HARBORLIGHT_V1, data) == judge(HARBORLIGHT_V1, data)
    assert judge(HARBORLIGHT_V1, data) == judge(HARBORLIGHT_V1, Observations(data))


def test_the_digest_is_the_sha256_of_the_canonical_json() -> None:
    data = _observations()
    expected = hashlib.sha256(canonical_bytes(data)).hexdigest()
    assert (
        observation_digest(Observations(data))
        == expected
        == judge(HARBORLIGHT_V1, data).observation_digest
    )
    shuffled = dict(reversed(list(data.items())))
    assert list(shuffled) != list(data)
    assert observation_digest(Observations(shuffled)) == expected, (
        "key order is not part of the digest"
    )
    changed = _observations()
    changed["case_record"]["version"] = 3
    assert observation_digest(Observations(changed)) != expected
    assert len(expected) == 64


def test_text_that_is_not_unicode_cannot_be_digested() -> None:
    frozen = Observations({"a": "lone surrogate \ud800"})
    with pytest.raises(PredicateError, match="not valid Unicode") as caught:
        observation_digest(frozen)
    assert caught.value.code == "invalid_observations"


@pytest.mark.parametrize(
    ("observations", "pointer"),
    [
        ({"a": float("nan")}, "/a is not a finite number"),
        ({"a": float("inf")}, "/a is not a finite number"),
        ({"a": [b"bytes"]}, "/a/0 is a bytes, which is not JSON-shaped"),
        ({"a": {1: "x"}}, "/a has a key that is not a string"),
        (None, "observations must be a mapping, not a NoneType"),
        (["not", "a", "mapping"], "observations must be a mapping, not a list"),
        ({"a": "lone \ud800"}, "not valid Unicode"),
    ],
)
def test_unreadable_observations_are_a_rejection_not_a_crash(
    observations: Any, pointer: str
) -> None:
    verdict = judge(HARBORLIGHT_V1, observations)
    assert verdict.outcome == REJECTED
    assert verdict.criteria == ()
    assert verdict.reason_code == "invalid_observations"
    assert verdict.reason_detail is not None and pointer in verdict.reason_detail
    assert verdict.criteria_version == "harborlight-v1"
    assert len(verdict.observation_digest) == 64


def test_unreadable_observations_that_differ_have_different_digests_and_never_a_readable_ones() -> (
    None
):
    a = judge(HARBORLIGHT_V1, {"a": float("nan")})
    b = judge(HARBORLIGHT_V1, {"b": float("nan")})
    assert a == judge(HARBORLIGHT_V1, {"a": float("nan")})
    assert a.observation_digest != b.observation_digest
    detail_only = {"unreadable": a.reason_detail}
    assert judge(HARBORLIGHT_V1, detail_only).observation_digest != a.observation_digest


def test_the_reason_detail_never_carries_a_value_and_is_ascii_and_bounded() -> None:
    secret = "do-not-store-0xBEEF"
    verdict = judge(HARBORLIGHT_V1, {"k": {"v": [secret, float("nan")]}})
    assert verdict.reason_detail is not None
    assert "/k/v/1" in verdict.reason_detail and secret not in verdict.reason_detail
    long_key = "é" * 600  # a key is downstream data: it is cut, and what is not ASCII is replaced
    long_verdict = judge(HARBORLIGHT_V1, {long_key: float("nan")})
    detail = long_verdict.reason_detail
    assert detail is not None and len(detail) == 512
    assert detail.isascii() and detail.startswith("/???")


def test_a_contract_error_is_raised_not_judged_even_for_unreadable_observations() -> None:
    unreadable = {"a": float("nan")}
    for observations in (_observations(), unreadable):
        with pytest.raises(PredicateError) as empty:
            judge([], observations)
        assert empty.value.code == "empty_predicate_set"
        with pytest.raises(PredicateError) as mixed:
            judge(
                [_pass_or_fail(True, version="v1"), _pass_or_fail(True, version="v2", pid="p-two")],
                observations,
            )
        assert mixed.value.code == "mixed_criteria_versions"
        with pytest.raises(PredicateError) as duplicate:
            judge([_pass_or_fail(True), _pass_or_fail(True)], observations)
        assert duplicate.value.code == "duplicate_predicate_id"
        with pytest.raises(PredicateError) as wrong:
            judge(["not a predicate"], observations)  # type: ignore[list-item]
        assert wrong.value.code == "invalid_predicate"

    def boom(_obs: Observations) -> CriterionResult:
        raise RuntimeError("boom")

    raising = Predicate("p-raises", "scripted-v1", ["rule.x"], boom)
    with pytest.raises(PredicateError) as failed:
        judge([raising], {})
    assert failed.value.code == "predicate_failed"
    verdict = judge(
        [raising], unreadable
    )  # never evaluated: the observations were unreadable first
    assert verdict.reason_code == "invalid_observations"
    assert verdict.criteria_version == "scripted-v1", "the version of the set, not a default"


def test_the_error_codes_are_the_published_set() -> None:
    assert {
        "bad_field",
        "unknown_run",
        "decision_conflict",
        "decision_corrupt",
        "store_failed",
    } == CODES
    error = AcceptanceError(UNKNOWN_RUN, "m")
    assert str(error) == "unknown_run: m" and error.code == "unknown_run"
    assert isinstance(error, ValueError)


def test_a_stored_decision_lists_its_failed_criteria_in_order() -> None:
    criteria = (
        CriterionRecord("a", True, "/a", ("r",)),
        CriterionRecord("b", False, "/b", ("r",)),
        CriterionRecord("c", False, "/c", ("r",)),
    )
    stored = decision_module.AcceptanceDecision(
        "acc-1", RUN, CANDIDATE, "v", "0" * 64, REJECTED, criteria, None, None, NOW
    )
    assert stored.failed_criteria == ("b", "c") and not stored.accepted
    assert criteria[0].as_json() == {
        "predicate_id": "a",
        "passed": True,
        "evidence_ref": "/a",
        "rule_ids": ["r"],
    }


# ------------------------------------------------------------------------------ record: on both dialects


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_accepted_decision_is_persisted_with_its_evidence(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        recorded = record(engine)
        assert recorded.created is True
        decision = recorded.decision
        assert decision.outcome == ACCEPTED and decision.accepted
        assert decision.decision_id.startswith("acc-")
        assert (decision.workflow_run_id, decision.candidate_id) == (RUN, CANDIDATE)
        assert decision.criteria_version == "harborlight-v1"
        assert decision.decided_at == NOW

        (row,) = rows(engine)
        assert row.decision_id == decision.decision_id
        assert (
            row.observation_digest == hashlib.sha256(canonical_bytes(_observations())).hexdigest()
        )
        assert (row.outcome, row.criteria_total, row.criteria_failed) == (ACCEPTED, 2, 0)
        assert row.reason_code is None and row.reason_detail is None
        assert row.criteria == [
            {
                "predicate_id": EXISTS,
                "passed": True,
                "evidence_ref": "/case_record/document_requests/0",
                "rule_ids": ["harborlight.document-request-recorded"],
            },
            {
                "predicate_id": ATTACHED,
                "passed": True,
                "evidence_ref": "/case_record/document_requests/0/case_version",
                "rule_ids": ["harborlight.request-attached-to-expected-version"],
            },
        ]
        assert run_status(engine) == "running", "recording a decision does not touch the run"

        with Session(engine) as session:
            assert read_decision(session, decision.decision_id) == decision
            assert (
                find_decision(
                    session,
                    workflow_run_id=RUN,
                    candidate_id=CANDIDATE,
                    criteria_version="harborlight-v1",
                    observation_digest=row.observation_digest,
                )
                == decision
            )
            assert decisions_for_run(session, RUN) == [decision]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_rejected_decision_persists_the_failed_criterion(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        decision = record(engine, _observations("stale_case_version")).decision
        assert decision.outcome == REJECTED and not decision.accepted
        assert decision.failed_criteria == (ATTACHED,)
        (row,) = rows(engine)
        assert (row.outcome, row.criteria_total, row.criteria_failed) == (REJECTED, 2, 1)
        assert row.criteria[1]["passed"] is False


@pytest.mark.parametrize("dialect", DIALECTS)
def test_recording_the_same_decision_again_is_idempotent(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        first = record(engine)
        second = record(engine, now=NOW + timedelta(hours=5))
        assert (first.created, second.created) == (True, False)
        assert second.decision == first.decision, (
            "the repeat returns the first decision, with its own time"
        )
        assert second.decision.decided_at == NOW
        assert len(rows(engine)) == 1
        # also from a key-order permutation of the same observations
        data = _observations()
        shuffled = dict(reversed(list(data.items())))
        assert record(engine, shuffled).created is False
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_new_candidate_observation_or_criteria_version_is_a_new_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        first = record(engine)
        other_candidate = record(engine, candidate="prop-candidate-2")
        other_observation = record(engine, _observations("stale_case_version"))
        versioned = [
            Predicate(p.id, "harborlight-v2", list(p.rule_ids), p.evaluate) for p in HARBORLIGHT_V1
        ]
        other_version = record(engine, predicates=versioned)
        decisions = [first, other_candidate, other_observation, other_version]
        assert all(d.created for d in decisions)
        assert len({d.decision.decision_id for d in decisions}) == 4
        assert [d.decision.outcome for d in decisions] == [ACCEPTED, ACCEPTED, REJECTED, ACCEPTED]
        assert len(rows(engine)) == 4
        with Session(engine) as session:
            assert {d.decision_id for d in decisions_for_run(session, RUN)} == {
                d.decision.decision_id for d in decisions
            }
            only = decisions_for_run(session, RUN, candidate_id="prop-candidate-2")
            assert [d.decision_id for d in only] == [other_candidate.decision.decision_id]
            assert decisions_for_run(session, RUN, candidate_id="nobody") == []
            assert decisions_for_run(session, "RUN-NONE") == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_two_runs_hold_their_own_decisions(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        seed_run(engine, "RUN-0135-B")
        a = record(engine)
        b = record(engine, run="RUN-0135-B")
        assert a.created and b.created and a.decision.decision_id != b.decision.decision_id
        assert len(rows(engine)) == 1 and len(rows(engine, "RUN-0135-B")) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_decisions_come_back_oldest_first(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        late = record(engine, candidate="prop-late", now=NOW + timedelta(minutes=2))
        early = record(engine, candidate="prop-early", now=NOW)
        middle = record(engine, candidate="prop-middle", now=NOW + timedelta(minutes=1))
        with Session(engine) as session:
            ordered = [d.decision_id for d in decisions_for_run(session, RUN)]
        assert ordered == [
            early.decision.decision_id,
            middle.decision.decision_id,
            late.decision.decision_id,
        ]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_unreadable_observations_are_stored_as_a_rejection_and_the_run_goes_on(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        bad = {"proposal": {"case_id": "C"}, "case_record": {"version": float("nan")}}
        first = record(engine, bad)
        assert first.created
        decision = first.decision
        assert decision.outcome == REJECTED
        assert decision.reason_code == "invalid_observations"
        assert decision.reason_detail == "/case_record/version is not a finite number"
        assert decision.criteria == () and decision.failed_criteria == ()
        (row,) = rows(engine)
        assert (row.criteria, row.criteria_total, row.criteria_failed) == ([], 0, 0)
        assert record(engine, bad).created is False, (
            "the same unreadable observation is the same decision"
        )
        other = record(engine, {"proposal": float("inf")})
        assert other.created and other.decision.decision_id != decision.decision_id
        assert record(engine, None).created, (
            "a failed read that produced no mapping is a rejection too"
        )
        assert len(rows(engine)) == 3
        with Session(engine) as session:
            assert read_decision(session, decision.decision_id) == decision


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_changed_verdict_under_one_key_is_a_conflict_and_never_replaces(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predicates changed without a new criteria version: the first decision stays the authoritative one."""
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        first = record(engine, {}, predicates=[_pass_or_fail(True)])
        with pytest.raises(AcceptanceError) as caught:
            record(engine, {}, predicates=[_pass_or_fail(False)])
        assert caught.value.code == DECISION_CONFLICT
        assert first.decision.decision_id in str(caught.value)
        (row,) = rows(engine)
        assert (row.decision_id, row.outcome, row.criteria_failed) == (
            first.decision.decision_id,
            ACCEPTED,
            0,
        )
        # the same outcome with other evidence is a conflict as well
        different_evidence = Predicate(
            "p-one",
            "scripted-v1",
            ["rule.p-one"],
            lambda _o: CriterionResult("p-one", True, "/elsewhere"),
        )
        with pytest.raises(AcceptanceError) as evidence:
            record(engine, {}, predicates=[different_evidence])
        assert evidence.value.code == DECISION_CONFLICT
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_unknown_run_and_malformed_arguments_store_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        with pytest.raises(AcceptanceError) as unknown:
            record(engine, run="RUN-NOT-THERE")
        assert unknown.value.code == UNKNOWN_RUN and "RUN-NOT-THERE" in str(unknown.value)
        for bad_run in ("", "r" * 65, None, 7):
            with pytest.raises(AcceptanceError) as run_error:
                record(engine, run=bad_run)  # type: ignore[arg-type]
            assert run_error.value.code == BAD_FIELD
        for bad_candidate in ("", " lead", "a b", "a/b", "x" * 129, None, 7, "é", "-dash-first"):
            with pytest.raises(AcceptanceError) as candidate_error:
                record(engine, candidate=bad_candidate)  # type: ignore[arg-type]
            assert candidate_error.value.code == BAD_FIELD
        with pytest.raises(AcceptanceError) as now_error:
            record(engine, now="2026-11-01")  # type: ignore[arg-type]
        assert now_error.value.code == BAD_FIELD
        assert rows(engine) == []
        assert record(engine, candidate="x" * 128).created, "128 characters is the boundary"
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_predicate_contract_error_stores_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        for predicates in ([], [_pass_or_fail(True), _pass_or_fail(True)]):
            with pytest.raises(PredicateError):
                record(engine, predicates=predicates)

        def boom(_o: Observations) -> CriterionResult:
            raise RuntimeError("x")

        with pytest.raises(PredicateError) as caught:
            record(engine, predicates=[Predicate("p-raises", "v1", ["rule.x"], boom)])
        assert caught.value.code == "predicate_failed"
        assert rows(engine) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_decision_lives_and_dies_with_the_callers_transaction(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        with Session(engine) as session:
            recorded = record_decision(
                session,
                workflow_run_id=RUN,
                candidate_id=CANDIDATE,
                predicates=HARBORLIGHT_V1,
                observations=_observations(),
                now=NOW,
            )
            assert recorded.created
            session.rollback()
        assert rows(engine) == [], "a rolled-back transaction leaves no decision"
        with Session(engine) as session:
            record_decision(
                session,
                workflow_run_id=RUN,
                candidate_id=CANDIDATE,
                predicates=HARBORLIGHT_V1,
                observations=_observations(),
                now=NOW,
            )
            session.commit()
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_time_with_a_zone_is_stored_as_utc_and_a_naive_one_is_taken_as_utc(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        plus_two = timezone(timedelta(hours=2))
        zoned = record(
            engine, candidate="prop-zoned", now=datetime(2026, 11, 1, 14, 0, tzinfo=plus_two)
        )
        naive = record(engine, candidate="prop-naive", now=datetime(2026, 11, 1, 12, 0))
        assert zoned.decision.decided_at == naive.decision.decided_at == NOW
        assert all(r.decided_at == datetime(2026, 11, 1, 12, 0) for r in rows(engine))
        default = record(engine, candidate="prop-default", now=None)
        assert abs(default.decision.decided_at - datetime.now(timezone.utc)) < timedelta(minutes=5)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_decision_the_database_refuses_is_store_failed_and_the_session_survives(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``accepted`` verdict with a failed criterion cannot reach a row: the CHECK refuses it, and the caller learns why."""
    forged = Verdict(
        criteria_version="harborlight-v1",
        observation_digest="a" * 64,
        outcome=ACCEPTED,
        criteria=(CriterionRecord(EXISTS, False, "/x", ("r",)),),
    )
    monkeypatch.setattr(decision_module, "judge", lambda _p, _o: forged)
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        with Session(engine) as session, session.begin():
            with pytest.raises(AcceptanceError) as caught:
                record_decision(
                    session,
                    workflow_run_id=RUN,
                    candidate_id=CANDIDATE,
                    predicates=HARBORLIGHT_V1,
                    observations={},
                    now=NOW,
                )
            assert caught.value.code == STORE_FAILED
            assert "aaaa" not in str(caught.value) and "INSERT" not in str(caught.value)
            assert session.execute(select(CaliberAcceptanceDecision)).first() is None
        assert rows(engine) == []


# ------------------------------------------------------------------------------ restart and concurrency


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_decision_survives_a_restart_and_the_repeat_finds_it(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        first = record(engine)
        url = engine.url.render_as_string(hide_password=False)
        engine.dispose()  # the process is gone: every connection with it
        restarted = create_engine(url)
        try:
            again = record(restarted)
            assert again.created is False and again.decision == first.decision
            with Session(restarted) as session:
                assert read_decision(session, first.decision.decision_id) == first.decision
        finally:
            restarted.dispose()


def _lose_the_first_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the writer's own pre-check see nothing, as it does when another writer commits between the check and the insert."""
    real = decision_module._row_for_key

    def blind(
        session: Any, run_id: str, candidate: str, verdict: Any, *, refresh: bool = False
    ) -> Any:
        return real(session, run_id, candidate, verdict, refresh=refresh) if refresh else None

    monkeypatch.setattr(decision_module, "_row_for_key", blind)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_writer_that_loses_the_race_at_the_unique_key_gets_the_winners_row(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        winner = record(engine)
        _lose_the_first_lookup(monkeypatch)
        loser = record(engine, now=NOW + timedelta(hours=1))
        assert loser.created is False and loser.decision == winner.decision
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_writer_that_loses_the_race_with_a_different_verdict_gets_a_conflict(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        winner = record(engine, {}, predicates=[_pass_or_fail(True)])
        _lose_the_first_lookup(monkeypatch)
        with pytest.raises(AcceptanceError) as caught:
            record(engine, {}, predicates=[_pass_or_fail(False)])
        assert caught.value.code == DECISION_CONFLICT
        (row,) = rows(engine)
        assert row.decision_id == winner.decision.decision_id and row.outcome == ACCEPTED


def _in_threads(count: int, target: Any) -> list[Any]:
    barrier = threading.Barrier(count)
    outcomes: list[Any] = [None] * count

    def run(index: int) -> None:
        barrier.wait(30)
        try:
            outcomes[index] = target(index)
        except BaseException as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(90)
    assert not any(thread.is_alive() for thread in threads), "a writer is stuck"
    return outcomes


@pytest.mark.parametrize("dialect", DIALECTS)
def test_concurrent_writers_of_one_decision_make_exactly_one_row(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        outcomes = _in_threads(6, lambda _i: record(engine))
        assert all(not isinstance(o, BaseException) for o in outcomes), outcomes
        assert sum(1 for o in outcomes if o.created) == 1
        assert len({o.decision.decision_id for o in outcomes}) == 1
        assert len(rows(engine)) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_concurrent_writers_of_different_observations_each_make_their_own_row(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:

        def write(index: int) -> Any:
            data = _observations()
            data["case_record"]["version"] = 100 + index
            return record(engine, data)

        outcomes = _in_threads(6, write)
        assert all(not isinstance(o, BaseException) and o.created for o in outcomes), outcomes
        assert len({o.decision.decision_id for o in outcomes}) == 6
        assert len(rows(engine)) == 6


# ------------------------------------------------------------------------------ readers verify what they read


def _insert_raw(engine: Engine, **overrides: Any) -> str:
    """Insert a row around the application (Core, so neither the ORM guard nor the writer's checks apply)."""
    values: dict[str, Any] = {
        "decision_id": f"acc-{uuid.uuid4().hex}",
        "workflow_run_id": RUN,
        "candidate_id": f"cand-{uuid.uuid4().hex[:8]}",
        "criteria_version": "harborlight-v1",
        "observation_digest": uuid.uuid4().hex + uuid.uuid4().hex,
        "outcome": ACCEPTED,
        "criteria": json.dumps(
            [{"predicate_id": "p", "passed": True, "evidence_ref": "/p", "rule_ids": ["r"]}]
        ),
        "criteria_total": 1,
        "criteria_failed": 0,
        "reason_code": None,
        "reason_detail": None,
    }
    values.update(overrides)
    values["criteria"] = json.loads(values["criteria"])
    values["decided_at"] = datetime(2026, 11, 1, 12, 0, 0)
    with engine.begin() as connection:
        connection.execute(insert(CaliberAcceptanceDecision.__table__).values(**values))
    return str(values["decision_id"])


def _criteria(*passed: bool) -> str:
    return json.dumps(
        [
            {"predicate_id": f"p{i}", "passed": ok, "evidence_ref": f"/p{i}", "rule_ids": ["r"]}
            for i, ok in enumerate(passed)
        ]
    )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_consistent_row_inserted_around_the_application_reads_back(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        accepted = _insert_raw(engine)
        rejected = _insert_raw(
            engine,
            outcome=REJECTED,
            criteria=_criteria(True, False),
            criteria_total=2,
            criteria_failed=1,
        )
        unreadable = _insert_raw(
            engine,
            outcome=REJECTED,
            criteria="[]",
            criteria_total=0,
            criteria_failed=0,
            reason_code="invalid_observations",
            reason_detail="/a is not a finite number",
        )
        with Session(engine) as session:
            assert read_decision(session, accepted).accepted
            assert read_decision(session, rejected).failed_criteria == ("p1",)
            assert read_decision(session, unreadable).reason_code == "invalid_observations"
            assert len(decisions_for_run(session, RUN)) == 3


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"criteria": '{"not": "a list"}'}, id="criteria-not-a-list"),
        pytest.param({"criteria": "[1]", "criteria_total": 1}, id="entry-not-a-mapping"),
        pytest.param(
            {"criteria": '[{"predicate_id": "p", "passed": true}]'}, id="entry-missing-keys"
        ),
        pytest.param(
            {
                "criteria": '[{"predicate_id": "p", "passed": 1, "evidence_ref": "/p", "rule_ids": ["r"]}]'
            },
            id="passed-not-a-bool",
        ),
        pytest.param(
            {
                "criteria": '[{"predicate_id": "p", "passed": true, "evidence_ref": "/p", "rule_ids": [1]}]'
            },
            id="rule-id-not-a-string",
        ),
        pytest.param(
            {
                "criteria": '[{"predicate_id": 5, "passed": true, "evidence_ref": "/p", "rule_ids": ["r"]}]'
            },
            id="predicate-id-not-a-string",
        ),
        pytest.param(
            {
                "criteria": '[{"predicate_id": "p", "passed": true, "evidence_ref": 5, "rule_ids": ["r"]}]'
            },
            id="evidence-not-a-string",
        ),
        pytest.param(
            {
                "criteria": '[{"predicate_id": "p", "passed": true, "evidence_ref": "/p", "rule_ids": "r"}]'
            },
            id="rule-ids-not-a-list",
        ),
        pytest.param({"criteria_total": 2, "criteria": _criteria(True)}, id="total-disagrees"),
        pytest.param(
            {"criteria": _criteria(False)}, id="accepted-but-a-criterion-failed"
        ),  # counts say 0 failed
        pytest.param(
            {"outcome": REJECTED, "criteria": _criteria(True), "criteria_failed": 1},
            id="rejected-but-every-criterion-passed",
        ),
        pytest.param({"observation_digest": "z" * 64}, id="digest-not-hex"),
        pytest.param({"observation_digest": "A" * 64}, id="digest-not-lowercase"),
    ],
)
def test_a_row_that_states_one_thing_and_holds_another_is_refused_on_read(
    dialect: str, overrides: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        decision_id = _insert_raw(engine, **overrides)
        with Session(engine) as session:
            with pytest.raises(AcceptanceError) as caught:
                read_decision(session, decision_id)
            assert caught.value.code == DECISION_CORRUPT and decision_id in str(caught.value)
            with pytest.raises(AcceptanceError):
                decisions_for_run(session, RUN)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_reading_an_unknown_decision_is_a_key_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine, Session(engine) as session:
        with pytest.raises(KeyError):
            read_decision(session, "acc-nope")
        assert (
            find_decision(
                session,
                workflow_run_id=RUN,
                candidate_id=CANDIDATE,
                criteria_version="harborlight-v1",
                observation_digest="0" * 64,
            )
            is None
        )


def _forged(**overrides: Any) -> CaliberAcceptanceDecision:
    """A row object as the database could never hold it (the CHECKs refuse these), to test the reader's own checks."""
    values: dict[str, Any] = {
        "decision_id": "acc-forged",
        "workflow_run_id": RUN,
        "candidate_id": CANDIDATE,
        "criteria_version": "harborlight-v1",
        "observation_digest": "c" * 64,
        "outcome": ACCEPTED,
        "criteria": [
            {"predicate_id": "p", "passed": True, "evidence_ref": "/p", "rule_ids": ["r"]}
        ],
        "criteria_total": 1,
        "criteria_failed": 0,
        "reason_code": None,
        "reason_detail": None,
        "decided_at": datetime(2026, 11, 1, 12, 0, 0),
    }
    values.update(overrides)
    return CaliberAcceptanceDecision(**values)


def test_the_reader_accepts_exactly_the_three_shapes_the_writer_makes() -> None:
    assert decision_module._decision(_forged()).accepted
    failed = [{"predicate_id": "p", "passed": False, "evidence_ref": "/p", "rule_ids": ["r"]}]
    rejected = decision_module._decision(
        _forged(outcome=REJECTED, criteria=failed, criteria_failed=1)
    )
    assert rejected.failed_criteria == ("p",)
    unreadable = decision_module._decision(
        _forged(
            outcome=REJECTED,
            criteria=[],
            criteria_total=0,
            reason_code="invalid_observations",
            reason_detail="/a is not a finite number",
        )
    )
    assert unreadable.reason_code == "invalid_observations" and unreadable.criteria == ()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"reason_code": "invalid_observations"}, id="accepted-with-a-reason-code"),
        pytest.param({"reason_detail": "x"}, id="accepted-with-a-detail"),
        pytest.param({"criteria": [], "criteria_total": 0}, id="accepted-over-nothing"),
        pytest.param({"outcome": "maybe"}, id="outcome-outside-the-set"),
        pytest.param({"outcome": REJECTED}, id="rejected-with-all-criteria-passed"),
        pytest.param(
            {"outcome": REJECTED, "criteria_failed": 1, "reason_detail": "x"},
            id="rejected-by-criteria-with-a-stray-detail",
        ),
        pytest.param(
            {"outcome": REJECTED, "criteria_failed": 1, "reason_code": "invalid_observations"},
            id="criteria-and-a-reason-together",
        ),
        pytest.param(
            {
                "outcome": REJECTED,
                "criteria": [],
                "criteria_total": 0,
                "reason_code": "invalid_observations",
                "reason_detail": "",
            },
            id="reason-with-an-empty-detail",
        ),
        pytest.param(
            {
                "outcome": REJECTED,
                "criteria": [],
                "criteria_total": 0,
                "reason_code": "something_else",
                "reason_detail": "x",
            },
            id="reason-code-outside-the-set",
        ),
        pytest.param(
            {
                "outcome": ACCEPTED,
                "criteria": [],
                "criteria_total": 0,
                "reason_code": "invalid_observations",
                "reason_detail": "x",
            },
            id="accepted-unreadable",
        ),
        pytest.param({"observation_digest": "short"}, id="digest-too-short"),
        pytest.param(
            {"criteria_failed": 1}, id="accepted-with-a-failed-count-over-passing-criteria"
        ),
        pytest.param({"criteria_total": 2}, id="total-count-disagrees"),
        pytest.param(
            {
                "outcome": REJECTED,
                "criteria": [
                    {"predicate_id": "p", "passed": False, "evidence_ref": "/p", "rule_ids": ["r"]}
                ],
                "criteria_failed": 2,
            },
            id="rejected-with-a-failed-count-above-the-failures",
        ),
    ],
)
def test_the_reader_refuses_every_other_shape(overrides: dict[str, Any]) -> None:
    with pytest.raises(AcceptanceError) as caught:
        decision_module._decision(_forged(**overrides))
    assert caught.value.code == DECISION_CORRUPT


# ------------------------------------------------------------------------------ the table itself


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_orm_refuses_an_edit_or_a_delete_of_a_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        decision_id = record(engine).decision.decision_id
        with Session(engine) as session:
            row = session.get(CaliberAcceptanceDecision, decision_id)
            assert row is not None
            row.outcome = REJECTED
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            row = session.get(CaliberAcceptanceDecision, decision_id)
            session.delete(row)
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            with pytest.raises(ImmutableRecordError):
                session.execute(update(CaliberAcceptanceDecision).values(outcome=REJECTED))
            with pytest.raises(ImmutableRecordError):
                session.execute(delete(CaliberAcceptanceDecision))
        assert rows(engine)[0].outcome == ACCEPTED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_database_refuses_update_and_delete_of_a_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        record(engine, _observations("stale_case_version"))
        for statement in (
            "UPDATE caliber_acceptance_decisions SET outcome = 'accepted'",
            "UPDATE caliber_acceptance_decisions SET criteria_failed = 0, outcome = 'accepted'",
            "UPDATE caliber_acceptance_decisions SET decided_at = decided_at",
            "DELETE FROM caliber_acceptance_decisions",
        ):
            with pytest.raises(IntegrityError, match="is immutable"):
                with engine.begin() as connection:
                    connection.execute(text(statement))
        (row,) = rows(engine)
        assert (row.outcome, row.criteria_failed) == (REJECTED, 1)


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "outcome": ACCEPTED,
        "criteria": _criteria(True),
        "criteria_total": 1,
        "criteria_failed": 0,
        "reason_code": None,
        "reason_detail": None,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param(_raw(outcome="maybe"), "ck_acceptance_decision_critical", id="outcome-domain"),
        pytest.param(
            _raw(criteria=_criteria(False), criteria_failed=1),
            "ck_acceptance_decision_critical",
            id="accepted-with-a-failed-criterion",
        ),
        pytest.param(
            _raw(
                reason_code="invalid_observations",
                reason_detail="x",
                criteria="[]",
                criteria_total=0,
            ),
            "ck_acceptance_decision_critical",
            id="accepted-with-a-reason",
        ),
        pytest.param(
            _raw(outcome=REJECTED),
            "ck_acceptance_decision_critical",
            id="rejected-with-nothing-wrong",
        ),
        pytest.param(
            _raw(criteria="[]", criteria_total=0),
            "ck_acceptance_decision_reason",
            id="accepted-over-no-criteria",
        ),
        pytest.param(
            _raw(
                outcome=REJECTED,
                criteria="[]",
                criteria_total=0,
                reason_code="something_else",
                reason_detail="x",
            ),
            "ck_acceptance_decision_reason",
            id="reason-code-outside-the-set",
        ),
        pytest.param(
            _raw(
                outcome=REJECTED,
                criteria="[]",
                criteria_total=0,
                reason_code="invalid_observations",
            ),
            "ck_acceptance_decision_reason",
            id="reason-without-detail",
        ),
        pytest.param(
            _raw(
                outcome=REJECTED,
                criteria="[]",
                criteria_total=0,
                reason_code="invalid_observations",
                reason_detail="",
            ),
            "ck_acceptance_decision_reason",
            id="reason-with-empty-detail",
        ),
        pytest.param(
            _raw(outcome=REJECTED, criteria_failed=1, reason_detail="orphan detail"),
            "ck_acceptance_decision_reason",
            id="detail-without-a-reason",
        ),
        pytest.param(
            _raw(outcome=REJECTED, reason_code="invalid_observations", reason_detail="x"),
            "ck_acceptance_decision_reason",
            id="reason-and-criteria-together",
        ),
        pytest.param(_raw(criteria_total=-1), "ck_acceptance_decision_counts", id="negative-total"),
        pytest.param(
            _raw(outcome=REJECTED, criteria_failed=2, criteria_total=1),
            "ck_acceptance_decision_counts",
            id="failed-above-total",
        ),
        pytest.param(
            _raw(criteria_failed=-1), "ck_acceptance_decision_counts", id="negative-failed"
        ),
    ],
)
def test_the_table_refuses_a_row_the_code_never_writes(
    dialect: str,
    overrides: dict[str, Any],
    constraint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        with pytest.raises(IntegrityError) as caught:
            _insert_raw(engine, **overrides)
        if dialect == "postgresql":
            assert constraint in str(caught.value)
        assert rows(engine) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_table_refuses_a_bad_digest_an_empty_identity_and_a_duplicate_key(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        for bad in (
            {"observation_digest": "a" * 63},
            {"observation_digest": "a" * 65},
            {"candidate_id": ""},
            {"criteria_version": ""},
        ):
            with pytest.raises(
                (IntegrityError, DataError)
            ):  # the CHECK, or PostgreSQL's varchar(64)
                _insert_raw(engine, **bad)
        first = record(engine).decision
        with pytest.raises(
            IntegrityError
        ):  # the key: the same run, candidate, criteria version and digest
            _insert_raw(
                engine,
                workflow_run_id=RUN,
                candidate_id=first.candidate_id,
                criteria_version=first.criteria_version,
                observation_digest=first.observation_digest,
            )
        _insert_raw(  # any one part of the key different is another decision
            engine,
            candidate_id=first.candidate_id,
            criteria_version="harborlight-v2",
            observation_digest=first.observation_digest,
        )
        assert len(rows(engine)) == 2


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="foreign keys are enforced on PostgreSQL only")
def test_postgresql_enforces_the_run_foreign_key_and_refuses_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db("postgresql", tmp_path, monkeypatch) as engine:
        with pytest.raises(IntegrityError, match="foreign key"):
            _insert_raw(engine, workflow_run_id="RUN-NOT-THERE")
        record(engine)
        with pytest.raises(IntegrityError, match="TRUNCATE refused"):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE caliber_acceptance_decisions"))
        assert len(rows(engine)) == 1


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="privileges are PostgreSQL-only")
def test_postgresql_public_holds_no_update_delete_or_truncate_on_the_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db("postgresql", tmp_path, monkeypatch) as engine:
        with engine.connect() as connection:
            granted = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'caliber_acceptance_decisions' AND grantee = 'PUBLIC'"
                    )
                )
            }
        assert not granted & {"UPDATE", "DELETE", "TRUNCATE"}


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="roles are PostgreSQL-only")
def test_a_runtime_role_may_insert_and_read_decisions_but_not_change_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role with only the privileges the runtime needs cannot UPDATE, DELETE or TRUNCATE; one granted them anyway is stopped by the trigger."""
    with acceptance_db("postgresql", tmp_path, monkeypatch) as engine:
        role = f"role_acc_{uuid.uuid4().hex[:10]}"
        with engine.begin() as connection:
            connection.execute(text(f'CREATE ROLE "{role}"'))
            connection.execute(text(f'GRANT SELECT ON caliber_workflow_runs TO "{role}"'))
            connection.execute(
                text(f'GRANT SELECT, INSERT ON caliber_acceptance_decisions TO "{role}"')
            )
        try:
            digest = "b" * 64
            with engine.begin() as connection:
                connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                connection.execute(
                    text(
                        "INSERT INTO caliber_acceptance_decisions (decision_id, workflow_run_id, candidate_id, "
                        "criteria_version, observation_digest, outcome, criteria, criteria_total, criteria_failed, "
                        "decided_at) VALUES ('acc-role', :run, 'cand-role', 'harborlight-v1', :digest, 'accepted', "
                        "CAST(:criteria AS JSON), 1, 0, now())"
                    ),
                    {"run": RUN, "digest": digest, "criteria": _criteria(True)},
                )
            for statement in (
                "UPDATE caliber_acceptance_decisions SET outcome = 'rejected'",
                "DELETE FROM caliber_acceptance_decisions",
                "TRUNCATE caliber_acceptance_decisions",
            ):
                with pytest.raises(DBAPIError, match="permission denied"):
                    with engine.begin() as connection:
                        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                        connection.execute(text(statement))
            assert len(rows(engine)) == 1
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f'GRANT UPDATE, DELETE, TRUNCATE ON caliber_acceptance_decisions TO "{role}"'
                    )
                )
            for statement in (
                "UPDATE caliber_acceptance_decisions SET outcome = 'rejected'",
                "DELETE FROM caliber_acceptance_decisions",
                "TRUNCATE caliber_acceptance_decisions",
            ):
                with pytest.raises(IntegrityError, match="is immutable"):
                    with engine.begin() as connection:
                        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                        connection.execute(text(statement))
            assert [r.outcome for r in rows(engine)] == [ACCEPTED]
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f'REVOKE ALL ON caliber_workflow_runs, caliber_acceptance_decisions FROM "{role}"'
                    )
                )
                connection.execute(text(f'DROP ROLE "{role}"'))


def test_the_model_carries_the_unique_key_and_the_checks() -> None:
    table = CaliberAcceptanceDecision.__table__
    assert {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"} == {
        "ck_acceptance_decision_identity",
        "ck_acceptance_decision_digest",
        "ck_acceptance_decision_counts",
        "ck_acceptance_decision_reason",
        "ck_acceptance_decision_critical",
    }
    unique = {
        tuple(column.name for column in c.columns)
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert unique == {("workflow_run_id", "candidate_id", "criteria_version", "observation_digest")}
    assert table.c.criteria.nullable is False and table.c.reason_code.nullable is True
    assert [fk.target_fullname for fk in table.c.workflow_run_id.foreign_keys] == [
        "caliber_workflow_runs.workflow_run_id"
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migrated_table_has_the_models_columns(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acceptance_db(dialect, tmp_path, monkeypatch) as engine:
        columns = {
            c["name"]: c for c in sa_inspect(engine).get_columns("caliber_acceptance_decisions")
        }
        assert set(columns) == {c.name for c in CaliberAcceptanceDecision.__table__.columns}
        assert (
            columns["criteria"]["nullable"] is False
            and columns["reason_detail"]["nullable"] is True
        )
