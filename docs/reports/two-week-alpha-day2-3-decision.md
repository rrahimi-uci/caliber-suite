---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Days 2-3 of docs/two-week-alpha-plan.md -- wiring diagnose -> candidate -> evaluate for the Day 1 seed fixture, and how the "real DSPy" vs. "deterministic/offline" tension was resolved.
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 1 table (Days 2-3 row) for the deliverable and acceptance this record satisfies
  - Read docs/reports/two-week-alpha-day1-decision.md for the fixture, path, and provider decisions this builds on
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Days 2-3 decision record

Last updated: 2026-09-19

## What this delivers

The plan's own Week 1 table, Days 2-3 row: "Wire diagnosis -> candidate ->
evaluate for the seed fixture, backend only (script/API call, not UI yet)."
Acceptance: "Running one script against a clean checkout produces a scored
candidate, reproducibly."

Both requirements are met by:

* `caliber/tests/fixtures/day2_day3_pipeline.py` -- the pipeline-driving
  logic (`run_diagnose_candidate_evaluate`), used by both callers below so
  the wiring exists exactly once.
* `caliber/scripts/two_week_alpha_day2_run.py` -- the human-facing demo
  script the plan's Day 2-3 row literally asks for. Seeds a throwaway
  SQLite DB, runs the pipeline, prints the scored candidate as JSON.
* `caliber/tests/test_two_week_alpha_day2_pipeline.py` -- the CI-enforced
  regression test. This, not the script, is what actually guards
  "reproducibly" on every future change.

## The tension: real DSPy path vs. deterministic/offline

The plan's acceptance wording ("reproducibly") and this repo's own standing
rule (`CLAUDE.md`: "ordinary validation stays deterministic and offline")
both demand no live network/model call. But the point of this slice --
feeding Day 4-5's Apply work a genuine `candidate_ready` job -- is to prove
the *real* DSPy candidate path runs, not a second fake standing in for it.

`caliber/tests/test_dspy_optimizer.py` already resolves exactly this tension
for unit-level DSPy coverage: its
`test_run_bootstrap_fewshot_end_to_end_with_dummy_lm` drives the real
`BootstrapFewShot` teleprompter by patching `dspy.LM` (the one seam that
would otherwise reach a live model) to `dspy.utils.dummies.DummyLM`. This
slice follows that precedent at the full pipeline level, with one addition:
where the existing test's `DummyLM` answers a fixed, unrelated string
("blue"), `day2_day3_pipeline.py::build_dummy_dspy_lm` maps each of the
fixture's real 12 eval examples' `ticket_text` to its own real `expectations`
(JSON-serialized the same way
`caliber.llm.dspy_optimizer._example_text`'s dict fallback does). `DummyLM`
supports exactly this: a `{substring_of_prompt: answer}` mapping, matched
against the rendered prompt. The result is that DSPy's own deterministic
`_demo_metric` containment check genuinely passes for the fixture's own
examples, and `BootstrapFewShot` bootstraps real few-shot demonstrations
from real fixture data -- not zero demos from a mismatched dummy answer.
Running the script twice confirms this: candidate content, rationale,
`diff_summary`, and the eval gate outcome are byte-identical across
independent runs (only the generated row IDs differ, as expected); one
representative run selected 4 real few-shot demonstrations
(`Bootstrapped 4 full traces after 4 examples for up to 1 rounds`) drawn
from the fixture's own golden examples.

Diagnosis, by contrast, is a plain `FakeLLMProvider` canned response. This is
deliberate, not a shortcut: the Day 1 seed job already pins
`job.optimizer_type = "DSPyBootstrapFewShot"` explicitly (see Day 1's
decision record -- automatic selection from diagnosis text would not reach
DSPy for this diagnosis), so nothing about *which* optimizer runs depends on
diagnosis content. Faking it keeps the run offline without weakening the one
thing this slice needs to prove genuinely: the DSPy candidate path.

Eval uses `FakeEvalProvider` -- the same deterministic double every other
integration test in this repo uses (`test_e2e_pipeline.py` included) --
scored against the fixture's real, DB-seeded 12-example dataset rather than a
trivial 2-3 row stand-in (`n_examples` and `eval_dataset_id` on the resulting
comparison reflect the genuine fixture data).

## The `[dspy]` extra

Already installed in this checkout's `.venv` (`dspy==3.2.1`) -- confirmed by
`python -c "import dspy; print(dspy.__version__)"` before writing any code,
per the task's own instruction not to assume either way. `caliber/pyproject.toml`
already declares it as a normal optional-dependency extra
(`[project.optional-dependencies].dspy`), and `tests/test_dspy_optimizer.py`
already gates on it with `pytest.importorskip("dspy")` -- this slice's new
test does the same, so CI (which installs the extended dev profile,
`caliber/Makefile`'s `install-extended`, per `.github/workflows/ci.yml`)
exercises the real DSPy path, and a checkout without the extra skips cleanly
rather than failing.

## Why the pipeline-driving code lives under `tests/fixtures/`, not `src/`

`day2_day3_pipeline.py` seeds fixture data and drives existing orchestrator
stage functions (`run_evidence`, `run_diagnosis`, `run_candidate`,
`run_eval`) with a scenario-specific `FakeLLMProvider`/`FakeEvalProvider`
combination -- it is demo/proof-of-path wiring for this one seeded scenario,
not new product behavior. It follows Day 1's own placement precedent
(`tests/fixtures/day1_seed_fixture.py`) and its own docstring: "Callers
(tests today, a Day 2-3 backend-wiring script next)". No `src/caliber`
production code changed in this slice -- the orchestrator stage functions,
`FakeLLMProvider`, `FakeEvalProvider`, and the DSPy bridge are all used
exactly as they already exist and are already tested elsewhere. Since
`pyproject.toml`'s pytest config runs with `caliber/` as rootdir and
`tests/__init__.py` present, both the pytest test and the standalone script
(which inserts `caliber/` onto `sys.path` before importing, the same pattern
`scripts/export_cookbook_capabilities.py` already uses for `src/`) resolve
`tests.fixtures.day2_day3_pipeline` the same way.

## What Day 4-5 inherits

Running either the script or the pytest test leaves a genuine
`CaliberRefinementJob` at `status="candidate_ready"` with:

* `job.diagnosis` -- the canned-but-structurally-real diagnosis;
* `job.candidate` -- real DSPy `BootstrapFewShot` output (original prompt +
  real few-shot demos), `optimizer_type="DSPyBootstrapFewShot"`;
* `job.eval_results` -- a gate decision (`passed=True` against the default
  fake eval provider's scores vs. the agent's `min_aggregate_score=0.80`
  threshold) scored against the real, seeded 12-example dataset.

Day 4-5 (Apply -> release -> rollback) can drive this same job through the
existing Apply endpoint / `apply.py` exactly as
`tests/test_e2e_pipeline.py::test_register_seed_pipeline_apply` already does
for its own seeded job.
