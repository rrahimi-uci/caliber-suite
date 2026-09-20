"""Days 2-3 of the two-week alpha plan: diagnose -> candidate -> evaluate.

See ``docs/two-week-alpha-plan.md`` (Week 1, Days 2-3) and
``docs/reports/two-week-alpha-day1-decision.md`` for the fixture, path, and
provider decisions this module builds on. Day 1 delivered a committed,
loadable fixture (``tests/fixtures/day1_seed_fixture.py``); this module wires
that fixture through the real orchestrator stages -- evidence -> diagnosis ->
candidate -> eval -- and produces a scored candidate, reproducibly.

**The tension this module resolves:** the plan's own Day 2-3 acceptance
("running one script against a clean checkout produces a scored candidate,
reproducibly") demands determinism and offline execution; but the point of
this slice is to prove the real DSPy candidate path (not a second, unrelated
fake) actually runs against the seed fixture's real eval set. Resolution,
following ``tests/test_dspy_optimizer.py``'s own precedent
(``test_run_bootstrap_fewshot_end_to_end_with_dummy_lm``, which drives the
real ``BootstrapFewShot`` teleprompter over ``dspy``'s ``DummyLM`` instead of a
live model):

* **Diagnosis** uses :class:`caliber.llm.fake.FakeLLMProvider` with a fixed,
  canned :class:`~caliber.llm.provider.Diagnosis` -- offline, and identical on
  every run. Nothing about which optimizer runs depends on this diagnosis
  content anyway: the Day 1 seed job pins ``optimizer_type`` to
  ``"DSPyBootstrapFewShot"`` explicitly (see
  ``day1_seed_fixture.SEED_OPTIMIZER_TYPE``), so automatic selection from the
  diagnosis text never enters the picture.
* **Candidate generation** calls the REAL production DSPy bridge,
  :func:`caliber.llm.dspy_optimizer.run_bootstrap_fewshot`, against the real
  ``BootstrapFewShot`` teleprompter and the fixture's real 12-case eval set
  (loaded as the DSPy trainset via ``orchestrator/candidate.py::_load_trainset``,
  the same production code path). The only thing swapped out is ``dspy.LM``
  itself -- patched (see :func:`deterministic_dspy_lm`) to a
  ``dspy.utils.dummies.DummyLM`` pre-loaded with the fixture's own expected
  answers, so BootstrapFewShot's per-example "run the teacher, keep the trace
  if the metric passes" loop is exercised for real, deterministically, with no
  network call. This is not a second fake standing in for DSPy -- it is DSPy,
  with only the LM's remote call intercepted (the same seam
  ``test_dspy_optimizer.py`` already patches for exactly this reason).
* **Eval** uses :class:`caliber.eval.fake.FakeEvalProvider` (the same
  deterministic double every other integration test in this repo uses,
  including ``tests/test_e2e_pipeline.py``), but scored against the fixture's
  real seeded 12-example dataset -- ``n_examples`` and ``eval_dataset_id`` on
  the resulting :class:`~caliber.eval.provider.EvalComparison` reflect the
  genuine fixture data, not a 2-3 row stand-in.

Both the Day 2-3 demo script (``scripts/two_week_alpha_day2_run.py``) and the
CI-enforced regression test (``tests/test_two_week_alpha_day2_pipeline.py``)
call :func:`run_diagnose_candidate_evaluate` -- the pipeline-driving logic
lives here exactly once.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from sqlalchemy.orm import Session

from caliber.artifact_store import FakeArtifactStore
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.ids import new_eval_dataset_id, new_eval_example_id
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import CandidateContext, Diagnosis, LLMProvider, LLMUsage, PromptCandidate
from caliber.orchestrator.candidate import run_candidate
from caliber.orchestrator.diagnosis import run_diagnosis
from caliber.orchestrator.eval_stage import run_eval
from caliber.orchestrator.evidence import run_evidence
from tests.fixtures.day1_seed_fixture import (
    DEFAULT_AGENT_ID,
    EvalExample,
    build_fake_trace_client,
    eval_examples_as_dataset_rows,
    load_eval_examples,
    load_prompt_template,
    seed_flagged_job,
)

if TYPE_CHECKING:
    from dspy.utils.dummies import DummyLM

# The eval dataset name ``_load_trainset``/``_resolve_eval_dataset`` fall back
# to when the agent doesn't pin ``eval_thresholds["eval_dataset_id"]`` -- see
# ``orchestrator/candidate.py::_DEFAULT_EVAL_DATASET`` /
# ``orchestrator/eval_stage.py::_DEFAULT_EVAL_DATASET``. Seeding the fixture's
# dataset under this name means the agent config below needs no explicit
# override to reach it from either stage.
DEFAULT_EVAL_DATASET_NAME = "default"

# The candidate generation model id passed to the DSPy bridge. Never actually
# reaches a network call in this pipeline (``dspy.LM`` is patched -- see
# :func:`deterministic_dspy_lm`); kept as a realistic value purely so the
# rendered LM handle / any log lines look like production.
DSPY_MODEL_ID = "gpt-4o-mini"
DSPY_MAX_BOOTSTRAPPED_DEMOS = 4
DSPY_MAX_LABELED_DEMOS = 4


@dataclass(frozen=True)
class PipelineRunResult:
    """The outcome of one diagnose -> candidate -> evaluate pipeline run."""

    job_id: str
    status: str
    optimizer_type: str | None
    diagnosis: dict[str, Any]
    candidate: dict[str, Any]
    eval_results: dict[str, Any]

    @property
    def passed_gate(self) -> bool:
        gate = self.eval_results.get("gate")
        return bool(isinstance(gate, dict) and gate.get("passed"))


def seed_agent(
    session: Session,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    owner: str = "@day1-seed-fixture",
) -> CaliberAgentConfig:
    """Register the Day 1 seed fixture's agent, mirroring
    ``tests/test_day1_seed_fixture.py::_seed_agent`` (kept here too since the
    pipeline driver needs an agent row to exist before seeding the job)."""
    agent = CaliberAgentConfig(
        agent_id=agent_id,
        # Derived from agent_id (not a fixed literal) so two independent
        # pipeline runs in the same process/DB -- e.g. the reproducibility
        # test, which seeds two differently-named agents to prove determinism
        # isn't an artifact of shared state -- don't collide on the column's
        # unique constraint.
        experiment_id=f"exp-{agent_id}",
        name="Intake Classifier (Day 1 seed)",
        owner=owner,
        artifact_types=["prompt"],
        eval_thresholds={"min_aggregate_score": 0.80, "max_regression_delta": 0.05},
        optimizer_config={},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    return agent


def seed_eval_dataset(
    session: Session,
    *,
    owner: str,
    dataset_name: str | None = None,
) -> str:
    """Seed the Day 1 fixture's real 12-case eval set as a ``CaliberEvalDataset``.

    This is what makes the DSPy candidate stage's trainset (loaded from the DB
    by ``orchestrator/candidate.py::_load_trainset``) and the eval stage's
    scored dataset (``orchestrator/eval_stage.py``) the *same* genuine
    fixture data -- not a trivial 2-3 row stand-in. Returns the new
    ``dataset_id``.

    ``CaliberEvalDataset.name`` carries a global unique constraint, so
    ``dataset_name`` defaults to a fresh, id-suffixed name rather than the
    literal ``"default"`` -- two independent pipeline runs sharing one DB
    (e.g. the reproducibility test, which seeds two agents) would otherwise
    collide seeding their own dataset. Callers that want a specific name
    (matching an agent's ``eval_thresholds["eval_dataset_id"]`` override, or
    the module-level ``DEFAULT_EVAL_DATASET_NAME`` for a single-agent DB) pass
    it explicitly. Either way, :func:`run_diagnose_candidate_evaluate` pins
    the returned ``dataset_id`` directly onto the agent's
    ``eval_thresholds["eval_dataset_id"]`` so name collisions never affect
    which dataset a given agent actually resolves to.
    """
    dataset_id = new_eval_dataset_id()
    dataset = CaliberEvalDataset(
        dataset_id=dataset_id,
        name=dataset_name or f"{DEFAULT_EVAL_DATASET_NAME}-{dataset_id}",
        owner=owner,
        visibility="user",
        version=1,
    )
    session.add(dataset)
    session.flush()

    for row in eval_examples_as_dataset_rows():
        session.add(
            CaliberEvalDatasetExample(
                example_id=new_eval_example_id(),
                dataset_id=dataset_id,
                dataset_version=1,
                input=row["input"],
                expected=row["expected"],
                weight=row["weight"],
                tags=row["tags"],
            )
        )
    session.commit()
    return dataset_id


def build_dummy_dspy_lm(eval_examples: list[EvalExample]) -> DummyLM:
    """Build a deterministic stand-in LM that "knows" the fixture's answers.

    ``dspy.utils.dummies.DummyLM`` accepts a ``{substring_of_prompt: answer}``
    mapping (see its ``forward()``): the first key found as a substring of the
    rendered prompt wins. Keying on each example's ``ticket_text`` and
    answering with the exact JSON-serialized ``expectations`` (matching
    ``caliber.llm.dspy_optimizer._example_text``'s dict-fallback formatting
    byte-for-byte) means DSPy's real ``_demo_metric`` containment check
    genuinely passes for these examples -- BootstrapFewShot bootstraps real
    demonstrations from the real fixture data, not a placeholder answer that
    never matches anything.
    """
    from dspy.utils.dummies import DummyLM

    answers: dict[str, dict[str, str]] = {}
    for example in eval_examples:
        ticket_text = example.inputs.get("ticket_text")
        if not isinstance(ticket_text, str) or not ticket_text:
            continue
        expected_text = json.dumps(example.expectations, ensure_ascii=False, sort_keys=True)
        answers[ticket_text] = {"answer": expected_text}
    return DummyLM(answers)


@contextmanager
def deterministic_dspy_lm(eval_examples: list[EvalExample]) -> Iterator[None]:
    """Patch ``dspy.LM`` so the real DSPy teleprompter runs offline.

    Follows ``tests/test_dspy_optimizer.py::test_run_bootstrap_fewshot_end_to_end_with_dummy_lm``'s
    own precedent exactly: patch the ``dspy.LM`` constructor (called inside
    ``caliber.llm.dspy_optimizer._prepare_program``) to return a
    pre-configured ``DummyLM`` instead of constructing a real
    ``litellm``-backed OpenAI client. Everything else in the DSPy bridge --
    the ``BootstrapFewShot`` teleprompter, the ``dspy.Predict`` program, the
    deterministic ``_demo_metric`` -- runs unmodified.
    """
    import dspy

    lm = build_dummy_dspy_lm(eval_examples)
    with patch.object(dspy, "LM", lambda *args, **kwargs: lm):
        yield


def _dspy_candidate_callable(context: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
    """``FakeLLMProvider.candidate_callable`` that runs the real DSPy bridge."""
    from caliber.llm.dspy_optimizer import run_bootstrap_fewshot

    return run_bootstrap_fewshot(
        context=context,
        model=DSPY_MODEL_ID,
        max_bootstrapped_demos=DSPY_MAX_BOOTSTRAPPED_DEMOS,
        max_labeled_demos=DSPY_MAX_LABELED_DEMOS,
    )


def build_pipeline_llm_provider() -> LLMProvider:
    """The hybrid provider this pipeline run drives diagnosis + candidate with.

    ``diagnose()``: a fixed, canned :class:`~caliber.llm.provider.Diagnosis`
    matching the flagged trace's real story (a compound ticket the prompt
    collapsed into a single intent) -- offline and identical on every run.
    ``generate_candidate()``: delegates to :func:`_dspy_candidate_callable`,
    the real DSPy path -- see the module docstring for why this split
    resolves the plan's "real DSPy" vs. "deterministic/offline" tension.
    """
    return FakeLLMProvider(
        diagnose_response=Diagnosis(
            root_cause=(
                "The intake-classifier prompt has no worked examples of "
                "compound/ambiguous tickets, so a multi-issue ticket "
                "('I was double charged AND the page keeps crashing...') gets "
                "collapsed into a single intent and needs_review is left false."
            ),
            affected_components=["prompt"],
            confidence=0.62,
            alternatives=[
                "Add few-shot examples of ambiguous/compound tickets",
                "Tighten the needs_review trigger language",
            ],
        ),
        diagnose_usage=LLMUsage(input_tokens=612, output_tokens=138, cost_usd=0.0021),
        candidate_callable=_dspy_candidate_callable,
    )


def run_diagnose_candidate_evaluate(
    session: Session,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    config: CaliberConfig | None = None,
) -> PipelineRunResult:
    """Drive the Day 1 seed fixture through evidence -> diagnosis -> candidate
    -> eval and return the scored candidate.

    Seeds a fresh agent, eval dataset, verification item, and refinement job
    into ``session`` (any pre-existing rows for ``agent_id`` will collide --
    callers should use a clean DB per run, exactly as the standalone script
    and the pytest test both do). Returns once the job has reached a terminal
    eval-stage outcome (``candidate_ready`` on a passing gate, ``rejected`` on
    a failing one) -- reproducibly: same fixture in, same result out, every
    run, with no live network call.
    """
    agent = seed_agent(session, agent_id=agent_id)
    dataset_id = seed_eval_dataset(session, owner=agent.owner)
    # Pin the seeded dataset explicitly rather than relying on the "default"
    # name fallback -- see seed_eval_dataset's docstring for why (name carries
    # a global unique constraint, so two agents in one DB can't both use it).
    agent.eval_thresholds = {**agent.eval_thresholds, "eval_dataset_id": dataset_id}
    session.commit()

    job_id = seed_flagged_job(session, agent_id=agent_id)
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:  # pragma: no cover - defensive; seed_flagged_job just created it
        raise LookupError(f"refinement job {job_id!r} not found immediately after seeding")

    # The triage stage isn't under test here (Day 1's own fixture test jumps
    # the same way) -- park the job straight at running/evidence.
    job.status = "running"
    job.current_stage = "evidence"
    session.commit()

    run_evidence(session, job_id, trace_client=build_fake_trace_client())

    llm = build_pipeline_llm_provider()
    artifact_store = FakeArtifactStore({agent_id: load_prompt_template()})
    eval_examples = load_eval_examples()

    with deterministic_dspy_lm(eval_examples):
        run_diagnosis(session, job_id, llm)
        run_candidate(session, job_id, llm, artifact_store, config=config)

    eval_provider = FakeEvalProvider(n_examples=len(eval_examples))
    run_eval(session, job_id, eval_provider, artifact_store=artifact_store, config=config)

    job = session.get(CaliberRefinementJob, job_id)
    if job is None:  # pragma: no cover - defensive; the row can't vanish mid-run
        raise LookupError(f"refinement job {job_id!r} not found after eval")

    return PipelineRunResult(
        job_id=job.job_id,
        status=job.status,
        optimizer_type=job.optimizer_type,
        diagnosis=dict(job.diagnosis or {}),
        candidate=dict(job.candidate or {}),
        eval_results=dict(job.eval_results or {}),
    )
