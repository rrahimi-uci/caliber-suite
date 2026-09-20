#!/usr/bin/env python3
"""Seeds the two-week-alpha journey's fixture data for the Playwright E2E server.

See ``docs/two-week-alpha-plan.md`` (Week 2, Day 9) and
``docs/reports/two-week-alpha-day9-decision.md`` for the full rationale.
Days 1-8 built the fixture (a flagged trace, the ``intake-classifier``
prompt, a 12-case eval set -- ``tests/fixtures/day1_seed_fixture.py``), the
real diagnose -> candidate -> evaluate pipeline
(``tests/fixtures/day2_day3_pipeline.py``), and the UI that drives a
seeded flagged item through to Apply/rollback
(``caliber-ui/src/components/PromptDiagnosisTab.tsx``,
``caliber-ui/src/pages/Prompts.tsx``'s Calibration tab,
``caliber-ui/src/pages/Prompts.tsx``'s Author-tab version history/rollback).
Day 9's Playwright journey (``caliber-ui/e2e/two-week-alpha-journey.spec.ts``)
needs that fixture already sitting in the database the running dev server
points at *before* the browser test drives it -- the fixture's own loader
functions are plain SQLAlchemy-session Python, not something reachable
through the running server's HTTP API. This script is the bridge.

Invoked exactly once, by ``scripts/run-playwright-server.sh``'s
``run_owner_bootstrap`` -- the same "seed once, against a freshly-created DB"
slot that script already reserved for a (currently absent) ``caliber.demo``
scenario seed -- and, critically, run **before** ``./scripts/run-dev.sh``
(the actual dev server) is started, not after it reports healthy. An earlier
version of this script seeded through the real HTTP API once the server's
health endpoint was already green; that raced Playwright's own independent
poll of that same endpoint (`playwright.config.ts`'s `webServer.url`) --
Playwright could start driving the browser before this script (login +
prompt creation + the real DSPy pipeline, several seconds of work) finished,
observing an empty prompt registry (reproduced: "0 Agents in registry" / "No
agents match the current filters" in the Prompts UI, `expect(card).toBeVisible()`
timing out). Running before anything is listening on the port at all makes
that race structurally impossible -- Playwright's health poll cannot succeed
before this script has already committed everything.

This is not idempotent end-to-end (``seed_flagged_job`` always inserts a
fresh verification item + job, and prompt registration is not upsert-safe)
and is not meant to be run against a DB that might already hold this
fixture's data -- the normal automated path (the fresh-DB bootstrap) never
re-runs it against non-fresh state, so that is not a concern there. A
developer using ``CALIBER_E2E_USE_EXISTING_SERVER=1`` against a DB that
might already have this fixture should not re-run this script by hand
without first clearing that DB (e.g. via ``--force-cleanup``).

Two steps, entirely in-process (no HTTP call, no server needs to be up):

1. **Register the fixture's prompt directly**, calling the exact same
   internal functions ``POST /prompts`` itself calls --
   ``routes/prompts.py::register_prompt_version`` (registers MLflow prompt
   version 1) and ``caliber.prompt_targets.ensure_prompt_target`` (creates
   the hidden runtime identity, ``agent_id == prompt name``) -- then
   ``routes/prompts.py::set_prompt_alias_version`` to point ``prod`` at
   version 1. This is a **real** MLflow-registered prompt target, not a
   fake -- Day 4-5's own decision record needed a ``FakePromptRegistry`` to
   fake this for pytest; here it does not need to be faked, because calling
   these functions directly against the same backend store the dev server
   will open (via ``MLFLOW_TRACKING_URI``, set below) produces the exact
   same registry state a real ``POST /prompts`` call would. Prompt
   registration is not gated by ``promoter_provider`` (confirmed by reading
   ``register_prompt_version`` -- it always calls ``mlflow.genai`` directly);
   this E2E server's ``promoter_provider`` is set to ``mlflow`` separately
   (see ``run-playwright-server.sh``) so the later Apply click in the
   browser drives the real ``caliber.release_operations`` state machine too,
   not ``FakePromoter`` (which Day 4-5's decision record notes "never
   touches ``caliber.release_operations`` at all").
2. **Drive the seed fixture's flagged item + refinement job through the
   real Day 2-3 pipeline** (``tests/fixtures/day2_day3_pipeline.py::run_diagnose_candidate_evaluate``),
   reusing the prompt's already-provisioned ``agent_id`` (via
   ``seed_agent``'s get-or-update path) instead of inserting a second,
   colliding ``CaliberAgentConfig`` row. This is exactly what Days 2-3
   proved offline and reproducibly (real DSPy ``BootstrapFewShot``, no live
   network call) -- run once more here, against the E2E server's own DB, so
   the browser has a genuine ``candidate_ready`` job with real DSPy
   candidate content and real eval evidence to show.

The caller (``run-playwright-server.sh``) is responsible for having already
applied migrations (``alembic upgrade head``) before invoking this script --
this script does not create or migrate schema itself.
"""

