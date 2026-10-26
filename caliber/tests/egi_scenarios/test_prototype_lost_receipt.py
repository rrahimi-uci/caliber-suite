"""DISPOSABLE spike (#625): a lost response and a second dispatch with the same key mutate the downstream once.

Deleted by #520 task 1 together with ``prototype/``. The downstream is a real process
(``downstream`` fixture) that counts its own mutations, so the claim is measured where it matters.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from caliber.contracts import ContractValidationError, validate_record
from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.egi_scenarios.conftest import Downstream
from tests.egi_scenarios.prototype import dispatch_spike as spike

CASE = "MORT-2026-000184"
DOC = "pay_statement"
SRC = Path(__file__).resolve().parents[2] / "src" / "caliber"


@pytest.fixture(params=DIALECTS)
def session(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Session]:
    with dialect_database_url(request.param, tmp_path, "egi_spike") as url:
        engine: Engine = create_engine(url)
        spike.metadata.create_all(engine)
        with Session(engine) as db:
            yield db
        engine.dispose()


def make_proposal(
    session: Session, *, run: str = "run-1", node: str = "request", version: str | None = "1"
) -> dict:
    return spike.propose(
        session,
        workflow_run_id=run,
        node_id=node,
        case_id=CASE,
        document_type=DOC,
        expected_version=version,
    )


# ---------------------------------------------------------------- the key


def test_logical_key_is_stable() -> None:
    first = spike.logical_key("run-1", "n", {"case_id": CASE, "document_type": DOC})
    reordered = spike.logical_key("run-1", "n", {"document_type": DOC, "case_id": CASE})
    assert first == reordered
    assert len(first) == 64
    assert spike.canonical_args({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'


@pytest.mark.parametrize(
    ("run", "node", "args"),
    [
        ("run-2", "n", {"case_id": CASE, "document_type": DOC}),
        ("run-1", "other", {"case_id": CASE, "document_type": DOC}),
        ("run-1", "n", {"case_id": CASE, "document_type": "bank_statement"}),
    ],
)
def test_logical_key_changes_with_run_node_or_args(run: str, node: str, args: dict) -> None:
    base = spike.logical_key("run-1", "n", {"case_id": CASE, "document_type": DOC})
    assert spike.logical_key(run, node, args) != base


def test_src_never_imports_egi_scenarios() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "egi_scenarios" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], f"production code must not depend on the scenario spike: {offenders}"


# ---------------------------------------------------------------- the proposal


def test_propose_persists_a_valid_contract_record(session: Session) -> None:
    record = make_proposal(session)
    assert validate_record("proposal", record) is None
    assert spike.state_of(session, record) == "proposed"
    assert record["logical_key"] == spike.logical_key("run-1", "request", record["args"])
    assert record["effect_class"] == "irreversible_write"


def test_the_same_effect_cannot_be_proposed_twice(session: Session) -> None:
    make_proposal(session)
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        make_proposal(session)
    session.rollback()


def test_a_record_that_breaks_the_contract_is_not_stored(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(kind: str, payload: dict) -> None:
        raise ContractValidationError(kind, ["injected"])

    monkeypatch.setattr(spike, "validate_record", broken)
    with pytest.raises(ContractValidationError):
        make_proposal(session)
    rows = session.execute(spike.spike_proposals.select()).all()
    assert rows == []


# ---------------------------------------------------------------- the lost response


def test_normal_dispatch_mutates_once(session: Session, downstream: Downstream) -> None:
    proposal = make_proposal(session)
    result = spike.dispatch(session, proposal, downstream.base_url)
    assert result["status_code"] == 201
    assert downstream.mutations() == 1
    assert spike.state_of(session, proposal) == "committed"
    assert downstream.query(proposal["logical_key"]) is not None


def test_dropped_response_then_redispatch_mutates_once(
    session: Session, downstream: Downstream
) -> None:
    proposal = make_proposal(session)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.RemoteProtocolError):
        spike.dispatch(session, proposal, downstream.base_url)
    assert spike.state_of(session, proposal) == "dispatched", "the outcome is unknown, not failed"
    assert downstream.mutations() == 1, "the downstream committed before the response was lost"

    replay = spike.dispatch(session, proposal, downstream.base_url)
    assert replay["status_code"] == 200, "the same key replays the stored answer"
    assert downstream.mutations() == 1
    assert spike.state_of(session, proposal) == "committed"


def test_a_stale_expected_version_is_refused_and_recorded_failed(
    session: Session, downstream: Downstream
) -> None:
    proposal = make_proposal(session, version="99")
    result = spike.dispatch(session, proposal, downstream.base_url)
    assert result["status_code"] == 412
    assert downstream.mutations() == 0
    assert spike.state_of(session, proposal) == "failed"


def test_a_different_key_for_the_same_request_would_duplicate(
    session: Session, downstream: Downstream
) -> None:
    """The control: a retry that does not carry the same key is a second mutation. The proposals share their
    arguments and differ in the node, so their keys differ; the second one expects the version the first made."""
    first = make_proposal(session, node="a", version="1")
    second = make_proposal(session, node="b", version="2")
    spike.dispatch(session, first, downstream.base_url)
    spike.dispatch(session, second, downstream.base_url)
    assert downstream.mutations() == 2


def test_the_expected_version_alone_also_stops_a_blind_second_dispatch(
    session: Session, downstream: Downstream
) -> None:
    """Finding: a different key with the *same* expected version is refused (412) once the first committed.
    The version precondition is a second guard, but it does not answer a lost response: it refuses the retry
    instead of replaying the stored result, which only the stable key does."""
    first = make_proposal(session, node="a", version="1")
    second = make_proposal(session, node="b", version="1")
    assert spike.dispatch(session, first, downstream.base_url)["status_code"] == 201
    assert spike.dispatch(session, second, downstream.base_url)["status_code"] == 412
    assert downstream.mutations() == 1


def test_dispatch_round_trip_is_fast_enough_to_be_a_unit_of_the_scenarios(
    session: Session, downstream: Downstream
) -> None:
    proposal = make_proposal(session)
    started = time.perf_counter()
    spike.dispatch(session, proposal, downstream.base_url)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"a dispatch took {elapsed:.3f}s"
