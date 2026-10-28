"""Verified effect receipts and settling a dispatch (B05.4a, #717).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by
Alembic. The end-to-end cases talk to the downstream fixture process through the real executor (no mocked transport).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts import ContractValidationError, validate_record
from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import CaliberEffectReceipt
from caliber.effects.connectors import (
    Classified,
    TransportOutcome,
    classify_error,
    classify_result,
)
from caliber.effects.connectors.document_request import (
    CAPABILITY,
    CONNECTOR,
    VERSION,
    facts_from_lookup,
    lookup_config,
    lookup_input,
    submit_config,
    submit_input,
)
from caliber.effects.receipts import (
    RECEIPT_CONFLICT,
    RECEIPT_CORRUPT,
    RECEIPT_MISMATCH,
    RECEIPT_STATE,
    ReceiptConflict,
    ReceiptFacts,
    ReceiptMismatch,
    read_receipt,
    record_receipt,
    settle_dispatch,
)
from caliber.effects.records import (
    APPROVED,
    BAD_FIELD,
    COMMITTED,
    DISPATCHED,
    EVIDENCE_REQUIRED,
    FAILED,
    POLICY_DECISION,
    RECEIPT_REFUSED,
    RECONCILED,
    UNKNOWN,
    Approval,
    Intent,
    ProposalError,
    proposal_state,
    proposal_view,
    record_proposal,
    transition,
)
from caliber.integrations.openapi.executor import OpenApiExecutionError, execute_openapi_http_tool
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.test_effect_proposals import _seed_run
from tests.test_effect_records import AT, ATTEMPT, NAIVE_AT, OWNER, _decision, _move
from tests.test_event_outbox import ALEMBIC_INI, _count, _database

CASE = "MORT-2026-000184"
DOC = "pay_statement"


@pytest.fixture(scope="module")
def _downstream_process(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from tests.egi_scenarios.conftest import Downstream

    handle = Downstream(tmp_path_factory.mktemp("downstream"))
    try:
        handle.start()
        yield handle
    finally:
        handle.close()


@pytest.fixture
def downstream(_downstream_process: Any) -> Any:
    from caliber.egress import EgressPolicy
    from caliber.integrations.openapi.executor import bind_egress_policy

    _downstream_process.reset()
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield _downstream_process
    bind_egress_policy(None)


def _proposal(
    session: Session,
    *,
    occurrence: str = "occ-1",
    document_type: str = DOC,
    expected: str | None = "1",
) -> str:
    """A read proposal for the document-request connector, approved and dispatched (attempt 1)."""
    recorded = record_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="ask",
        occurrence_id=occurrence,
        intent=Intent(
            connector=CONNECTOR,
            connector_version=VERSION,
            effect_class="read",
            resource=f"cases/{CASE}/document-requests",
            args={"document_type": document_type},
            expected_resource_version=expected,
        ),
    )
    proposal_id = recorded.proposal_id
    decision = _decision(session, proposal_id, decision_id=f"pd-{proposal_id}")
    _move(session, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision))
    _move(session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=ATTEMPT)
    return proposal_id


def _facts(session: Session, proposal_id: str, **overrides: Any) -> ReceiptFacts:
    view = proposal_view(session, proposal_id)
    fields: dict[str, Any] = {
        "attempt": view.attempt,
        "idempotency_key": view.logical_key,
        "connector": view.connector,
        "connector_version": view.connector_version,
        "resource": view.resource,
        "intent_hash": view.intent_hash,
        "downstream_request_id": f"DR-{proposal_id[-8:]}",
        "response_digest": "sha256:" + "b" * 64,
        "resource_version": "2",
    }
    fields.update(overrides)
    return ReceiptFacts(**fields)


def _refused(session: Session, proposal_id: str, code: str, **overrides: Any) -> ProposalError:
    with pytest.raises(ProposalError) as refused:
        record_receipt(session, proposal_id, _facts(session, proposal_id, **overrides), now=AT)
    assert refused.value.code == code, refused.value
    return refused.value


# ---------------------------------------------------------------- recording and reading


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_verified_receipt_is_recorded_once_and_reads_back_as_a_valid_v1_record(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            facts = _facts(session, proposal_id)
            first = record_receipt(session, proposal_id, facts, now=AT)
            again = record_receipt(session, proposal_id, facts, now=AT)
            session.commit()
        assert (first.created, again.created) == (True, False)
        assert again.receipt_id == first.receipt_id
        assert _count(factory, CaliberEffectReceipt) == 1
        with factory() as session:
            record = read_receipt(session, first.receipt_id)
            view = proposal_view(session, proposal_id)
        validate_record("receipt", record)
        assert record == {
            "schema_version": "v1",
            "receipt_id": first.receipt_id,
            "proposal_id": proposal_id,
            "downstream_request_id": facts.downstream_request_id,
            "response_digest": "sha256:" + "b" * 64,
            "verified_by": "query",
            "verified_at": "2026-03-04T05:06:07.000000Z",
            "attempt": 1,
            "logical_key": view.logical_key,
            "intent_hash": view.intent_hash,
            "connector": CONNECTOR,
            "connector_version": VERSION,
            "resource": f"cases/{CASE}/document-requests",
            "resource_version": "2",
        }


def test_the_receipt_contract_still_accepts_a_record_written_without_the_new_fields() -> None:
    from tests.test_contracts_registry import valid_record

    validate_record("receipt", valid_record("receipt"))
    for bad in ({"attempt": 0}, {"logical_key": ""}, {"resource_version": ""}, {"surprise": 1}):
        with pytest.raises(ContractValidationError):
            validate_record("receipt", {**valid_record("receipt"), **bad})


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt", 2),
        ("idempotency_key", "some-other-key"),
        ("connector", "another-connector"),
        ("connector_version", "9"),
        ("resource", "cases/OTHER/document-requests"),
        ("intent_hash", "sha256:" + "c" * 64),
    ],
)
def test_a_receipt_that_is_not_this_proposals_effect_is_refused_naming_the_field_never_the_value(
    dialect: str, field: str, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            refused = _refused(session, proposal_id, RECEIPT_MISMATCH, **{field: value})
            assert isinstance(refused, ReceiptMismatch) and refused.fields == (field,)
            assert str(value) not in str(refused) and field in str(refused)
            many = _refused(
                session,
                proposal_id,
                RECEIPT_MISMATCH,
                intent_hash="sha256:" + "d" * 64,
                connector="x",
            )
            assert isinstance(many, ReceiptMismatch) and many.fields == ("connector", "intent_hash")
            session.rollback()
        assert _count(factory, CaliberEffectReceipt) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_attempt_cannot_get_a_second_different_authoritative_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            first = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            for change in (
                {"downstream_request_id": "DR-OTHER"},
                {"resource_version": "3"},
                {"resource_version": None},
            ):
                conflict = _refused(session, proposal_id, RECEIPT_CONFLICT, **change)
                assert isinstance(conflict, ReceiptConflict)
            session.commit()
        with factory() as session:
            assert read_receipt(session, first.receipt_id)["downstream_request_id"] != "DR-OTHER"
        assert _count(factory, CaliberEffectReceipt) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_downstream_request_id_cannot_be_borrowed_by_another_proposal(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _proposal(session)
            second = _proposal(session, occurrence="occ-2")
            record_receipt(
                session, first, _facts(session, first, downstream_request_id="DR-SHARED"), now=AT
            )
            _refused(session, second, RECEIPT_CONFLICT, downstream_request_id="DR-SHARED")
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_receipt_can_be_recorded_only_while_the_outcome_is_open(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            early = record_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="ask",
                occurrence_id="occ-early",
                intent=Intent(
                    connector=CONNECTOR,
                    connector_version=VERSION,
                    effect_class="read",
                    resource=f"cases/{CASE}/document-requests",
                    args={"document_type": DOC},
                    expected_resource_version="1",
                ),
            ).proposal_id
            # a proposed or approved proposal has no dispatch attempt: nothing to answer
            assert _facts_or_none(session, early) is None
            with pytest.raises(ProposalError) as too_early:
                record_receipt(session, early, _synthetic(session, early), now=AT)
            assert too_early.value.code == RECEIPT_STATE
            decision = _decision(session, early, decision_id="pd-early")
            _move(session, early, APPROVED, approval=Approval(POLICY_DECISION, decision))
            with pytest.raises(ProposalError) as still_early:
                record_receipt(session, early, _synthetic(session, early), now=AT)
            assert still_early.value.code == RECEIPT_STATE
            late = _proposal(session, occurrence="occ-late")
            record_receipt(session, late, _facts(session, late), now=AT)
            _move(session, late, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
            record_receipt(
                session, late, _facts(session, late), now=AT
            )  # unknown still takes it (a duplicate)
            _move(session, late, RECONCILED, reason="found by key")
            record_receipt(session, late, _facts(session, late), now=AT)
            session.commit()
        with factory() as session:
            settled = _proposal(session, occurrence="occ-settled")
            _move(session, settled, FAILED, owner_id=OWNER, attempt=ATTEMPT, reason="rejected")
            _refused(session, settled, RECEIPT_STATE)
            with pytest.raises(KeyError):
                record_receipt(session, "prop-missing", _synthetic(session, settled), now=AT)
            with pytest.raises(ProposalError) as not_facts:
                record_receipt(session, settled, {"attempt": 1}, now=AT)  # type: ignore[arg-type]
            assert not_facts.value.code == BAD_FIELD


def _synthetic(session: Session, proposal_id: str) -> ReceiptFacts:
    view = proposal_view(session, proposal_id)
    return ReceiptFacts(
        attempt=1,
        idempotency_key=view.logical_key,
        connector=view.connector,
        connector_version=view.connector_version,
        resource=view.resource,
        intent_hash=view.intent_hash,
        downstream_request_id="DR-synthetic",
        response_digest="sha256:" + "f" * 64,
    )


def _facts_or_none(session: Session, proposal_id: str) -> ReceiptFacts | None:
    view = proposal_view(session, proposal_id)
    return None if view.attempt is None else _synthetic(session, proposal_id)


@pytest.mark.parametrize(
    "bad",
    [
        {"attempt": 0},
        {"attempt": -1},
        {"attempt": True},
        {"attempt": "1"},
        {"attempt": 2**31},
        {"idempotency_key": ""},
        {"idempotency_key": "k" * 256},
        {"connector": "  "},
        {"connector": "a\x00b"},
        {"resource": "r" * 513},
        {"downstream_request_id": ""},
        {"downstream_request_id": "d" * 257},
        {"downstream_request_id": 5},
        {"resource_version": ""},
        {"resource_version": "v" * 257},
        {"resource_version": 2},
        {"intent_hash": "abc"},
        {"intent_hash": "sha256:" + "A" * 64},
        {"response_digest": ""},
        {"response_digest": "md5:" + "a" * 64},
        {"response_digest": None},
    ],
)
def test_malformed_receipt_facts_are_refused_when_built(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "attempt": 1,
        "idempotency_key": "k",
        "connector": "c",
        "connector_version": "1",
        "resource": "r",
        "intent_hash": "sha256:" + "a" * 64,
        "downstream_request_id": "d",
        "response_digest": "sha256:" + "b" * 64,
    }
    fields.update(bad)
    with pytest.raises(ProposalError) as refused:
        ReceiptFacts(**fields)
    assert refused.value.code == BAD_FIELD


# ---------------------------------------------------------------- a tampered receipt is detected


def _tamper(engine: Any, dialect: str, statement: str, **params: Any) -> None:
    with engine.begin() as connection:
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_receipts DISABLE TRIGGER USER"))
        else:
            connection.execute(text("DROP TRIGGER trg_caliber_effect_receipts_no_update"))
        connection.execute(text(statement), params)
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_receipts ENABLE TRIGGER USER"))


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "assignment",
    [
        "downstream_request_id = 'DR-FORGED'",
        "response_digest = 'sha256:' || repeat('0', 64)",
        "resource_version = '99'",
        "attempt = 2",
        "verified_at = '2001-01-01 00:00:00'",
        "intent_hash = 'sha256:' || repeat('1', 64)",
        "logical_key = repeat('2', 64)",
        "connector = 'forged'",
        "resource = 'cases/FORGED/document-requests'",
        "receipt_bytes = receipt_bytes || x'20'",
    ],
)
def test_a_receipt_edited_around_the_triggers_is_detected_when_it_is_read(
    dialect: str, assignment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if dialect == "postgresql":
        assignment = assignment.replace("x'20'", "'\\x20'::bytea")
    else:
        assignment = (
            assignment.replace("repeat('0', 64)", "'" + "0" * 64 + "'")
            .replace("repeat('1', 64)", "'" + "1" * 64 + "'")
            .replace("repeat('2', 64)", "'" + "2" * 64 + "'")
            .replace("'sha256:' || '", "'sha256:")
            .replace("' || '", "")
        )
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            receipt = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            session.commit()
        if dialect == "postgresql" and "receipt_bytes" in assignment:
            # the CHECK that ties the digest to the bytes still holds with the triggers off: the edit itself is refused
            with pytest.raises(DBAPIError, match="ck_effect_receipt_digest_is_sha256"):
                _tamper(engine, dialect, f"UPDATE caliber_effect_receipts SET {assignment}")
            with factory() as session:
                assert read_receipt(session, receipt.receipt_id)["proposal_id"] == proposal_id
            return
        _tamper(engine, dialect, f"UPDATE caliber_effect_receipts SET {assignment}")
        with factory() as session, pytest.raises(ProposalError) as corrupt:
            read_receipt(session, receipt.receipt_id)
        assert corrupt.value.code == RECEIPT_CORRUPT


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_receipt_is_immutable_in_the_database_and_the_orm(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            receipt = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            session.commit()
        for statement in (
            "UPDATE caliber_effect_receipts SET resource_version = '9'",
            "UPDATE caliber_effect_receipts SET attempt = 1",
            "DELETE FROM caliber_effect_receipts",
        ):
            with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
                connection.execute(text(statement))
        with factory() as session:
            row = session.get(CaliberEffectReceipt, receipt.receipt_id)
            assert row is not None
            row.resource_version = "9"
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            row = session.get(CaliberEffectReceipt, receipt.receipt_id)
            assert row is not None
            session.delete(row)
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
        assert _count(factory, CaliberEffectReceipt) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_table_refuses_an_unverified_receipt_and_a_digest_that_is_not_the_bytes(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            session.commit()
        insert = text(
            "INSERT INTO caliber_effect_receipts (receipt_id, proposal_id, attempt, logical_key, intent_hash, connector, "
            "connector_version, resource, downstream_request_id, response_digest, verified_by, verified_at, receipt_bytes, "
            "receipt_digest) VALUES (:id, :p, 1, :k, :h, 'c', 'v', 'r', :d, :rd, :vb, CURRENT_TIMESTAMP, :b, :bd)"
        )
        good = {
            "p": proposal_id,
            "k": view.logical_key,
            "h": view.intent_hash,
            "rd": "sha256:" + "a" * 64,
            "vb": "query",
            "b": b"x",
        }
        import hashlib

        good["bd"] = hashlib.sha256(b"x").hexdigest()
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(insert, {**good, "id": "r1", "d": "d1", "vb": "none"})  # unverified
        if dialect == "postgresql":
            with (
                pytest.raises(DBAPIError, match="ck_effect_receipt_digest_is_sha256"),
                engine.begin() as connection,
            ):
                connection.execute(insert, {**good, "id": "r2", "d": "d2", "bd": "0" * 64})
        with engine.begin() as connection:
            connection.execute(insert, {**good, "id": "r3", "d": "d3"})
        assert _count(factory, CaliberEffectReceipt) == 1


# ---------------------------------------------------------------- committing needs a verified receipt


@pytest.mark.parametrize("dialect", DIALECTS)
def test_committed_requires_verified_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            other = _proposal(session, occurrence="occ-2")
            foreign = record_receipt(session, other, _facts(session, other), now=AT).receipt_id
            version = proposal_state(session, proposal_id).version

            def commit(**evidence: Any) -> Any:
                return transition(
                    session,
                    proposal_id,
                    COMMITTED,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    now=AT,
                    **evidence,
                )

            with pytest.raises(ProposalError) as none:
                commit()
            assert none.value.code == EVIDENCE_REQUIRED
            for bad in ("rcpt-missing", foreign):  # unknown, and another proposal's
                with pytest.raises(ProposalError) as refused:
                    commit(receipt_id=bad)
                assert refused.value.code == RECEIPT_REFUSED
            own = record_receipt(
                session, proposal_id, _facts(session, proposal_id), now=AT
            ).receipt_id
            assert commit(receipt_id=own).applied
            state = proposal_state(session, proposal_id)
            assert (state.state, state.receipt_id) == (COMMITTED, own)
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_repeated_commit_with_the_same_receipt_is_recorded_once_and_without_it_is_not_a_repeat(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            receipt = record_receipt(
                session, proposal_id, _facts(session, proposal_id), now=AT
            ).receipt_id
            version = proposal_state(session, proposal_id).version

            def commit(**evidence: Any) -> Any:
                return transition(
                    session,
                    proposal_id,
                    COMMITTED,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    now=AT,
                    **evidence,
                )

            assert commit(receipt_id=receipt).applied is True
            assert commit(receipt_id=receipt).applied is False, (
                "the same actor repeating its commit"
            )
            with pytest.raises(ProposalError) as unproven:
                commit()  # a caller that cannot present the receipt is not told it was already recorded
            assert unproven.value.code == "stale_version"
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_raw_sql_cannot_commit_a_proposal_without_a_receipt_of_its_own(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            other = _proposal(session, occurrence="occ-other")
            own = record_receipt(
                session, proposal_id, _facts(session, proposal_id), now=AT
            ).receipt_id
            foreign = record_receipt(session, other, _facts(session, other), now=AT).receipt_id
            session.commit()
        commit = (
            "UPDATE caliber_effect_proposals SET state = 'committed', version = 4, settled_at = CURRENT_TIMESTAMP"
            "{extra} WHERE proposal_id = :p"
        )
        for extra, match in (
            ("", "evidence|needs a receipt"),  # no receipt (both rules refuse it)
            (", receipt_id = 'rcpt-any'", "needs a receipt"),  # one that does not exist
            (f", receipt_id = '{foreign}'", "needs a receipt"),  # another proposal's
        ):
            with pytest.raises(DBAPIError, match=match), engine.begin() as connection:
                connection.execute(text(commit.format(extra=extra)), {"p": proposal_id})
        with engine.begin() as connection:
            connection.execute(
                text(commit.format(extra=f", receipt_id = '{own}'")), {"p": proposal_id}
            )
        with factory() as session:
            assert proposal_state(session, proposal_id).state == COMMITTED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_proposal_with_a_verified_receipt_cannot_be_failed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downstream's own query said it committed: ``failed`` is final, so it would leave a proposal that says it did not
    happen next to a receipt that proves it did."""
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            version = proposal_state(session, proposal_id).version
            with pytest.raises(ProposalError) as refused:
                transition(
                    session,
                    proposal_id,
                    FAILED,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="rejected",
                )
            assert refused.value.code == "receipt_exists"
            session.commit()
        with (
            pytest.raises(DBAPIError, match="cannot be failed"),
            engine.begin() as connection,
        ):  # and not by raw SQL
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET state = 'failed', version = 4, settled_at = CURRENT_TIMESTAMP, "
                    "reason = 'x' WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )
        with factory() as session:
            assert proposal_state(session, proposal_id).state == DISPATCHED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_database_refuses_a_receipt_for_a_proposal_that_is_not_open(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            settled = _proposal(session)
            _move(session, settled, FAILED, owner_id=OWNER, attempt=ATTEMPT, reason="rejected")
            view = proposal_view(session, settled)
            session.commit()
        import hashlib

        insert = text(
            "INSERT INTO caliber_effect_receipts (receipt_id, proposal_id, attempt, logical_key, intent_hash, connector, "
            "connector_version, resource, downstream_request_id, response_digest, verified_by, verified_at, receipt_bytes, "
            "receipt_digest) VALUES ('r1', :p, 1, :k, :h, 'c', 'v', 'r', 'd1', :rd, 'query', CURRENT_TIMESTAMP, :b, :bd)"
        )
        params = {
            "p": settled,
            "k": view.logical_key,
            "h": view.intent_hash,
            "rd": "sha256:" + "a" * 64,
            "b": b"x",
            "bd": hashlib.sha256(b"x").hexdigest(),
        }
        with (
            pytest.raises(DBAPIError, match="dispatched, unknown or reconciled"),
            engine.begin() as connection,
        ):
            connection.execute(insert, params)
        assert _count(factory, CaliberEffectReceipt) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_stale_settle_is_refused_before_a_query_is_made_and_records_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.effects.records import StaleVersion

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            version = proposal_state(session, proposal_id).version
            claim = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1)
            queried: list[bool] = []

            def verify() -> ReceiptFacts | None:
                queried.append(True)
                return _facts(session, proposal_id)

            def settle(**overrides: Any) -> Any:
                arguments: dict[str, Any] = {
                    "expected_version": version,
                    "owner_id": OWNER,
                    "attempt": ATTEMPT,
                    "outcome": claim,
                    "verify": verify,
                    "now": AT,
                }
                arguments.update(overrides)
                return settle_dispatch(session, proposal_id, **arguments)

            with pytest.raises(ProposalError) as other_owner:
                settle(owner_id="worker-2")
            assert other_owner.value.code == "stale_owner"
            with pytest.raises(ProposalError) as other_attempt:
                settle(attempt=2)
            assert other_attempt.value.code == "stale_owner"
            with pytest.raises(StaleVersion):
                settle(expected_version=version + 5)
            assert queried == [] and _count_receipts(session) == 0
            assert settle().state == COMMITTED
            with pytest.raises(StaleVersion):  # a repeat of a settle that already happened
                settle()
            queried.clear()
            unknown = _proposal(session, occurrence="occ-u")
            _move(session, unknown, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
            with pytest.raises(ProposalError) as not_dispatched:
                settle_dispatch(
                    session,
                    unknown,
                    expected_version=proposal_state(session, unknown).version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    outcome=claim,
                    verify=verify,
                )
            assert not_dispatched.value.code == "illegal_transition" and queried == []


def _count_receipts(session: Session) -> int:
    return len(list(session.execute(select(CaliberEffectReceipt)).scalars()))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_receipt_is_not_kept_when_the_commit_is_refused_and_a_failing_query_changes_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.effects import receipts as receipts_module
    from caliber.effects.records import StaleVersion

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            version = proposal_state(session, proposal_id).version
            claim = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1)

            def raising() -> ReceiptFacts | None:
                raise RuntimeError("the lookup is down")

            with pytest.raises(RuntimeError):
                settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    outcome=claim,
                    verify=raising,
                )
            assert (
                proposal_state(session, proposal_id).state == DISPATCHED
                and _count_receipts(session) == 0
            )

            def refuse(
                *args: Any, **kwargs: Any
            ) -> Any:  # another transaction moved it after the precheck
                raise StaleVersion(proposal_id, version, version + 1, UNKNOWN)

            monkeypatch.setattr(receipts_module, "transition", refuse)
            with pytest.raises(StaleVersion):
                settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    outcome=claim,
                    verify=lambda: _facts(session, proposal_id),
                )
            session.commit()
        assert _count(factory, CaliberEffectReceipt) == 0, (
            "the receipt went with the refused commit"
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_redelivery_whose_lookup_body_gained_a_field_is_still_the_same_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            first = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            drifted = _facts(session, proposal_id, response_digest="sha256:" + "9" * 64)
            again = record_receipt(session, proposal_id, drifted, now=AT)
            assert (again.created, again.receipt_id) == (False, first.receipt_id)
            assert (
                read_receipt(session, first.receipt_id)["response_digest"] == "sha256:" + "b" * 64
            )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_receipt_belongs_to_the_attempt_that_dispatched_and_to_no_other_move(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            receipt = record_receipt(
                session, proposal_id, _facts(session, proposal_id), now=AT
            ).receipt_id
            version = proposal_state(session, proposal_id).version
            with pytest.raises(ProposalError) as wrong_move:
                transition(
                    session,
                    proposal_id,
                    UNKNOWN,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="timeout",
                    receipt_id=receipt,
                )
            assert wrong_move.value.code == BAD_FIELD
            with pytest.raises(ProposalError) as bad_text:
                transition(
                    session,
                    proposal_id,
                    COMMITTED,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    receipt_id="r" * 65,
                )
            assert bad_text.value.code == BAD_FIELD
            session.rollback()


# ---------------------------------------------------------------- settling a dispatch from what was proven


def _settle(
    session: Session, proposal_id: str, outcome: Classified, facts: ReceiptFacts | None
) -> Any:
    called: list[bool] = []

    def verify() -> ReceiptFacts | None:
        called.append(True)
        return facts

    settled = settle_dispatch(
        session,
        proposal_id,
        expected_version=proposal_state(session, proposal_id).version,
        owner_id=OWNER,
        attempt=ATTEMPT,
        outcome=outcome,
        verify=verify,
        now=AT,
    )
    return settled, bool(called)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_what_a_dispatch_proved_decides_what_is_recorded(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            cases = [
                (
                    Classified(TransportOutcome.DEFINITIVE_REJECTION, "http_412", 412, 1),
                    FAILED,
                    "rejected:http_412",
                ),
                (
                    Classified(TransportOutcome.PROVEN_NOT_SENT, "not_sent", None, 1),
                    FAILED,
                    "not_sent",
                ),
                (
                    Classified(TransportOutcome.AMBIGUOUS, "http_503", 503, 1),
                    UNKNOWN,
                    "ambiguous:http_503",
                ),
                (
                    Classified(TransportOutcome.AMBIGUOUS, "may_have_been_sent", None, 1),
                    UNKNOWN,
                    "ambiguous:may_have_been_sent",
                ),
            ]
            for number, (outcome, state, reason) in enumerate(cases):
                proposal_id = _proposal(session, occurrence=f"occ-{number}")
                settled, verified = _settle(session, proposal_id, outcome, None)
                assert (settled.state, settled.reason, settled.receipt_id, verified) == (
                    state,
                    reason,
                    None,
                    False,
                )
                recorded = proposal_state(session, proposal_id)
                assert (recorded.state, recorded.reason) == (state, reason)
            session.commit()
        assert _count(factory, CaliberEffectReceipt) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_commit_claim_with_a_confirming_query_commits_with_its_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            claim = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1)
            settled, verified = _settle(session, proposal_id, claim, _facts(session, proposal_id))
            assert verified and settled.state == COMMITTED and settled.receipt_id is not None
            state = proposal_state(session, proposal_id)
            assert (state.state, state.receipt_id) == (COMMITTED, settled.receipt_id)
            record = read_receipt(session, settled.receipt_id)
            assert record["proposal_id"] == proposal_id and record["verified_by"] == "query"
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_200_without_query_match_is_unknown(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2xx claim nobody could confirm is not a commit, and a lookup that finds nothing is not a failure."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            claim = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_200", 200, 1)
            settled, verified = _settle(session, proposal_id, claim, None)
            assert verified and (settled.state, settled.reason, settled.receipt_id) == (
                UNKNOWN,
                "receipt_unverified",
                None,
            )
            assert proposal_state(session, proposal_id).state == UNKNOWN
            session.commit()
        assert _count(factory, CaliberEffectReceipt) == 0


# ---------------------------------------------------------------- the document-request connector against its fixture


def _dispatch(downstream: Any, session: Session, proposal_id: str) -> Classified:
    view = proposal_view(session, proposal_id)
    result = execute_openapi_http_tool(
        execution_config=submit_config(downstream.base_url),
        input_schema=None,
        input_data=submit_input(
            case_id=CASE,
            idempotency_key=view.logical_key,
            expected_resource_version=int(view.expected_resource_version or 0),
            body={"document_type": DOC},
        ),
    )
    return classify_result(result, capability=CAPABILITY)


def _lookup(downstream: Any, key: str) -> dict[str, Any]:
    return execute_openapi_http_tool(
        execution_config=lookup_config(downstream.base_url),
        input_schema=None,
        input_data=lookup_input(key),
    )


def test_a_dispatch_the_downstream_confirms_by_query_is_committed_with_a_receipt(
    downstream: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            outcome = _dispatch(downstream, session, proposal_id)
            assert outcome.outcome is TransportOutcome.COMMITTED_RECEIPT

            def verify() -> ReceiptFacts | None:
                found = _lookup(downstream, view.logical_key)
                return facts_from_lookup(
                    view, lookup_status=found["status_code"], lookup_body=found["json"]
                )

            settled = settle_dispatch(
                session,
                proposal_id,
                expected_version=proposal_state(session, proposal_id).version,
                owner_id=OWNER,
                attempt=ATTEMPT,
                outcome=outcome,
                verify=verify,
                now=AT,
            )
            assert settled.state == COMMITTED and settled.receipt_id
            record = read_receipt(session, settled.receipt_id)
            assert (
                record["downstream_request_id"].startswith("DR-")
                and record["resource_version"] == "2"
            )
            assert (
                record["logical_key"] == view.logical_key
                and record["intent_hash"] == view.intent_hash
            )
            assert downstream.mutations() == 1


def test_a_lost_response_stays_unknown_until_a_lookup_confirms_it_and_only_then_commits(
    downstream: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            downstream.set_fault("drop_response_after_commit")
            with pytest.raises(OpenApiExecutionError) as lost:
                _dispatch(downstream, session, proposal_id)
            settled = settle_dispatch(
                session,
                proposal_id,
                expected_version=proposal_state(session, proposal_id).version,
                owner_id=OWNER,
                attempt=ATTEMPT,
                outcome=classify_error(lost.value),
                verify=lambda: None,
                now=AT,
            )
            assert (settled.state, settled.reason) == (UNKNOWN, "ambiguous:may_have_been_sent")
            assert downstream.mutations() == 1, (
                "it committed, and the platform honestly does not know yet"
            )
            # later: the lookup by key answers, the receipt is recorded while the proposal is unknown, then reconciled
            found = _lookup(downstream, view.logical_key)
            facts = facts_from_lookup(
                view, lookup_status=found["status_code"], lookup_body=found["json"]
            )
            assert facts is not None
            receipt = record_receipt(session, proposal_id, facts, now=AT)
            _move(session, proposal_id, RECONCILED, reason="found by key")
            moved = _move(session, proposal_id, COMMITTED, receipt_id=receipt.receipt_id)
            assert moved.state == COMMITTED
            assert proposal_state(session, proposal_id).receipt_id == receipt.receipt_id


def test_a_lookup_that_finds_nothing_or_something_else_is_not_a_receipt(
    downstream: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            nothing = _lookup(downstream, view.logical_key)  # nothing was dispatched: a 404
            assert nothing["status_code"] == 404
            assert facts_from_lookup(view, lookup_status=404, lookup_body=nothing["json"]) is None
            # the downstream holds a *different* request under that key (another document type)
            execute_openapi_http_tool(
                execution_config=submit_config(downstream.base_url),
                input_schema=None,
                input_data=submit_input(
                    case_id=CASE,
                    idempotency_key=view.logical_key,
                    expected_resource_version=1,
                    body={"document_type": "bank_statement"},
                ),
            )
            other = _lookup(downstream, view.logical_key)
            assert other["status_code"] == 200
            assert facts_from_lookup(view, lookup_status=200, lookup_body=other["json"]) is None
            claim = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_200", 200, 1)
            settled, _ = _settle(
                session,
                proposal_id,
                claim,
                facts_from_lookup(view, lookup_status=200, lookup_body=other["json"]),
            )
            assert settled.state == UNKNOWN and settled.reason == "receipt_unverified"


_STORED = {
    "request_id": "DR-0001",
    "case_id": CASE,
    "idempotency_key": "",  # filled in per test
    "document_type": DOC,
    "case_version": 2,
}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "change",
    [
        {"idempotency_key": "another-key"},
        {"request_id": ""},
        {"request_id": None},
        {"request_id": 5},
        {"case_id": "OTHER-CASE"},
        {"case_id": 5},
        {"document_type": "bank_statement"},
        {"document_type": 5},
        {"case_version": 3},
        {"case_version": 1},
        {"case_version": "2"},
        {"case_version": True},
        {"case_id": "bad id/with slash"},
    ],
)
def test_a_stored_request_that_is_not_this_proposals_effect_gives_no_facts(
    dialect: str, change: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            body = {**_STORED, "idempotency_key": view.logical_key}
            assert facts_from_lookup(view, lookup_status=200, lookup_body=body) is not None, (
                "the baseline matches"
            )
            assert (
                facts_from_lookup(view, lookup_status=200, lookup_body={**body, **change}) is None
            )


def test_the_connectors_own_intent_builder_makes_the_one_shape_the_verifier_can_confirm() -> None:
    from caliber.effects.connectors.document_request import build_intent

    intent = build_intent(
        case_id=CASE,
        document_type=DOC,
        expected_resource_version=1,
        effect_class="irreversible_write",
    )
    assert (intent.connector, intent.connector_version) == (CONNECTOR, VERSION)
    assert intent.resource == f"cases/{CASE}/document-requests"
    assert dict(intent.args) == {"document_type": DOC} and intent.expected_resource_version == "1"
    assert (
        build_intent(
            case_id=CASE, document_type=DOC, expected_resource_version=None, effect_class="read"
        ).expected_resource_version
        is None
    )
    for bad in ("", "..", "a/b", None):
        with pytest.raises(ValueError):
            build_intent(
                case_id=bad, document_type=DOC, expected_resource_version=1, effect_class="read"
            )  # type: ignore[arg-type]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_proposal_with_arguments_beyond_the_document_type_can_never_be_confirmed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented, not hidden: the downstream stores only the case and the document type, so such a proposal would commit
    downstream and then stay ``unknown``; ``build_intent`` is the one way to build a proposal this connector can verify."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = record_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="ask",
                occurrence_id="occ-extra",
                intent=Intent(
                    connector=CONNECTOR,
                    connector_version=VERSION,
                    effect_class="read",
                    resource=f"cases/{CASE}/document-requests",
                    args={"document_type": DOC, "note": "please hurry"},
                    expected_resource_version="1",
                ),
            )
            proposal_id = recorded.proposal_id
            decision = _decision(session, proposal_id, decision_id="pd-extra")
            _move(session, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision))
            _move(session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=ATTEMPT)
            view = proposal_view(session, proposal_id)
            stored = {**_STORED, "idempotency_key": view.logical_key}
            assert facts_from_lookup(view, lookup_status=200, lookup_body=stored) is None


@pytest.mark.parametrize("expected", ["+1", " 1", "1 ", "1_0", "\uff11", "-1", "1.0", "0x1", ""])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_expected_version_that_is_not_plain_digits_is_not_compared_loosely(
    dialect: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            try:
                proposal_id = _proposal(session, expected=expected)
            except ProposalError:
                return  # the empty string is refused by the intent itself
            view = proposal_view(session, proposal_id)
            stored = {**_STORED, "idempotency_key": view.logical_key}
            assert facts_from_lookup(view, lookup_status=200, lookup_body=stored) is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_lookup_verifier_refuses_what_it_cannot_read(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            versioned = _proposal(session)
            unversioned = _proposal(session, occurrence="occ-u", expected=None)
            odd = _proposal(session, occurrence="occ-odd", expected="one")
            body = {**_STORED, "idempotency_key": proposal_view(session, versioned).logical_key}
            for status in (404, 500, 503, 201, 302):
                assert (
                    facts_from_lookup(
                        proposal_view(session, versioned), lookup_status=status, lookup_body=body
                    )
                    is None
                )
            for not_a_request in (None, [], "text", 5, {}):
                assert (
                    facts_from_lookup(
                        proposal_view(session, versioned),
                        lookup_status=200,
                        lookup_body=not_a_request,
                    )
                    is None
                )
            # a proposal that expected no version accepts the version the downstream reports
            free = {
                **_STORED,
                "idempotency_key": proposal_view(session, unversioned).logical_key,
                "case_version": 9,
            }
            assert (
                facts_from_lookup(
                    proposal_view(session, unversioned), lookup_status=200, lookup_body=free
                )
                is not None
            )
            # an expected version that is not an integer cannot be checked against the stored one
            text_version = {**_STORED, "idempotency_key": proposal_view(session, odd).logical_key}
            assert (
                facts_from_lookup(
                    proposal_view(session, odd), lookup_status=200, lookup_body=text_version
                )
                is None
            )
            # a proposal that was never dispatched has no attempt to answer
            fresh = record_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="n",
                occurrence_id="o",
                intent=Intent(
                    connector=CONNECTOR,
                    connector_version=VERSION,
                    effect_class="read",
                    resource=f"cases/{CASE}/document-requests",
                    args={"document_type": DOC},
                    expected_resource_version="1",
                ),
            ).proposal_id
            fresh_view = proposal_view(session, fresh)
            stored = {**_STORED, "idempotency_key": fresh_view.logical_key}
            assert facts_from_lookup(fresh_view, lookup_status=200, lookup_body=stored) is None


# ---------------------------------------------------------------- the migration and the race


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_receipt_migration_rolls_back_to_0124_and_forward_again(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            _proposal(session)
            session.commit()
        assert "caliber_effect_receipts" in inspect(engine).get_table_names()
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_effect_proposals")}
        assert "receipt_id" in columns
        command.downgrade(Config(str(ALEMBIC_INI)), "0124")
        assert "caliber_effect_receipts" not in inspect(engine).get_table_names()
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_effect_proposals")}
        assert "receipt_id" not in columns and "version" in columns
        with factory() as session:  # 0124's rules are back: a commit needs no receipt again
            proposal_id = session.execute(
                text("SELECT proposal_id FROM caliber_effect_proposals")
            ).scalar_one()
            assert proposal_id
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        assert "caliber_effect_receipts" in inspect(engine).get_table_names()


def _need_postgresql() -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")


def _race(factory: sessionmaker[Session], proposal_id: str, ids: list[str]) -> list[str]:
    barrier = threading.Barrier(len(ids))
    outcomes: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def deliver(request_id: str) -> None:
        try:
            with factory() as session:
                facts = _facts(session, proposal_id, downstream_request_id=request_id)
                barrier.wait(10)
                try:
                    created = record_receipt(session, proposal_id, facts, now=AT).created
                    session.commit()
                    outcome = "created" if created else "duplicate"
                except ReceiptConflict:
                    session.rollback()
                    outcome = "conflict"
            with lock:
                outcomes.append(outcome)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=deliver, args=(i,), daemon=True) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors
    return outcomes


def test_concurrent_identical_deliveries_record_one_receipt_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            session.commit()
        outcomes = _race(factory, proposal_id, ["DR-same"] * 8)
        assert sorted(outcomes) == ["created"] + ["duplicate"] * 7
        assert _count(factory, CaliberEffectReceipt) == 1


def test_concurrent_different_claims_leave_exactly_one_authoritative_receipt_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            session.commit()
        outcomes = _race(factory, proposal_id, [f"DR-{n}" for n in range(6)])
        assert sorted(outcomes) == ["conflict"] * 5 + ["created"]
        with factory() as session:
            rows = list(session.execute(select(CaliberEffectReceipt)).scalars())
            assert len(rows) == 1 and read_receipt(session, rows[0].receipt_id)


# ---------------------------------------------------------------- the races and the odd corners


def _blind_first(session: Session, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``receipts.<name>`` find nothing the first time it is asked (as if the row landed just after the read)."""
    from caliber.effects import receipts

    real = getattr(receipts, name)
    state = {"blind": True}

    def blind(*args: Any, **kwargs: Any) -> Any:
        if state["blind"] and not kwargs.get("refresh"):
            state["blind"] = False
            return None
        return real(*args, **kwargs)

    monkeypatch.setattr(receipts, name, blind)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("same", [True, False])
def test_losing_the_insert_race_for_an_attempt_returns_the_winner_or_a_conflict(
    dialect: str, same: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            winner = record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            session.commit()
        with factory() as session:
            _blind_first(session, monkeypatch, "_for_attempt")
            facts = _facts(session, proposal_id, **({} if same else {"resource_version": "7"}))
            if same:
                again = record_receipt(session, proposal_id, facts, now=AT)
                assert (again.created, again.receipt_id) == (False, winner.receipt_id)
            else:
                with pytest.raises(ReceiptConflict):
                    record_receipt(session, proposal_id, facts, now=AT)
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_losing_the_insert_race_for_a_request_id_is_a_conflict(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _proposal(session)
            second = _proposal(session, occurrence="occ-2")
            record_receipt(
                session, first, _facts(session, first, downstream_request_id="DR-RACE"), now=AT
            )
            session.commit()
        with factory() as session:
            _blind_first(session, monkeypatch, "_request_owner")
            with pytest.raises(ReceiptConflict):
                record_receipt(
                    session,
                    second,
                    _facts(session, second, downstream_request_id="DR-RACE"),
                    now=AT,
                )
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_insert_that_fails_for_another_reason_is_an_error_without_the_drivers_text(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session)
            real_flush = session.flush

            def failing(*args: Any, **kwargs: Any) -> None:
                if not any(isinstance(pending, CaliberEffectReceipt) for pending in session.new):
                    return real_flush(*args, **kwargs)
                monkeypatch.setattr(session, "flush", real_flush)
                raise IntegrityError(
                    "INSERT", {"p": "SECRET"}, Exception("a constraint that is not a key")
                )

            monkeypatch.setattr(session, "flush", failing)
            with pytest.raises(ProposalError) as failed:
                record_receipt(session, proposal_id, _facts(session, proposal_id), now=AT)
            assert failed.value.code == "store_failed" and "SECRET" not in str(failed.value)
            assert failed.value.__suppress_context__


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_unknown_receipt_or_proposal_is_a_key_error_and_a_wrong_digest_is_refused_by_the_orm(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(KeyError):
                read_receipt(session, "rcpt-missing")
            with pytest.raises(KeyError):
                proposal_view(session, "prop-missing")
            proposal_id = _proposal(session)
            view = proposal_view(session, proposal_id)
            session.add(
                CaliberEffectReceipt(
                    receipt_id="rcpt-orm",
                    proposal_id=proposal_id,
                    attempt=1,
                    logical_key=view.logical_key,
                    intent_hash=view.intent_hash,
                    connector=CONNECTOR,
                    connector_version=VERSION,
                    resource=view.resource,
                    downstream_request_id="DR-ORM",
                    response_digest="sha256:" + "a" * 64,
                    verified_by="query",
                    verified_at=NAIVE_AT,
                    receipt_bytes=b"x",
                    receipt_digest="0" * 64,
                )
            )
            with pytest.raises(ValueError, match="SHA-256 of receipt_bytes"):
                session.flush()
            session.rollback()


def test_a_stored_case_id_the_intent_cannot_represent_gives_no_facts() -> None:
    from caliber.effects.records import ProposalView

    view = ProposalView(
        proposal_id="prop-x",
        logical_key="k" * 64,
        connector=CONNECTOR,
        connector_version=VERSION,
        effect_class="read",
        resource="r",
        expected_resource_version="1",
        intent_hash="sha256:" + "a" * 64,
        attempt=1,
        state=DISPATCHED,
    )
    body = {**_STORED, "idempotency_key": view.logical_key, "case_id": "c" * 600}
    assert facts_from_lookup(view, lookup_status=200, lookup_body=body) is None
