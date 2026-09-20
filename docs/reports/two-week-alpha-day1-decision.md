---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Day 1 of docs/two-week-alpha-plan.md -- the release-governance path decision, the provider-profile decision, and the one committed seed fixture.
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 1 table (Day 1 row) for the deliverable and acceptance this record satisfies
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Day 1 decision record

Last updated: 2026-09-19

## Path decision

This journey targets **the roadmap's own single-tenant release-governance
path** -- `caliber/src/caliber/release_operations.py`,
`caliber/src/caliber/routes/releases.py`,
`caliber/src/caliber/workflows/deploy_gate.py`,
`caliber/src/caliber/workflows/effect_ledger.py`,
`caliber/src/caliber/trace_client.py`, `caliber/src/caliber/llm/dspy_optimizer.py`,
`caliber/src/caliber/assistant/service.py`, and the 6-stage
triage -> evidence -> diagnosis -> candidate -> eval -> apply pipeline under
`caliber/src/caliber/orchestrator/` plus `caliber/src/caliber/apply.py` -- not
the separate, newer, multi-tenant "Workspace" release-governance subsystem
(`workspace_release_service.py` / `workspace_release_governance.py`), which
was built for a different initiative with its own evaluation gates,
quality-signoff, approve, and break-glass semantics. The PRD and roadmap
reference the roadmap's own path; the Workspace subsystem is architecturally
distinct and out of scope for these two weeks.

## Provider decision

The one provider profile for this journey is **OpenAI**
(`llm_provider="openai"`). This is not really an open choice: the DSPy
candidate-refinement path this plan wants to demonstrate
(`caliber/src/caliber/llm/dspy_optimizer.py`) is only reachable through the
OpenAI-backed provider today -- its own module docstring states DSPy "ships
in the dedicated `[dspy]` extra on top of the OpenAI-backed provider" and
that "the OpenAI provider lazily loads this module" when a DSPy optimizer is
selected. No other provider implementation in this codebase wires DSPy in at
all. The OpenAI provider is also the most heavily tested path in the repo
(`tests/test_openai_agents_provider.py`, 25 tests), which further supports
picking it as the one profile a two-week alpha checkpoint stakes its backend
proof on.

One important non-obvious finding this decision depends on: the DSPy
candidate path is **opt-in, not the automatic default**. Without an explicit
override, `caliber/src/caliber/orchestrator/optimizer_select.py::select_optimizer`
picks `MetaPrompt` for most diagnoses (`GEPA` fires automatically only for
low-confidence/many-alternative diagnoses). To actually exercise the DSPy
path, a job must set `CaliberRefinementJob.optimizer_type` explicitly to
`DSPyBootstrapFewShot` (or `DSPyMIPRO`) -- the first branch
`select_optimizer` checks, ahead of any automatic rule. The seed fixture
below does exactly this.

## The seed fixture

The committed fixture lives under `caliber/tests/fixtures/day1_seed/` (data)
and `caliber/tests/fixtures/day1_seed_fixture.py` (loader). It packages one
fixed **trace**, one fixed **prompt**, and one fixed **eval set**, chosen to
tell a single coherent story: the `intake-classifier` support-ticket
classification prompt (reused from the first-milestone pilot fixture pack's
"Fixture A" -- `docs/reports/first-milestone-pilot-fixture-pack.md`, prompt
source `docs-site/cookbooks/01-prompt-regression-lab/assets/prompts/intake-classifier.md`)
produced a flagged trace in which it classified a compound support ticket
("I was double charged AND the page keeps crashing when I open billing.") as
a single billing issue and set `needs_review=false`, when the ticket's
ambiguity should have forced review. The trace is captured as a recorded,
replayable `TraceSummary` (`caliber/tests/fixtures/day1_seed/flagged_trace.json`)
loaded through a `FakeTraceClient` -- the same offline-safe convention
`caliber/tests/test_evidence_trace.py` already uses to exercise the real
`orchestrator.evidence.run_evidence` stage without a live MLflow server, per
this repo's standing rule that ordinary validation stays deterministic and
offline. The eval set is the same pilot fixture pack's 12-case
`intake-classifier.jsonl` (golden, ambiguous/edge, and prompt-injection
cases), reused byte-for-byte, because its P07-P09 ambiguous/edge cases are
exactly the worked examples a DSPy few-shot bootstrap would draw on to fix
the flagged failure -- which is why the seed job pins
`optimizer_type="DSPyBootstrapFewShot"` explicitly rather than relying on
automatic selection (see the provider decision above). Seeding follows the
same pattern `caliber/tests/test_e2e_pipeline.py::_seed_verified_job` already
uses (a verified `CaliberVerificationItem` plus a queued
`CaliberRefinementJob`), via `day1_seed_fixture.py::seed_flagged_job`.

`caliber/tests/test_day1_seed_fixture.py` proves the fixture is well-formed
and loadable: the prompt template parses and carries its required
`{{ ticket_text }}` / `{{ channel }}` / `{{ metadata }}` variables; the
eval-set JSONL parses into 12 well-formed rows matching the schema
`CaliberEvalDatasetExample` and DSPy's trainset loader expect; the flagged
trace loads as a `TraceSummary` and, injected into the real evidence stage
via `FakeTraceClient`, produces the expected trace evidence on the audit
log; and seeding the fixture produces a job whose optimizer selection
resolves to `DSPyBootstrapFewShot` through the explicit-override path, while
a regression test pins down that *without* that override the same diagnosis
would resolve to `MetaPrompt` -- the non-obvious trap this decision exists to
avoid. Per the plan's own Day 1 acceptance, this is deliberately scoped to
"fixture committed and loadable," not a full diagnosis/candidate/eval run
against a real LLM -- that wiring is Day 2-3's job, and it can now build
directly on `day1_seed_fixture.py`'s loader functions
(`load_prompt_template`, `load_eval_examples`,
`eval_examples_as_dataset_rows`, `load_flagged_trace_summary`,
`build_fake_trace_client`, `seed_flagged_job`).
