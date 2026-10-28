"""Effect proposals: one immutable intent per logical effect (B05.1, #700).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by
Alembic. The concurrency cases are PostgreSQL's: SQLite serialises writers, so a race cannot be arranged there.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from caliber.contracts import ContractValidationError, validate_record
from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.effects.records import (
    BAD_FIELD,
    CORRUPT,
    INTENT_ALGORITHM,
    INTENT_CONFLICT,
    MAX_ARGS_DEPTH,
    MAX_INTENT_BYTES,
    RUN_NOT_FOUND,
    STORE_FAILED,
    UNSUPPORTED_ALGORITHM,
    Intent,
    ProposalConflict,
    ProposalError,
    intent_digest,
    logical_key,
    read_proposal,
    record_proposal,
)
from caliber.run_manifest import FIXED_TENANT_ID
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.test_event_outbox import _count, _database


def _seed_run(factory: Any, run_id: str = "RUN-ep") -> str:
    with factory() as session:
        session.add_all(
            [
                CaliberProject(project_id="PRJ-ep", name="Ep", owner="@test"),
                CaliberWorkflow(workflow_id="wf-ep", project_id="PRJ-ep", name="Ep", owner="@test"),
                CaliberWorkflowRun(
                    workflow_run_id=run_id,
                    workflow_id="wf-ep",
                    project_id="PRJ-ep",
                    status="running",
                ),
            ]
        )
        session.commit()
    return run_id


def _nested(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = [value]
    return value


def _intent(**overrides: Any) -> Intent:
    fields: dict[str, Any] = {
        "connector": "document-request",
        "connector_version": "1.0.0",
        "effect_class": "irreversible_write",
        "resource": "cases/MORT-1/document-requests",
        "args": {"document_type": "pay_statement", "case_id": "MORT-1"},
        "expected_resource_version": "7",
    }
    fields.update(overrides)
    return Intent(**fields)


def _record(session: Session, occurrence: str = "occ-1", node: str = "ask", **intent: Any) -> Any:
    return record_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id=node,
        occurrence_id=occurrence,
        intent=_intent(**intent),
    )


# ---------------------------------------------------------------- the intent and its digest


def test_the_intent_digest_is_a_prefixed_sha256_of_canonical_bytes_that_name_their_algorithm() -> (
    None
):
    intent = _intent()
    canonical = json.loads(intent.canonical)
    assert canonical == {
        "algorithm": INTENT_ALGORITHM,
        "connector": {"name": "document-request", "version": "1.0.0"},
        "effect_class": "irreversible_write",
        "target": {"resource": "cases/MORT-1/document-requests"},
        "args": {"case_id": "MORT-1", "document_type": "pay_statement"},
        "expected_resource_version": "7",
    }
    assert intent.canonical == json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert intent.digest == "sha256:" + hashlib.sha256(intent.canonical).hexdigest()
    assert intent.digest == intent_digest(intent.canonical)
    assert intent.algorithm == INTENT_ALGORITHM


def test_the_digest_does_not_depend_on_argument_order_and_ignores_later_changes_to_the_caller_dict() -> (
    None
):
    args = {"b": [1, {"y": 2, "x": 1}], "a": "é"}
    first = _intent(args=args)
    second = _intent(args={"a": "é", "b": [1, {"x": 1, "y": 2}]})
    assert first.digest == second.digest
    args["a"] = "changed afterwards"
    assert first.digest == second.digest, "the canonical bytes were taken at construction"
    assert "changed afterwards" not in first.canonical.decode()


@pytest.mark.parametrize(
    "change",
    [
        {"connector": "other-connector"},
        {"connector_version": "1.0.1"},
        {"effect_class": "reversible_write"},
        {"effect_class": "read"},
        {"resource": "cases/MORT-2/document-requests"},
        {"args": {"document_type": "pay_statement", "case_id": "MORT-2"}},
        {"expected_resource_version": "8"},
        {"expected_resource_version": None},
    ],
)
def test_connector_version_or_effect_class_changes_digest(change: dict[str, Any]) -> None:
    """Any one intent field changes the digest: an approval of the old intent cannot cover the new one."""
    assert _intent(**change).digest != _intent().digest


def test_a_null_expected_version_and_the_string_null_are_different_intents() -> None:
    assert (
        _intent(expected_resource_version=None).digest
        != _intent(expected_resource_version="null").digest
    )


@pytest.mark.parametrize(
    "change",
    [
        {"connector": ""},
        {"connector": "   "},
        {"connector": "c" * 129},
        {"connector": "a\x00b"},
        {"connector": 5},
        {"connector_version": ""},
        {"connector_version": "v" * 65},
        {"connector_version": None},
        {"effect_class": "write"},
        {"effect_class": ""},
        {"resource": ""},
        {"resource": "r" * 513},
        {"resource": "a\nb"},
        {"expected_resource_version": ""},
        {"expected_resource_version": "v" * 257},
        {"expected_resource_version": 7},
        {"args": []},
        {"args": "x"},
        {"args": None},
        {"args": {1: "non-string key"}},
        {"args": {"n": float("nan")}},
        {"args": {"n": float("inf")}},
        {"args": {"o": object()}},
        {"args": {"s": "a\ud800b"}},
        {"args": {"t": (1, 2)}},
        {"args": {"outer": {1: "an integer key"}}},
        {"args": {"outer": {None: "a null key"}}},
        {"args": {"flag": enum.IntEnum("E", {"A": 1}).A}},
        {"args": {"s": type("S", (str,), {})("x")}},
        {"args": {"a": {1, 2}}},
        {"args": {"a": b"bytes"}},
        {"args": {"deep": _nested(MAX_ARGS_DEPTH + 1)}},
        lambda: {
            "args": {"deep": _nested(100000)}
        },  # built when run: Allure reprs every parameter value
        {"args": {"big": "x" * MAX_INTENT_BYTES}},
    ],
)
def test_a_malformed_intent_is_refused(change: Any) -> None:
    fields = change() if callable(change) else change
    with pytest.raises(ProposalError) as refused:
        _intent(**fields)
    assert refused.value.code == BAD_FIELD


def test_nesting_up_to_the_cap_is_accepted_and_a_read_only_mapping_is_a_json_object() -> None:
    assert _intent(args={"deep": _nested(MAX_ARGS_DEPTH - 1)}).digest.startswith("sha256:")
    proxied = _intent(args=MappingProxyType({"a": 1}))
    assert proxied == _intent(args={"a": 1}) and hash(proxied) == hash(_intent(args={"a": 1}))
    assert proxied != _intent(args={"a": 2}) and proxied != "not an intent"


def test_values_of_different_python_types_never_share_a_digest() -> None:
    """``intent-v1`` is exact about types: an int is not a float, a bool is not an int, and neither is a string."""
    values = [1, 1.0, True, "1", [1], {"a": 1}, None, "null", 0, 0.0, -0.0, False, "", [], {}]
    digests = {_intent(args={"v": value}).digest for value in values}
    assert len(digests) == len(values)


@pytest.mark.parametrize(
    "args",
    [
        {"n": 7, "f": 1.5, "z": -0.0, "e": 1e22, "t": 5e-324, "i": 2**63, "neg": -(2**200)},
        {
            "big": 10**30,
            "u": "naïve café ☕ \U0001f600",
            "ctl": "tab\there\nnewline\u0085 nul\x00 done",
        },
        {
            "nested": {"a": [1, [2, {"b": None}], True, False], "": "empty key"},
            "empty": {},
            "list": [],
        },
    ],
)
@pytest.mark.parametrize("dialect", DIALECTS)
def test_arguments_round_trip_to_the_same_bytes_on_every_dialect(
    dialect: str, args: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session, args=args)
            session.commit()
        with factory() as session:
            assert read_proposal(session, recorded.proposal_id)["args"] == args
            assert _record(session, args=args).created is False


def test_the_boundary_lengths_are_accepted() -> None:
    intent = _intent(
        connector="c" * 128,
        connector_version="v" * 64,
        resource="r" * 512,
        expected_resource_version="e" * 256,
        args={},
    )
    assert intent.digest.startswith("sha256:")


def test_the_logical_key_separates_its_parts_and_is_stable() -> None:
    key = logical_key(workflow_run_id="run", node_id="node", occurrence_id="occ")
    assert key == logical_key(workflow_run_id="run", node_id="node", occurrence_id="occ")
    assert len(key) == 64 and key == key.lower()
    assert logical_key(workflow_run_id="a:b", node_id="c", occurrence_id="d") != logical_key(
        workflow_run_id="a", node_id="b:c", occurrence_id="d"
    )
    assert (
        len(
            {
                logical_key(workflow_run_id="run", node_id="node", occurrence_id="occ"),
                logical_key(workflow_run_id="run2", node_id="node", occurrence_id="occ"),
                logical_key(workflow_run_id="run", node_id="node2", occurrence_id="occ"),
                logical_key(workflow_run_id="run", node_id="node", occurrence_id="occ2"),
            }
        )
        == 4
    )


def test_an_intent_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _intent().connector = "x"  # type: ignore[misc]


# ---------------------------------------------------------------- recording


@pytest.mark.parametrize("dialect", DIALECTS)
def test_duplicate_occurrence_idempotent(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _record(session)
            session.commit()
        with factory() as session:
            again = _record(session)
            session.commit()
        assert first.created is True and again.created is False
        assert (again.proposal_id, again.logical_key, again.intent_hash, again.state) == (
            first.proposal_id,
            first.logical_key,
            first.intent_hash,
            "proposed",
        )
        assert first.logical_key == logical_key(
            workflow_run_id="RUN-ep", node_id="ask", occurrence_id="occ-1"
        )
        assert first.intent_hash == _intent().digest
        assert _count(factory, CaliberEffectProposal) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_changed_intent_409(dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _record(session)
            session.commit()
        with factory() as session:
            with pytest.raises(ProposalConflict) as conflict:
                _record(session, args={"document_type": "pay_statement", "case_id": "OTHER"})
            session.rollback()
        refused = conflict.value
        assert (refused.code, refused.logical_key) == (INTENT_CONFLICT, first.logical_key)
        assert refused.stored_intent_hash == first.intent_hash
        assert refused.offered_intent_hash != first.intent_hash
        assert "OTHER" not in str(refused) and "pay_statement" not in str(refused), (
            "no arguments in the error"
        )
        with factory() as session:
            stored = session.execute(select(CaliberEffectProposal)).scalar_one()
            assert stored.args["case_id"] == "MORT-1" and stored.intent_hash == first.intent_hash


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "change",
    [
        {"connector_version": "2.0.0"},
        {"effect_class": "export"},
        {"expected_resource_version": None},
    ],
)
def test_a_change_to_a_field_a_reduced_hash_would_miss_is_still_a_conflict(
    dialect: str, change: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            _record(session)
            session.commit()
        with factory() as session, pytest.raises(ProposalConflict):
            _record(session, **change)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_repeated_loop_distinct(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next occurrence of an identical call is its own logical effect with the same digest."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            outcomes = [_record(session, f"occ-{n}") for n in range(3)]
            other_node = _record(session, "occ-0", node="other")
            session.commit()
        assert [o.created for o in outcomes] == [True, True, True] and other_node.created
        assert len({o.proposal_id for o in outcomes}) == 3
        assert len({o.logical_key for o in outcomes}) == 3
        assert len({o.intent_hash for o in [*outcomes, other_node]}) == 1, (
            "identical intent, identical digest"
        )
        assert _count(factory, CaliberEffectProposal) == 4


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_unknown_run_or_a_malformed_key_part_is_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ProposalError) as unknown:
                record_proposal(
                    session,
                    workflow_run_id="RUN-nope",
                    node_id="ask",
                    occurrence_id="o",
                    intent=_intent(),
                )
            assert unknown.value.code == RUN_NOT_FOUND
            for kwargs in (
                {"node_id": "", "occurrence_id": "o"},
                {"node_id": "ask", "occurrence_id": "o" * 129},
                {"node_id": "n" * 129, "occurrence_id": "o"},
                {"node_id": "ask", "occurrence_id": "a\x00b"},
                {"node_id": "ask", "occurrence_id": 3},
            ):
                with pytest.raises(ProposalError) as bad:
                    record_proposal(
                        session,
                        workflow_run_id="RUN-ep",
                        intent=_intent(),
                        **kwargs,  # type: ignore[arg-type]
                    )
                assert bad.value.code == BAD_FIELD
            with pytest.raises(ProposalError) as not_intent:
                record_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="ask",
                    occurrence_id="o",
                    intent={"connector": "x"},  # type: ignore[arg-type]
                )
            assert not_intent.value.code == BAD_FIELD
        assert _count(factory, CaliberEffectProposal) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_caller_owns_the_transaction(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            assert _record(session).created
            session.rollback()
        assert _count(factory, CaliberEffectProposal) == 0
        with factory() as session:
            assert _record(session).created, "nothing was recorded, so the retry creates it"
            session.commit()
        assert _count(factory, CaliberEffectProposal) == 1


def _blind_first_lookup(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the session's first lookup by logical key find nothing, as if the row were committed just after it."""
    real_execute = session.execute
    state = {"blind": True}

    def blind(*args: Any, **kwargs: Any) -> Any:
        statement = str(args[0])
        if (
            state["blind"]
            and "caliber_effect_proposals" in statement
            and "logical_key" in statement
        ):
            state["blind"] = False
            return real_execute(select(CaliberEffectProposal).where(text("1 = 0")))
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", blind)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_losing_the_insert_race_returns_the_winner_or_a_conflict(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another transaction commits the same logical key between this one's read and its insert."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as other:
            winner = _record(other)
            other.commit()
        for different in (False, True):
            with factory() as session:
                _blind_first_lookup(session, monkeypatch)
                if different:
                    with pytest.raises(ProposalConflict):
                        _record(session, expected_resource_version="99")
                else:
                    result = _record(session)
                    assert (result.created, result.proposal_id) == (False, winner.proposal_id)
                session.rollback()
        assert _count(factory, CaliberEffectProposal) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_insert_that_fails_for_another_reason_than_the_key_is_an_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            real_flush = session.flush

            def failing(*args: Any, **kwargs: Any) -> None:
                if not any(isinstance(pending, CaliberEffectProposal) for pending in session.new):
                    return real_flush(*args, **kwargs)
                monkeypatch.setattr(session, "flush", real_flush)
                raise IntegrityError(
                    "INSERT", {"args": "SECRET-ARGS"}, Exception("a constraint that is not the key")
                )

            monkeypatch.setattr(session, "flush", failing)
            with pytest.raises(ProposalError) as failed:
                _record(session)
            assert failed.value.code == STORE_FAILED
            assert "SECRET-ARGS" not in str(failed.value) and failed.value.__cause__ is None
            assert failed.value.__suppress_context__, "the driver error is not chained"
        assert _count(factory, CaliberEffectProposal) == 0


