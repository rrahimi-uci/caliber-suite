"""Days 2-3 of ``docs/two-week-alpha-plan.md``: diagnose -> candidate -> evaluate.

This is the CI-enforced proof behind the plan's own Day 2-3 acceptance
criterion: "Running one script against a clean checkout produces a scored
candidate, reproducibly." The standalone demo script
(``scripts/two_week_alpha_day2_run.py``) and this test both call
``tests.fixtures.day2_day3_pipeline.run_diagnose_candidate_evaluate`` -- the
same pipeline-driving logic -- so this test is what actually guards that
claim on every future change, not just at the moment the script was written.

See ``tests/fixtures/day2_day3_pipeline.py``'s module docstring for how this
resolves "prove the real DSPy path runs" against "stay deterministic and
offline": diagnosis is a canned fake, candidate generation drives the real
``caliber.llm.dspy_optimizer.run_bootstrap_fewshot`` bridge (real
``BootstrapFewShot`` teleprompter, real fixture eval-set trainset) under a
deterministic ``dspy.utils.dummies.DummyLM``, and eval scores against the
fixture's real, seeded 12-case dataset.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

dspy = pytest.importorskip("dspy")

from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from tests.fixtures.day1_seed_fixture import SEED_OPTIMIZER_TYPE, load_eval_examples
from tests.fixtures.day2_day3_pipeline import (
    PipelineRunResult,
    build_dummy_dspy_lm,
    run_diagnose_candidate_evaluate,
    seed_agent,
    seed_eval_dataset,
)


class TestSeedEvalDataset:
    """The real 12-case eval set is seeded byte-for-byte as a DB dataset."""

    def test_seeds_all_twelve_examples(self, db_session: Session) -> None:
        agent = seed_agent(db_session)
        dataset_id = seed_eval_dataset(db_session, owner=agent.owner)

        dataset = db_session.get(CaliberEvalDataset, dataset_id)
        assert dataset is not None
        assert dataset.name.startswith("default-")
        assert dataset.owner == agent.owner

        examples = (
            db_session.query(CaliberEvalDatasetExample)
            .filter(CaliberEvalDatasetExample.dataset_id == dataset_id)
            .all()
        )
        assert len(examples) == 12
        inputs = {json.dumps(e.input, sort_keys=True) for e in examples}
        fixture_inputs = {
            json.dumps(example.inputs, sort_keys=True) for example in load_eval_examples()
        }
        assert inputs == fixture_inputs


class TestDummyLM:
    """The deterministic stand-in LM answers with the fixture's real expected values."""

    def test_answers_match_fixture_expectations(self) -> None:
        examples = load_eval_examples()
        lm = build_dummy_dspy_lm(examples)
        assert isinstance(lm.answers, dict)
        assert len(lm.answers) == len(examples)
        for example in examples:
            answer = lm.answers[example.inputs["ticket_text"]]
            assert json.loads(answer["answer"]) == example.expectations


class TestPipelineRun:
    """The full evidence -> diagnosis -> candidate -> eval run, end to end."""

    def test_produces_a_scored_candidate_ready_for_apply(self, db_session: Session) -> None:
        result = run_diagnose_candidate_evaluate(db_session)

        assert isinstance(result, PipelineRunResult)
        assert result.job_id
        assert result.optimizer_type == SEED_OPTIMIZER_TYPE
        # The eval gate (min_aggregate_score=0.80) passes against the fake
        # eval provider's default candidate score -> the job lands at the
        # terminal candidate_ready state Day 4-5's Apply work depends on.
        assert result.status == "candidate_ready"
        assert result.passed_gate is True

        # Diagnosis ran (offline, canned) and was recorded.
        assert result.diagnosis["root_cause"]
        assert result.diagnosis["confidence"] == pytest.approx(0.62)

        # Candidate generation genuinely went through the real DSPy bridge:
        # the rationale/diff_summary the bridge itself writes are present,
        # and at least one real few-shot demo (drawn from the fixture's own
        # eval set, via the deterministic DummyLM) was selected.
        candidate = result.candidate
        assert candidate["artifact_type"] == "prompt"
        assert "DSPy BootstrapFewShot" in candidate["rationale"]
        assert "few-shot demo(s)" in candidate["diff_summary"]
        assert "Few-shot examples (selected by DSPy BootstrapFewShot)" in candidate["content"]
        # The candidate's content still opens with the fixture's own prompt
        # instructions -- BootstrapFewShot only appends demos, it doesn't
        # discard the original prompt.
        assert candidate["content"].startswith("---\n")
        assert "name: intake-classifier" in candidate["content"]

        # Eval scored against the fixture's real 12-case dataset, not a
        # trivial stand-in.
        eval_results = result.eval_results
        assert eval_results["n_examples"] == len(load_eval_examples())
        assert eval_results["gate"]["passed"] is True
        assert eval_results["caliber_tags"]["caliber.gate_passed"] is True

    def test_is_reproducible_across_independent_runs(self, session_factory, engine) -> None:
        """Same fixture in, same result out -- the plan's own literal acceptance
        wording ("reproducibly"). Two fully independent DB + session runs (not
        just two calls sharing state) must produce byte-identical diagnosis,
        candidate content, and eval outcome."""
        with session_factory() as session_one:
            result_one = run_diagnose_candidate_evaluate(session_one, agent_id="repro-agent-1")
        with session_factory() as session_two:
            result_two = run_diagnose_candidate_evaluate(session_two, agent_id="repro-agent-2")

        assert result_one.status == result_two.status == "candidate_ready"
        assert result_one.optimizer_type == result_two.optimizer_type
        assert result_one.diagnosis == result_two.diagnosis
        assert result_one.candidate["content"] == result_two.candidate["content"]
        assert result_one.candidate["rationale"] == result_two.candidate["rationale"]
        assert result_one.candidate["diff_summary"] == result_two.candidate["diff_summary"]
        assert result_one.eval_results["gate"] == result_two.eval_results["gate"]
        assert result_one.eval_results["n_examples"] == result_two.eval_results["n_examples"]

    def test_candidate_context_carried_the_real_fixture_trainset(self, db_session: Session) -> None:
        """Regression guard for the seam this whole slice depends on: the DSPy
        candidate stage's trainset must be the fixture's real 12 examples, not
        an empty/fallback trainset (which would silently produce a
        MetaPrompt-shaped no-op instead of exercising BootstrapFewShot)."""
        from caliber.db.models import CaliberAgentConfig
        from caliber.orchestrator.candidate import _load_trainset

        agent = seed_agent(db_session)
        # Only one agent/dataset in this test's DB, so the "default" name
        # (which ``_load_trainset`` falls back to without an explicit
        # ``eval_dataset_id`` override) is safe to use directly here.
        seed_eval_dataset(db_session, owner=agent.owner, dataset_name="default")
        db_session.commit()

        refreshed_agent = db_session.get(CaliberAgentConfig, agent.agent_id)
        assert refreshed_agent is not None
        trainset = _load_trainset(db_session, refreshed_agent)
        assert len(trainset) == len(load_eval_examples())
