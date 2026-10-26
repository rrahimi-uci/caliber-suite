"""DISPOSABLE spike (#626): ``unknown`` blocks, and the downstream settles it by key, even after a process kill.

Deleted by #522 together with ``prototype/``; the state names, the transition table and the kill scenario are
copied into ``caliber/src/caliber/effects/records.py`` and ``test_v07_restart_exactly_once.py`` there.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.egi_scenarios.conftest import Downstream
from tests.egi_scenarios.prototype import dispatch_spike as spike

CASE = "MORT-2026-000184"
CALIBER_DIR = Path(__file__).resolve().parents[2]
STARTED_BY_SECONDS = 10.0
QUERY_BUDGET_SECONDS = 5.0


@pytest.fixture(params=DIALECTS)
def session(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Session]:
    with dialect_database_url(request.param, tmp_path, "egi_unknown") as url:
        engine = create_engine(url)
        spike.metadata.create_all(engine)
        with Session(engine) as db:
            yield db
        engine.dispose()


def make_proposal(
    session: Session, *, run: str = "run-1", version: str | None = "1"
) -> dict[str, Any]:
    return spike.propose(
        session,
        workflow_run_id=run,
        node_id="request",
        case_id=CASE,
        document_type="pay_statement",
        expected_version=version,
    )


def request_id_of(session: Session, proposal: dict[str, Any]) -> str | None:
    return session.execute(
        select(spike.spike_proposals.c.downstream_request_id).where(
            spike.spike_proposals.c.proposal_id == proposal["proposal_id"]
        )
    ).scalar_one()


# ---------------------------------------------------------------- the states


def test_the_state_machine_is_the_documented_one() -> None:
    assert spike.STATES == ("proposed", "dispatched", "unknown", "committed", "failed")
    assert {
        "proposed": {"dispatched"},
        "dispatched": {"committed", "failed", "unknown"},
        "unknown": {"committed", "failed"},
    } == spike.TRANSITIONS
    reachable = {to for targets in spike.TRANSITIONS.values() for to in targets} | {"proposed"}
    assert reachable == set(spike.STATES)


@pytest.mark.parametrize(
    ("start", "to"),
    [
        ("proposed", "committed"),
        ("proposed", "unknown"),
        ("dispatched", "proposed"),
        ("unknown", "dispatched"),  # a blind second dispatch is not a transition
        ("committed", "failed"),
        ("failed", "committed"),
        ("committed", "dispatched"),
    ],
)
def test_illegal_transition_raises(session: Session, start: str, to: str) -> None:
    proposal = make_proposal(session)
    path = {
        "proposed": [],
        "dispatched": ["dispatched"],
        "unknown": ["dispatched", "unknown"],
        "committed": ["dispatched", "committed"],
        "failed": ["dispatched", "failed"],
    }[start]
    for step in path:
        spike.transition(session, proposal["proposal_id"], step)
    with pytest.raises(spike.IllegalTransition, match=f"{start} -> {to}"):
        spike.transition(session, proposal["proposal_id"], to)
    assert spike.state_of(session, proposal) == start


def test_every_documented_transition_is_allowed(session: Session) -> None:
    for start, targets in spike.TRANSITIONS.items():
        for to in targets:
            proposal = make_proposal(session, run=f"run-{start}-{to}")
            for step in {
                "proposed": [],
                "dispatched": ["dispatched"],
                "unknown": ["dispatched", "unknown"],
            }[start]:
                spike.transition(session, proposal["proposal_id"], step)
            spike.transition(session, proposal["proposal_id"], to)
            assert spike.state_of(session, proposal) == to


# ---------------------------------------------------------------- unknown blocks, reconcile settles


def test_dependent_action_blocked_while_unknown(session: Session, downstream: Downstream) -> None:
    proposal = make_proposal(session)
    assert spike.dependent_action(session, proposal["proposal_id"]) == "proposed"
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.RemoteProtocolError):
        spike.dispatch(session, proposal, downstream.base_url)
    assert spike.state_of(session, proposal) == "unknown"
    with pytest.raises(spike.UnknownEffectError, match="reconcile"):
        spike.dependent_action(session, proposal["proposal_id"])
    spike.reconcile(session, proposal["proposal_id"], downstream.base_url)
    assert spike.dependent_action(session, proposal["proposal_id"]) == "committed"


def test_a_blind_second_dispatch_of_an_unknown_proposal_is_refused(
    session: Session, downstream: Downstream
) -> None:
    proposal = make_proposal(session)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.RemoteProtocolError):
        spike.dispatch(session, proposal, downstream.base_url)
    with pytest.raises(spike.IllegalTransition, match="unknown -> dispatched"):
        spike.dispatch(session, proposal, downstream.base_url)
    assert downstream.mutations() == 1


def test_reconcile_after_dropped_response(session: Session, downstream: Downstream) -> None:
    proposal = make_proposal(session)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.RemoteProtocolError):
        spike.dispatch(session, proposal, downstream.base_url)
    started = time.perf_counter()
    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "committed"
    assert time.perf_counter() - started < QUERY_BUDGET_SECONDS
    assert downstream.mutations() == 1
    stored = downstream.query(proposal["logical_key"])
    assert stored is not None
    assert request_id_of(session, proposal) == stored["request_id"]


def test_a_5xx_is_unknown_not_failed_and_reconcile_finds_nothing_when_nothing_committed(
    session: Session, downstream: Downstream
) -> None:
    """A server error does not say whether the request committed, so it is ``unknown``; the downstream's
    answer to the query by key then settles it (here: nothing committed)."""
    proposal = make_proposal(session)
    downstream.set_fault("fail_before_commit")
    result = spike.dispatch(session, proposal, downstream.base_url)
    assert result["status_code"] == 500
    assert spike.state_of(session, proposal) == "unknown"
    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "failed"
    assert request_id_of(session, proposal) is None
    assert downstream.mutations() == 0


def test_a_4xx_refusal_is_a_definite_failure(session: Session, downstream: Downstream) -> None:
    proposal = make_proposal(session, version="99")
    assert spike.dispatch(session, proposal, downstream.base_url)["status_code"] == 412
    assert spike.state_of(session, proposal) == "failed"


def test_a_gateway_error_after_the_commit_is_unknown_and_reconciles_to_committed(
    session: Session, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy in front of a downstream that did commit answers 502 with an HTML body."""
    real_send = spike.send

    def committed_then_bad_gateway(proposal: dict[str, Any], base_url: str) -> httpx.Response:
        real_send(proposal, base_url)
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    monkeypatch.setattr(spike, "send", committed_then_bad_gateway)
    proposal = make_proposal(session)
    result = spike.dispatch(session, proposal, downstream.base_url)
    assert result["status_code"] == 502 and result["body"] == {}
    assert spike.state_of(session, proposal) == "unknown"
    assert downstream.mutations() == 1
    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "committed"