# ---------------------------------------------------------------- reading


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_proposal_reads_back_as_a_valid_v1_record_with_the_recomputable_intent(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        with factory() as session:
            record = read_proposal(session, recorded.proposal_id)
            row = session.get(CaliberEffectProposal, recorded.proposal_id)
        validate_record("proposal", record)
        assert record == {
            "schema_version": "v1",
            "proposal_id": recorded.proposal_id,
            "workflow_run_id": "RUN-ep",
            "node_id": "ask",
            "logical_key": recorded.logical_key,
            "intent_hash": _intent().digest,
            "effect_class": "irreversible_write",
            "target": {
                "connector": "document-request",
                "resource": "cases/MORT-1/document-requests",
            },
            "args": {"document_type": "pay_statement", "case_id": "MORT-1"},
            "expected_resource_version": "7",
            "state": "proposed",
            "tenant_id": FIXED_TENANT_ID,
            "region": "local",
            "occurrence_id": "occ-1",
            "connector_version": "1.0.0",
            "intent_algorithm": INTENT_ALGORITHM,
        }
        assert row is not None and bytes(row.intent_bytes) == _intent().canonical
        with factory() as session, pytest.raises(KeyError):
            read_proposal(session, "prop-missing")


def test_a_record_written_before_the_new_optional_fields_is_still_valid() -> None:
    from tests.test_contracts_registry import valid_record

    validate_record("proposal", valid_record("proposal"))
    with pytest.raises(ContractValidationError):
        validate_record("proposal", {**valid_record("proposal"), "occurrence_id": ""})
    with pytest.raises(ContractValidationError):
        validate_record("proposal", {**valid_record("proposal"), "surprise": 1})


def _tamper(engine: Any, dialect: str, statement: str, **params: Any) -> None:
    """Edit a row around the immutability trigger, as a database administrator or a restore could."""
    with engine.begin() as connection:
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_proposals DISABLE TRIGGER USER"))
        else:
            connection.execute(text("DROP TRIGGER trg_caliber_effect_proposals_frozen"))
        connection.execute(text(statement), params)
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_proposals ENABLE TRIGGER USER"))


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "assignment",
    [
        "connector_version = '9.9.9'",
        "effect_class = 'read'",
        "resource = 'cases/OTHER/document-requests'",
        "expected_resource_version = '8'",
        "node_id = 'moved'",
        "occurrence_id = 'moved'",
        "tenant_id = 'elsewhere'",
    ],
)
def test_a_row_edited_around_the_triggers_is_detected_when_it_is_read(
    dialect: str, assignment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        _tamper(engine, dialect, f"UPDATE caliber_effect_proposals SET {assignment}")
        with factory() as session, pytest.raises(ProposalError) as corrupt:
            read_proposal(session, recorded.proposal_id)
        assert corrupt.value.code == CORRUPT


@pytest.mark.parametrize("dialect", DIALECTS)
def test_edited_arguments_and_a_dropped_digest_are_detected(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        _tamper(
            engine,
            dialect,
            "UPDATE caliber_effect_proposals SET args = :args",
            args='{"case_id": "OTHER"}',
        )
        with factory() as session, pytest.raises(ProposalError) as corrupt:
            read_proposal(session, recorded.proposal_id)
        assert corrupt.value.code == CORRUPT
        assert "OTHER" not in str(corrupt.value)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_intent_from_an_algorithm_this_build_does_not_know_is_refused_not_read_as_the_nearest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        _tamper(
            engine, dialect, "UPDATE caliber_effect_proposals SET intent_algorithm = 'intent-v2'"
        )
        with factory() as session, pytest.raises(ProposalError) as refused:
            read_proposal(session, recorded.proposal_id)
        assert refused.value.code == UNSUPPORTED_ALGORITHM
        assert "intent-v2" in str(refused.value)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_stored_intent_that_no_longer_validates_is_corrupt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        _tamper(engine, dialect, "UPDATE caliber_effect_proposals SET args = '[]'")
        with factory() as session, pytest.raises(ProposalError) as corrupt:
            read_proposal(session, recorded.proposal_id)
        assert corrupt.value.code == CORRUPT


# ---------------------------------------------------------------- immutability


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "assignment",
    [
        "connector = 'x'",
        "connector_version = '2'",
        "effect_class = 'read'",
        "resource = 'x'",
        "args = '{}'",
        "expected_resource_version = '2'",
        "intent_algorithm = 'x'",
        "intent_bytes = x'00'",
        "intent_hash = 'sha256:00'",
        "logical_key = 'x'",
        "node_id = 'x'",
        "occurrence_id = 'x'",
        "workflow_run_id = 'RUN-ep'",
    ],
)
def test_the_intent_columns_are_frozen_by_the_database(
    dialect: str, assignment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if dialect == "postgresql" and "x'00'" in assignment:
        assignment = "intent_bytes = '\\x00'::bytea"
    if dialect == "postgresql" and assignment == "args = '{}'":
        assignment = "args = '{}'::json"
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        after_noop = assignment == "workflow_run_id = 'RUN-ep'"
        with engine.begin() as connection:
            if after_noop:
                # Setting a column to the value it already has is not a change.
                connection.execute(text(f"UPDATE caliber_effect_proposals SET {assignment}"))
                return
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(f"UPDATE caliber_effect_proposals SET {assignment} WHERE proposal_id = :p"),
                {"p": recorded.proposal_id},
            )
        assert _count(factory, CaliberEffectProposal) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_intent_column_is_frozen_by_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Derived from the model: every column that is not state or the evidence that moves with it."""
    new_values = {
        "proposal_id": "'prop-other'",
        "tenant_id": "'elsewhere'",
        "created_at": "'2001-01-01 00:00:00'",
        "intent_bytes": "'\\x01'::bytea" if dialect == "postgresql" else "x'01'",
        "args": "'[]'::json" if dialect == "postgresql" else "'[]'",
    }
    mutable = {
        "state",
        "version",
        "attempt",
        "owner_id",
        "dispatch_token",
        "dispatched_at",
        "unknown_at",
        "settled_at",
        "reason",
        "approval_kind",
        "approval_ref",
        "receipt_id",
    }
    columns = [c.name for c in CaliberEffectProposal.__table__.columns if c.name not in mutable]
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        for column in columns:
            value = new_values.get(column, "'changed'")
            with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE caliber_effect_proposals SET {column} = {value} WHERE proposal_id = :p"
                    ),
                    {"p": recorded.proposal_id},
                )
        assert read_proposal(factory(), recorded.proposal_id)["state"] == "proposed"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_state_moves_only_with_the_next_version_and_only_to_a_known_state(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full transition rules are in ``test_effect_records.py``; here, that the freeze leaves room for them."""
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        with pytest.raises(DBAPIError, match="version"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET state = 'approved', approval_kind = 'policy_decision', "
                    "approval_ref = 'pd-1' WHERE proposal_id = :p"
                ),
                {"p": recorded.proposal_id},
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET state = 'approved', version = 2, "
                    "approval_kind = 'policy_decision', approval_ref = 'pd-1' WHERE proposal_id = :p"
                ),
                {"p": recorded.proposal_id},
            )
        with factory() as session:
            assert read_proposal(session, recorded.proposal_id)["state"] == "approved"
        with pytest.raises((IntegrityError, DBAPIError)), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET state = 'done', version = 3 WHERE proposal_id = :p"
                ),
                {"p": recorded.proposal_id},
            )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_proposal_cannot_be_deleted_and_the_orm_refuses_an_intent_edit(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            recorded = _record(session)
            session.commit()
        with pytest.raises(DBAPIError, match="DELETE refused"), engine.begin() as connection:
            connection.execute(text("DELETE FROM caliber_effect_proposals"))
        with factory() as session:
            row = session.get(CaliberEffectProposal, recorded.proposal_id)
            assert row is not None
            row.connector = "other"
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            row = session.get(CaliberEffectProposal, recorded.proposal_id)
            assert row is not None
            session.delete(row)
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
        assert _count(factory, CaliberEffectProposal) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_row_whose_hash_is_not_the_digest_of_its_bytes_is_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        intent = _intent()
        with factory() as session:
            session.add(
                CaliberEffectProposal(
                    proposal_id="prop-orm",
                    tenant_id=FIXED_TENANT_ID,
                    workflow_run_id="RUN-ep",
                    node_id="n",
                    occurrence_id="o",
                    logical_key="0" * 64,
                    connector=intent.connector,
                    connector_version=intent.connector_version,
                    effect_class=intent.effect_class,
                    resource=intent.resource,
                    args={},
                    expected_resource_version=None,
                    intent_algorithm=INTENT_ALGORITHM,
                    intent_bytes=intent.canonical,
                    intent_hash="sha256:" + "0" * 64,
                )
            )
            with pytest.raises(ValueError, match="SHA-256 of intent_bytes"):
                session.flush()
            session.rollback()
        if dialect == "postgresql":
            insert = text(
                "INSERT INTO caliber_effect_proposals (proposal_id, tenant_id, workflow_run_id, node_id, occurrence_id, "
                "logical_key, connector, connector_version, effect_class, resource, args, intent_algorithm, "
                "intent_bytes, intent_hash) VALUES ('prop-raw', 'local', 'RUN-ep', 'n', 'o', :k, 'c', 'v', 'read', 'r', "
                "'{}', 'intent-v1', :b, :h)"
            )
            with (
                pytest.raises(IntegrityError, match="ck_effect_proposal_hash_is_sha256"),
                engine.begin() as connection,
            ):
                connection.execute(
                    insert, {"k": "0" * 64, "b": intent.canonical, "h": "sha256:" + "0" * 64}
                )
        assert _count(factory, CaliberEffectProposal) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_table_refuses_a_second_row_for_one_occurrence_and_malformed_rows(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        digest = intent_digest(b"x")
        base = {
            "t": FIXED_TENANT_ID,
            "run": "RUN-ep",
            "n": "ask",
            "o": "occ-1",
            "k": "a" * 64,
            "b": b"x",
            "h": digest,
            "c": "read",
            "s": "proposed",
        }
        insert = text(
            "INSERT INTO caliber_effect_proposals (proposal_id, tenant_id, workflow_run_id, node_id, occurrence_id, "
            "logical_key, connector, connector_version, effect_class, resource, args, intent_algorithm, intent_bytes, "
            "intent_hash, state) VALUES (:id, :t, :run, :n, :o, :k, 'c', 'v', :c, 'r', :args, 'intent-v1', :b, :h, :s)"
        )

        def put(**changes: Any) -> None:
            params = {**base, "id": changes.pop("id"), "args": "{}", **changes}
            with engine.begin() as connection:
                connection.execute(insert, params)

        put(id="p1")
        for changes in (
            {"id": "p2", "k": "b" * 64},  # same (tenant, run, node, occurrence)
            {"id": "p3", "o": "occ-2"},  # same logical key
            {"id": "p4", "o": "occ-4", "k": "c" * 64, "c": "write"},
            {"id": "p5", "o": "occ-5", "k": "d" * 64, "s": "done"},
            {"id": "p6", "o": "occ-6", "k": "short"},
            {"id": "p7", "o": "occ-7", "k": "e" * 64, "h": "sha256:short"},
            {"id": "p8", "o": "occ-8", "k": "f" * 64, "b": b""},
            {"id": "p9", "o": "", "k": "1" * 64},
            # SQLite does not enforce foreign keys unless asked to, so the missing run is PostgreSQL's case.
            *(
                [{"id": "p10", "o": "occ-10", "k": "2" * 64, "run": "RUN-missing"}]
                if dialect == "postgresql"
                else []
            ),
        ):
            with pytest.raises(IntegrityError):
                put(**changes)
        assert _count(factory, CaliberEffectProposal) == 1


# ---------------------------------------------------------------- the rest of the system


def test_deleting_a_workflow_whose_runs_have_proposals_is_a_409(
    client: Any, db_session: Session
) -> None:
    from tests.workflow_helpers import PREFIX, create_workflow

    kept = create_workflow(client, "Has Proposal")
    db_session.add(CaliberWorkflowRun(workflow_run_id="RUN-ep", workflow_id=kept))
    db_session.commit()
    record_proposal(
        db_session,
        workflow_run_id="RUN-ep",
        node_id="ask",
        occurrence_id="occ-1",
        intent=_intent(),
    )
    db_session.commit()

    refused = client.delete(f"{PREFIX}/workflows/{kept}")
    assert refused.status_code == 409, refused.text
    assert "immutable effect proposals" in refused.text
    assert client.get(f"{PREFIX}/workflows/{kept}").status_code == 200
    assert (
        db_session.execute(select(func.count()).select_from(CaliberEffectProposal)).scalar_one()
        == 1
    )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_retention_pruning_keeps_a_run_that_has_a_proposal(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from caliber.workflows.promoter import prune_workflow_runs

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            session.add(
                CaliberWorkflowRun(
                    workflow_run_id="RUN-plain", workflow_id="wf-ep", project_id="PRJ-ep"
                )
            )
            _record(session)
            session.commit()
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        with factory() as session, session.begin():
            assert prune_workflow_runs(session, retention_days=1, now=far_future) == 1
        with factory() as session:
            assert session.get(CaliberWorkflowRun, "RUN-ep") is not None, "a proposal's run is kept"
            assert session.get(CaliberWorkflowRun, "RUN-plain") is None


# ---------------------------------------------------------------- concurrency (PostgreSQL)


def _need_postgresql() -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")


def test_concurrent_insert_postgres_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight workers record the same logical effect at once: one proposal, seven reports of it."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        workers = 8
        barrier = threading.Barrier(workers)
        outcomes: list[tuple[bool, str]] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def record() -> None:
            try:
                with factory() as session:
                    barrier.wait(10)
                    result = _record(session)
                    session.commit()
                with lock:
                    outcomes.append((result.created, result.proposal_id))
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=record, daemon=True) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert sorted(created for created, _ in outcomes) == [False] * (workers - 1) + [True]
        assert len({proposal_id for _, proposal_id in outcomes}) == 1
        assert _count(factory, CaliberEffectProposal) == 1


def test_concurrent_different_intents_leave_exactly_one_winner_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        workers = 6
        barrier = threading.Barrier(workers)
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def record(index: int) -> None:
            try:
                with factory() as session:
                    barrier.wait(10)
                    try:
                        _record(session, args={"n": index})
                        session.commit()
                        outcome = "recorded"
                    except ProposalConflict:
                        session.rollback()
                        outcome = "conflict"
                with lock:
                    results.append(outcome)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=record, args=(i,), daemon=True) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert sorted(results) == ["conflict"] * (workers - 1) + ["recorded"]
        assert _count(factory, CaliberEffectProposal) == 1


def test_truncate_is_refused_postgresql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            _record(session)
            session.commit()
        with (
            pytest.raises(DBAPIError, match="DELETE refused|immutable"),
            engine.begin() as connection,
        ):
            # every table that references the proposals, in one statement: no foreign-key refusal, so it is the
            # table's own trigger that refuses
            connection.execute(
                text(
                    "TRUNCATE caliber_effect_proposals, caliber_effect_receipts, "
                    "caliber_runtime_approval_requests"
                )
            )
        assert _count(factory, CaliberEffectProposal) == 1
