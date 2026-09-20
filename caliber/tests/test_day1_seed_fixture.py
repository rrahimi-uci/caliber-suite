"""Proves the two-week alpha plan's Day 1 seed fixture is well-formed.

This is Day 1 of ``docs/two-week-alpha-plan.md``'s Week 1 table: "write the
one fixed seed fixture (trace + prompt + eval set)". Day 1's own acceptance
is just "fixture committed" -- it does not require the full pipeline (a real
LLM diagnosis/candidate/eval run) to work yet, that's Day 2-3. What this
module proves instead:

* the prompt template parses and carries the variables the pipeline expects;
* the eval-set JSONL is well-formed and matches the schema
  ``CaliberEvalDatasetExample`` / DSPy's trainset loader expect;
* the flagged-trace fixture loads as a :class:`TraceSummary` and, loaded
  into a :class:`FakeTraceClient`, wires correctly into the real
  ``orchestrator.evidence.run_evidence`` stage (no live MLflow call);
* seeding the fixture's verification item + refinement job produces a job
  whose optimizer selection takes the explicit-override path to
  ``DSPyBootstrapFewShot`` -- proving the fixture actually exercises the
  DSPy/candidate path the two-week plan names, not the automatic default
  (``MetaPrompt``) ``orchestrator.optimizer_select.select_optimizer`` would
  otherwise pick for this diagnosis.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.orchestrator.evidence import run_evidence
from caliber.orchestrator.optimizer_select import select_optimizer
from caliber.trace_client import TraceSummary
from tests.fixtures.day1_seed_fixture import (
    DEFAULT_AGENT_ID,
    SEED_OPTIMIZER_TYPE,
    build_fake_trace_client,
    eval_examples_as_dataset_rows,
    flagged_trace_id,
    load_eval_examples,
    load_flagged_trace_summary,
    load_prompt_template,
    seed_flagged_job,
)

_ALLOWED_INTENTS = {"billing", "how_to", "bug", "account", "feature_request", "unknown"}
_ALLOWED_PRIORITIES = {"low", "medium", "high", "urgent"}


def _seed_agent(session: Session, agent_id: str = DEFAULT_AGENT_ID) -> CaliberAgentConfig:
    agent = CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id="exp-day1-seed",
        name="Intake Classifier (Day 1 seed)",
        owner="@day1-seed-fixture",
        artifact_types=["prompt"],
        eval_thresholds={"min_aggregate_score": 0.80, "max_regression_delta": 0.05},
        optimizer_config={},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    return agent


class TestPromptTemplate:
    """The reused ``intake-classifier`` prompt loads and is well-formed."""

    def test_loads_and_has_frontmatter(self) -> None:
        text = load_prompt_template()
        assert text.startswith("---\n")
        assert "name: intake-classifier" in text

    def test_has_required_template_variables(self) -> None:
        text = load_prompt_template()
        for variable in ("{{ ticket_text }}", "{{ channel }}", "{{ metadata }}"):
            assert variable in text, f"prompt is missing template variable {variable!r}"

    def test_describes_the_expected_output_contract(self) -> None:
        text = load_prompt_template()
        # The contract the eval set's ``expectations`` and the judge config
        # both assume: intent/priority/confidence/needs_review/reason.
        for field_name in ("intent", "priority", "confidence", "needs_review", "reason"):
            assert f'"{field_name}"' in text


class TestEvalSet:
    """The reused 12-case ``intake-classifier`` eval set parses correctly."""

    def test_parses_all_rows(self) -> None:
        examples = load_eval_examples()
        assert len(examples) == 12
        ids = [example.id for example in examples]
        assert ids == [f"P{n:02d}" for n in range(1, 13)]
        assert len(set(ids)) == len(ids), "duplicate example ids"

    def test_every_row_has_a_ticket_and_channel(self) -> None:
        for example in load_eval_examples():
            assert isinstance(example.inputs.get("ticket_text"), str)
            assert example.inputs["ticket_text"], f"{example.id}: empty ticket_text"
            assert isinstance(example.inputs.get("channel"), str)

    def test_expectations_use_the_documented_enums(self) -> None:
        for example in load_eval_examples():
            intent = example.expectations.get("intent")
            if intent is not None:
                assert intent in _ALLOWED_INTENTS, f"{example.id}: unexpected intent {intent!r}"
            priority = example.expectations.get("priority")
            if priority is not None:
                assert priority in _ALLOWED_PRIORITIES, (
                    f"{example.id}: unexpected priority {priority!r}"
                )
            needs_review = example.expectations.get("needs_review")
            if needs_review is not None:
                assert isinstance(needs_review, bool)

    def test_covers_the_ambiguous_and_injection_cases_the_trace_needs(self) -> None:
        # The flagged-trace fixture's failure mode (missed needs_review on an
        # ambiguous/compound ticket) is only a meaningful DSPy few-shot fix if
        # the eval set actually contains worked examples of that pattern.
        tagged = {example.id: example.tags for example in load_eval_examples()}
        edge_or_negative = [
            example_id
            for example_id, tags in tagged.items()
            if "edge" in tags or "negative" in tags or "prompt_injection" in tags
        ]
        assert len(edge_or_negative) >= 3

    def test_as_dataset_rows_matches_caliber_eval_dataset_example_shape(self) -> None:
        rows = eval_examples_as_dataset_rows()
        assert len(rows) == len(load_eval_examples())
        for row in rows:
            assert set(row) == {"input", "expected", "weight", "tags"}
            assert isinstance(row["input"], dict)
            assert isinstance(row["expected"], dict)
            assert row["weight"] == 1.0
            assert isinstance(row["tags"], list)


class TestFlaggedTrace:
    """The flagged-trace fixture loads offline and wires into the real
    evidence stage through :class:`FakeTraceClient` -- no live MLflow call."""

    def test_loads_as_trace_summary(self) -> None:
        summary = load_flagged_trace_summary()
        assert isinstance(summary, TraceSummary)
        assert summary.status == "OK"
        assert summary.span_count == 2
        assert summary.tool_calls == []
        assert summary.error is None
        assert "needs_review" in summary.response_preview

    def test_flagged_trace_id_is_stable(self) -> None:
        assert flagged_trace_id() == "tr-intake-classifier-day1-seed-0001"

    def test_fake_trace_client_resolves_the_fixture_and_nothing_else(self) -> None:
        client = build_fake_trace_client()
        summary = client.get_trace_summary(flagged_trace_id())
        assert summary == load_flagged_trace_summary()
        assert client.get_trace_summary("some-other-trace-id") is None


class TestSeedFlaggedJob:
    """Seeding the fixture produces a job that reaches the evidence stage and
    pins the DSPy optimizer through the explicit-override path."""

    def test_seed_creates_a_queued_triage_job(self, db_session: Session) -> None:
        _seed_agent(db_session)
        job_id = seed_flagged_job(db_session, agent_id=DEFAULT_AGENT_ID)

        job = db_session.get(CaliberRefinementJob, job_id)
        assert job is not None
        assert job.agent_id == DEFAULT_AGENT_ID
        assert job.status == "queued"
        assert job.current_stage == "triage"
        assert job.artifact_type == "prompt"
        assert job.optimizer_type == SEED_OPTIMIZER_TYPE

        item = db_session.get(CaliberVerificationItem, job.primary_item_id)
        assert item is not None
        assert item.trace_id == flagged_trace_id()
        assert item.status == "verified"

    def test_explicit_override_selects_dspy_where_automatic_selection_would_not(
        self, db_session: Session
    ) -> None:
        """The two-week plan's own non-obvious finding, pinned as a regression
        test: without the seed's explicit ``optimizer_type`` override,
        automatic selection does not reach DSPy for this diagnosis."""
        agent = _seed_agent(db_session)
        job_id = seed_flagged_job(db_session, agent_id=DEFAULT_AGENT_ID)
        job = db_session.get(CaliberRefinementJob, job_id)
        assert job is not None

        # As seeded: the explicit override wins.
        assert select_optimizer(agent, job) == SEED_OPTIMIZER_TYPE

        # Without the override (and with a bland diagnosis matching none of
        # GEPA's/few-shot's own auto-selection criteria), the automatic
        # default is MetaPrompt -- not DSPy. This is exactly the trap Day 1's
        # research flagged: the DSPy path is opt-in, not automatic.
        job.optimizer_type = None
        job.diagnosis = {
            "root_cause": "minor tone mismatch",
            "affected_components": [],
            "confidence": 0.95,
            "alternatives": [],
        }
        assert select_optimizer(agent, job) == "MetaPrompt"

    def test_evidence_stage_wires_the_fake_trace_client(self, db_session: Session) -> None:
        _seed_agent(db_session)
        job_id = seed_flagged_job(db_session, agent_id=DEFAULT_AGENT_ID)

        job = db_session.get(CaliberRefinementJob, job_id)
        assert job is not None
        # The triage stage (not under test here) is what would normally move
        # a queued job to running/evidence; jump straight there since this
        # test is only proving the evidence stage's trace wiring.
        job.status = "running"
        job.current_stage = "evidence"
        db_session.commit()

        run_evidence(db_session, job_id, trace_client=build_fake_trace_client())

        refreshed = db_session.get(CaliberRefinementJob, job_id)
        assert refreshed is not None
        assert refreshed.current_stage == "diagnosis"

        audit_rows = (
            db_session.query(CaliberAuditLog)
            .filter(
                CaliberAuditLog.entity_type == "refinement_job",
                CaliberAuditLog.entity_id == job_id,
                CaliberAuditLog.action == "advance_stage",
            )
            .order_by(CaliberAuditLog.timestamp.desc())
            .all()
        )
        assert audit_rows, "expected an advance_stage audit row from run_evidence"
        evidence = audit_rows[0].details["evidence"]
        assert evidence["has_trace_link"] is True
        trace = evidence["trace"]
        assert trace["status"] == "OK"
        assert trace["span_count"] == 2
        assert trace["tool_calls"] == []
        assert trace["error"] is None
        assert "needs_review" in trace["response_preview"]