def test_a_timeout_after_the_commit_is_unknown_and_reconciles_to_committed(
    session: Session, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spike, "TIMEOUT_SECONDS", 0.3)
    proposal = make_proposal(session)
    downstream.set_fault("delay_response_seconds", seconds=1)
    with pytest.raises(httpx.ReadTimeout):
        spike.dispatch(session, proposal, downstream.base_url)
    assert spike.state_of(session, proposal) == "unknown"
    assert downstream.mutations() == 1, "the downstream commits first and then answers late"
    monkeypatch.setattr(spike, "TIMEOUT_SECONDS", 5.0)
    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "committed"
    assert downstream.mutations() == 1


def test_only_an_unknown_proposal_is_reconciled(session: Session, downstream: Downstream) -> None:
    proposal = make_proposal(session)
    with pytest.raises(spike.IllegalTransition, match="only an unknown proposal"):
        spike.reconcile(session, proposal["proposal_id"], downstream.base_url)


def test_an_unreadable_downstream_leaves_the_proposal_unknown(
    session: Session, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = make_proposal(session)
    spike.transition(session, proposal["proposal_id"], "dispatched")
    spike.transition(session, proposal["proposal_id"], "unknown")

    def server_error(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    monkeypatch.setattr(httpx.Client, "get", server_error)
    with pytest.raises(RuntimeError, match="503"):
        spike.reconcile(session, proposal["proposal_id"], downstream.base_url)
    assert spike.state_of(session, proposal) == "unknown"


def _unknown_proposal(session: Session) -> dict[str, Any]:
    proposal = make_proposal(session)
    spike.transition(session, proposal["proposal_id"], "dispatched")
    spike.transition(session, proposal["proposal_id"], "unknown")
    return proposal


def test_a_stored_request_that_is_not_the_one_the_proposal_meant_is_not_a_receipt(
    session: Session, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _unknown_proposal(session)

    def other_request(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        body = {"request_id": "DR-9", "case_id": CASE, "document_type": "bank_statement"}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx.Client, "get", other_request)
    with pytest.raises(RuntimeError, match="not the one this proposal meant"):
        spike.reconcile(session, proposal["proposal_id"], downstream.base_url)
    assert spike.state_of(session, proposal) == "unknown"


def test_a_stored_request_without_an_id_is_not_a_receipt(
    session: Session, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _unknown_proposal(session)

    def no_id(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"case_id": CASE, "document_type": "pay_statement"})

    monkeypatch.setattr(httpx.Client, "get", no_id)
    with pytest.raises(RuntimeError, match="no request id"):
        spike.reconcile(session, proposal["proposal_id"], downstream.base_url)
    assert spike.state_of(session, proposal) == "unknown"


# ---------------------------------------------------------------- two actors, one proposal


def test_a_stale_read_cannot_overwrite_a_newer_state(session: Session) -> None:
    """Two actors read ``proposed``; one moves it to ``dispatched``; the other's update, conditioned on what it
    read, matches nothing. This is the property that stops two dispatchers both sending, tested directly because
    a thread race is too narrow to hit reliably."""
    proposal = make_proposal(session)
    spike.transition(session, proposal["proposal_id"], "dispatched")  # the first actor wins
    with pytest.raises(spike.IllegalTransition, match="changed under this actor"):
        spike.compare_and_set(session, proposal["proposal_id"], "proposed", "dispatched")
    assert spike.state_of(session, proposal) == "dispatched"


def test_two_dispatchers_of_one_proposal_send_once(
    session: Session, downstream: Downstream
) -> None:
    """The state change is a compare-and-set, so only one of two concurrent dispatchers sends."""
    proposal = make_proposal(session)
    engine = session.get_bind()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def dispatcher() -> None:
        with Session(engine) as mine:
            barrier.wait()
            try:
                spike.dispatch(mine, proposal, downstream.base_url)
                outcomes.append("sent")
            except spike.IllegalTransition:
                outcomes.append("refused")

    threads = [threading.Thread(target=dispatcher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["refused", "sent"]
    assert downstream.mutations() == 1


def test_a_second_actor_cannot_settle_what_the_first_already_settled(
    session: Session, downstream: Downstream
) -> None:
    proposal = make_proposal(session)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.RemoteProtocolError):
        spike.dispatch(session, proposal, downstream.base_url)
    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "committed"
    with pytest.raises(spike.IllegalTransition, match="only an unknown proposal"):
        spike.reconcile(session, proposal["proposal_id"], downstream.base_url)


# ---------------------------------------------------------------- the process dies after the commit


def test_kill_after_commit_then_reconcile_in_new_process(
    session: Session, downstream: Downstream
) -> None:
    """The dispatching process is killed while it waits for a late answer: the downstream already committed."""
    proposal = make_proposal(session)
    downstream.set_fault("delay_response_seconds", seconds=20)
    url = session.get_bind().url.render_as_string(hide_password=False)
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.egi_scenarios.prototype.dispatch_child",
            "--base-url",
            downstream.base_url,
            "--proposal-id",
            proposal["proposal_id"],
        ],
        cwd=CALIBER_DIR,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(CALIBER_DIR), str(CALIBER_DIR / "src")]),
            "SPIKE_DB_URL": url,  # in the environment, not the command line, where `ps` would show a password
            "SPIKE_TIMEOUT_SECONDS": "60",  # the 20 s delayed answer, not the client timeout, bounds the window
        },
    )
    try:
        deadline = time.monotonic() + STARTED_BY_SECONDS
        while downstream.mutations() < 1:
            assert child.poll() is None, (
                "the dispatching process exited before the downstream committed"
            )
            assert time.monotonic() < deadline, "the downstream never committed"
            time.sleep(0.05)
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == -9

    session.expire_all()
    assert spike.state_of(session, proposal) == "dispatched", (
        "the dead process could not record the outcome"
    )
    spike.transition(session, proposal["proposal_id"], "unknown")
    with pytest.raises(spike.UnknownEffectError):
        spike.dependent_action(session, proposal["proposal_id"])

    assert spike.reconcile(session, proposal["proposal_id"], downstream.base_url) == "committed"
    assert downstream.mutations() == 1
    stored = downstream.query(proposal["logical_key"])
    assert stored is not None and request_id_of(session, proposal) == stored["request_id"]
