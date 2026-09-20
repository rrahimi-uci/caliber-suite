#!/usr/bin/env python3
"""Days 2-3 of ``docs/two-week-alpha-plan.md``: diagnose -> candidate -> evaluate.

Human-facing demo script for the plan's own Week 1 acceptance criterion:

    "Running one script against a clean checkout produces a scored
    candidate, reproducibly."

Run it from the ``caliber/`` directory of a checkout with the dev + dspy
extras installed (``pip install -e ".[dev,dspy]"``)::

    python scripts/two_week_alpha_day2_run.py

It seeds a throwaway SQLite database (deleted on exit), drives the Day 1
seed fixture (``tests/fixtures/day1_seed_fixture.py`` -- one flagged trace,
one prompt, one 12-case eval set) through the real orchestrator stages --
evidence -> diagnosis -> candidate -> eval -- and prints the resulting scored
candidate as JSON. No live network call, no MLflow server, no OpenAI API key:
diagnosis uses a canned deterministic response, and candidate generation runs
the REAL DSPy ``BootstrapFewShot`` bridge (``caliber.llm.dspy_optimizer``)
against the fixture's real eval set under a deterministic stand-in LM.
Running it twice produces byte-identical output.

The actual pipeline-driving logic lives in
``tests/fixtures/day2_day3_pipeline.py::run_diagnose_candidate_evaluate`` --
read that module's docstring for the full "real DSPy path vs.
deterministic/offline" design rationale. This script and
``tests/test_two_week_alpha_day2_pipeline.py`` (the CI-enforced regression
test for the same acceptance criterion) both call it, so there is exactly one
place this wiring is implemented.
"""

# This is a CLI/demo tool -- stdout (the printed JSON payload) is its
# interface, so print() is intentional. Same convention as
# scripts/publish_allure.py.
# ruff: noqa: T201

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path

CALIBER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBER_ROOT / "src"))
# ``tests`` (and the Day 1/Day 2-3 fixture modules under ``tests/fixtures/``)
# is not part of the installed ``caliber`` distribution -- it is this
# checkout's test package. The Day 1 decision record names this script as
# exactly the second caller its fixture loaders were written for.
sys.path.insert(0, str(CALIBER_ROOT))

from tests.fixtures.day2_day3_pipeline import run_diagnose_candidate_evaluate  # noqa: E402

from caliber.config import CaliberConfig  # noqa: E402
from caliber.db import Base  # noqa: E402
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="caliber-two-week-alpha-day2-") as tmp_dir:
        db_path = Path(tmp_dir) / "day2_day3_run.db"
        config = CaliberConfig.load(
            environ={"CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db_path}"}
        )
        engine = create_engine_from_config(config)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker_from_engine(engine)

        try:
            # DSPy's BootstrapFewShot teleprompter prints its own bootstrap
            # progress (a tqdm bar + a summary line) directly to stdout,
            # unconditionally -- there is no verbosity flag to suppress it
            # (checked: `BootstrapFewShot.__init__` takes no such option).
            # Redirect it to stderr for the run so this script's stdout stays
            # clean, valid JSON -- the one thing a "produces a scored
            # candidate" demo script must guarantee.
            with session_factory() as session, contextlib.redirect_stdout(sys.stderr):
                result = run_diagnose_candidate_evaluate(session, config=config)
        finally:
            engine.dispose()

    payload = {
        "job_id": result.job_id,
        "status": result.status,
        "optimizer_type": result.optimizer_type,
        "diagnosis": result.diagnosis,
        "candidate": result.candidate,
        "eval_results": result.eval_results,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if result.status != "candidate_ready":
        print(
            f"\nUNEXPECTED: job ended at status={result.status!r}, not candidate_ready "
            "-- the eval gate rejected the candidate. See eval_results.gate.reasons above.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nOK: job {result.job_id} reached candidate_ready with optimizer="
        f"{result.optimizer_type!r}, gate.passed=True, "
        f"n_examples={result.eval_results.get('n_examples')}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
