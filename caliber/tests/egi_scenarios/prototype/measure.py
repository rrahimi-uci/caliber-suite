"""DISPOSABLE (#625): the numbers quoted in the findings, reproducibly.

``cd caliber && PYTHONPATH=src:. python -m tests.egi_scenarios.prototype.measure`` runs 40 dispatches of each kind
against the downstream process on loopback with a SQLite proposal store and prints median and maximum milliseconds.
"""

from __future__ import annotations

import contextlib
import pathlib
import statistics
import tempfile
import time

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.egi_scenarios.conftest import Downstream
from tests.egi_scenarios.prototype import dispatch_spike as spike

RUNS = 40
CASE = "MORT-2026-000184"


def _ms(values: list[float]) -> tuple[float, float]:
    return round(statistics.median(values) * 1000, 1), round(max(values) * 1000, 1)


def main() -> None:
    downstream = Downstream(pathlib.Path(tempfile.mkdtemp()))
    downstream.start()
    engine = create_engine("sqlite://")
    spike.metadata.create_all(engine)
    first: list[float] = []
    replay: list[float] = []
    dropped: list[float] = []
    try:
        with Session(engine) as session:
            for i in range(RUNS):
                downstream.reset()
                proposal = spike.propose(
                    session,
                    workflow_run_id=f"r{i}",
                    node_id="n",
                    case_id=CASE,
                    document_type="pay_statement",
                    expected_version="1",
                )
                started = time.perf_counter()
                spike.dispatch(session, proposal, downstream.base_url)
                first.append(time.perf_counter() - started)
                started = time.perf_counter()
                spike.dispatch(session, proposal, downstream.base_url)
                replay.append(time.perf_counter() - started)

                downstream.reset()
                lost = spike.propose(
                    session,
                    workflow_run_id=f"x{i}",
                    node_id="n",
                    case_id=CASE,
                    document_type="pay_statement",
                    expected_version="1",
                )
                downstream.set_fault("drop_response_after_commit")
                started = time.perf_counter()
                with contextlib.suppress(httpx.RemoteProtocolError):
                    spike.dispatch(session, lost, downstream.base_url)
                dropped.append(time.perf_counter() - started)
    finally:
        downstream.close()
    print(f"first dispatch   median/max ms: {_ms(first)}")  # noqa: T201
    print(f"replay            median/max ms: {_ms(replay)}")  # noqa: T201
    print(f"dropped response median/max ms: {_ms(dropped)}")  # noqa: T201


if __name__ == "__main__":
    main()