# This is a CLI/bootstrap tool -- the printed lines are its operator-facing
# progress/diagnostics (all to stderr; see scripts/two_week_alpha_day2_run.py
# for the same convention), so print() is intentional throughout.
# ruff: noqa: T201

from __future__ import annotations

import os
import sys
from pathlib import Path

CALIBER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBER_ROOT / "src"))
# ``tests`` (and the Day 1/Day 2-3 fixture modules under ``tests/fixtures/``)
# is not part of the installed ``caliber`` distribution -- it is this
# checkout's test package, the same convention
# ``scripts/two_week_alpha_day2_run.py`` already established.
sys.path.insert(0, str(CALIBER_ROOT))

# Must happen before ``import mlflow`` (directly, or transitively via any
# ``caliber.*`` import below) -- ``mlflow.genai.register_prompt``/
# ``set_prompt_alias``/``load_prompt`` resolve their store from
# ``mlflow.get_tracking_uri()``, which otherwise defaults to a local
# ``./mlruns`` directory having nothing to do with this E2E server's real
# backend store. Pointing it at the same store
# ``run-playwright-server.sh`` configures (``MLFLOW_BACKEND_STORE_URI``,
# itself derived from ``CALIBER_DATABASE_URL``) makes this script's writes
# land exactly where the dev server it precedes will look for them --
# verified directly (a standalone probe script with only this env var set,
# no running server) before relying on it here.
os.environ.setdefault(
    "MLFLOW_TRACKING_URI",
    os.environ.get("MLFLOW_BACKEND_STORE_URI", os.environ.get("CALIBER_DATABASE_URL", "")),
)

from tests.fixtures.day1_seed_fixture import DEFAULT_AGENT_ID, load_prompt_template  # noqa: E402
from tests.fixtures.day2_day3_pipeline import run_diagnose_candidate_evaluate  # noqa: E402

from caliber.config import CaliberConfig  # noqa: E402
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine  # noqa: E402
from caliber.prompt_targets import ensure_prompt_target  # noqa: E402
from caliber.routes.prompts import register_prompt_version, set_prompt_alias_version  # noqa: E402

# The one fixed prompt name this journey seeds and the Playwright spec looks
# for. Reusing Day 1's own ``DEFAULT_AGENT_ID`` means the prompt's
# auto-provisioned ``agent_id`` (== its name, see ``ensure_prompt_target``)
# lines up with the fixture's own default with no extra plumbing.
PROMPT_NAME = DEFAULT_AGENT_ID

# Matches the dev-bootstrap admin identity this E2E server always
# provisions (``CALIBER_DEV_USER``/``CALIBER_ADMIN_USERS`` default to
# ``admin`` in ``run-playwright-server.sh``) -- used only as the recorded
# "owner" on the hidden prompt target, not for authentication (this script
# never makes an HTTP call).
SEED_OWNER = "admin"


def _seed_prompt() -> int:
    """Register the fixture prompt's v1 and point ``prod`` at it.

    Mirrors exactly what ``POST /prompts`` does (``routes/prompts.py::create_prompt``):
    register a version, then auto-provision the hidden runtime target -- with
    one addition, setting the ``prod`` alias, which ``create_prompt`` itself
    deliberately does not do (prompt creation is documented as "non-live");
    this fixture needs a live v1 so Apply's later promotion has a genuine
    prior version to roll back to, the same role
    ``FakePromptRegistry.seed_initial_version`` plays in the Day 4-5 pytest
    suite.
    """
    result = register_prompt_version(
        name=PROMPT_NAME,
        template=load_prompt_template(),
        commit_message="two-week-alpha E2E seed (Day 9)",
        source="two-week-alpha-e2e-seed",
        set_prod_alias=False,
    )
    version = int(result["version"])

    config = CaliberConfig.load()
    engine = create_engine_from_config(config)
    try:
        session_factory = sessionmaker_from_engine(engine)
        with session_factory() as session:
            ensure_prompt_target(session, PROMPT_NAME, owner=SEED_OWNER)
            session.commit()
    finally:
        engine.dispose()

    set_prompt_alias_version(name=PROMPT_NAME, alias="prod", version=version)
    return version


def main() -> int:
    version = _seed_prompt()
    print(
        f">> two-week-alpha E2E prompt seeded: {PROMPT_NAME!r} v{version} live on @prod",
        file=sys.stderr,
    )

    config = CaliberConfig.load()
    engine = create_engine_from_config(config)
    try:
        session_factory = sessionmaker_from_engine(engine)
        with session_factory() as session:
            result = run_diagnose_candidate_evaluate(session, agent_id=PROMPT_NAME, config=config)
    finally:
        engine.dispose()

    if result.status != "candidate_ready":
        print(
            f"error: two-week-alpha E2E seed job ended at status={result.status!r}, not "
            "candidate_ready -- the eval gate rejected the candidate. See "
            "eval_results.gate.reasons.",
            file=sys.stderr,
        )
        return 1

    print(
        f">> two-week-alpha E2E fixture ready: prompt={PROMPT_NAME!r} job={result.job_id!r} "
        f"optimizer={result.optimizer_type!r} status=candidate_ready",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
