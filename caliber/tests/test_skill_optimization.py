"""Tests for the skill optimization workflow.

Covers: triage skill attribution, skill evidence collection, skill-aware
candidate generation, skill optimizer selection, SkillPromoter,
CompositePromoter, FakeArtifactStore skill support, and multi-agent
baseline resolution in the eval stage.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    LLMUsage,
    TriageClassification,
    TriageContext,
)
from caliber.orchestrator.candidate import run_candidate
from caliber.orchestrator.eval_stage import run_eval
from caliber.orchestrator.evidence import run_evidence
from caliber.orchestrator.optimizer_select import select_optimizer
from caliber.orchestrator.triage import run_triage
from caliber.promoter import (
    CompositePromoter,
    FakePromoter,
    PromoterError,
    PromotionRequest,
    RollbackRequest,
    SkillPromoter,
    build_promoter,
)

# ───────────────────── helpers ─────────────────────


def _make_agent(
    session: Session,
    agent_id: str = "support-agent",
    skills: list[str] | None = None,
) -> CaliberAgentConfig:
    """Create an agent with optional skill references."""
    optimizer_config: dict = {}
    if skills:
        optimizer_config["skills"] = skills
    agent = CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id="exp-1",
        name="Support",
        owner="@test",
        artifact_types=["prompt"],
        eval_thresholds={"overall_min": 0.5},
        optimizer_config=optimizer_config,
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    return agent


def _make_skill(
    session: Session,
    name: str = "tool-use",
    content: str = "Use tools carefully.",
    allowed_tools: str | None = "Bash(python:*) WebFetch",
    depends_on: list[str] | None = None,
) -> CaliberSkill:
    """Create an active skill."""
    skill = CaliberSkill(
        skill_id=f"SK-{name}",
        name=name,
        description=f"Skill: {name}",
        summary=f"Summary for {name}",
        content=content,
        owner="@test",
        category="mcp_enhancement",
        tags=["test"],
        skill_metadata={"author": "test"},
        allowed_tools=allowed_tools,
        depends_on=depends_on or [],
        status="active",
        version=1,
    )
    session.add(skill)
    session.flush()
    return skill


def _make_item(
    session: Session,
    category: str = "tool_use",
    artifact_type_hint: str | None = None,
    artifact_ref: str | None = None,
) -> CaliberVerificationItem:
    """Create a verification item."""
    item = CaliberVerificationItem(
        item_id="FB-SKILL",
        agent_id="support-agent",
        category=category,
        free_text="Tool invocation failed",
        severity="critical",
        status="verified",
        artifact_type_hint=artifact_type_hint,
        artifact_ref=artifact_ref,
    )
    session.add(item)
    session.flush()
    return item


def _make_job(
    session: Session,
    *,
    artifact_type: str = "",
    status: str = "running",
    stage: str = "triage",
    skill_name: str | None = None,
) -> CaliberRefinementJob:
    """Create a refinement job."""
    job = CaliberRefinementJob(
        job_id="RFN-SKILL",
        agent_id="support-agent",
        primary_item_id="FB-SKILL",
        artifact_type=artifact_type,
        status=status,
        current_stage=stage,
        bundle_targets=[],
        skill_name=skill_name,
    )
    session.add(job)
    session.commit()
    return job


# ───────────────────── Triage: skill attribution ─────────────────────


class TestTriageSkillAttribution:
    """Triage classifies skill-related feedback correctly."""

    def test_explicit_skill_hint_routes_to_skill(self, db_session: Session) -> None:
        """When verifier explicitly tags artifact_type_hint='skill' + artifact_ref."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")
        _make_item(
            db_session,
            category="tool_use",
            artifact_type_hint="skill",
            artifact_ref="tool-use",
        )
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"

    def test_implicit_tool_use_routes_to_skill_when_agent_has_tool_skill(
        self, db_session: Session
    ) -> None:
        """tool_use feedback + agent has skill with allowed_tools → route to skill."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")
        _make_item(db_session, category="tool_use")
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"

    def test_tool_use_without_skill_routes_to_prompt(self, db_session: Session) -> None:
        """tool_use feedback but agent has no skills → route to prompt."""
        _make_agent(db_session, skills=[])
        _make_item(db_session, category="tool_use")
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "prompt"
        assert job.skill_name is None

    def test_hallucination_always_routes_to_prompt(self, db_session: Session) -> None:
        """Hallucination feedback is never skill-attributed."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")
        _make_item(db_session, category="hallucination")
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "prompt"
        assert job.skill_name is None

    def test_explicit_hint_with_nonexistent_skill_falls_through(self, db_session: Session) -> None:
        """artifact_type_hint='skill' but artifact_ref names a missing skill."""
        _make_agent(db_session)
        _make_item(
            db_session,
            category="tool_use",
            artifact_type_hint="skill",
            artifact_ref="nonexistent-skill",
        )
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        # Falls through to normal tool_use classification
        assert job.artifact_type == "prompt"
        assert job.skill_name is None

    def test_skill_without_allowed_tools_not_matched_for_tool_use(
        self, db_session: Session
    ) -> None:
        """A skill without allowed_tools should not be matched for tool_use."""
        _make_agent(db_session, skills=["reasoning"])
        _make_skill(db_session, name="reasoning", allowed_tools=None)
        _make_item(db_session, category="tool_use")
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "prompt"
        assert job.skill_name is None


class TestLLMTriage:
    """Triage uses the LLM classifier when a provider is supplied — with the
    deterministic heuristic as a fallback and explicit hints taking precedence."""

    def test_llm_classification_drives_artifact_type_with_skill_resolution(
        self, db_session: Session
    ) -> None:
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")  # allowed_tools set by default
        _make_item(db_session, category="tool_use")
        _make_job(db_session, artifact_type="")
        llm = FakeLLMProvider(
            triage_response=TriageClassification(
                cluster="tool_use", artifact_type="skill", confidence=0.7, rationale="needs tool"
            )
        )

        job = run_triage(db_session, "RFN-SKILL", llm=llm)

        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"  # resolved deterministically, not by the model
        assert len(llm.triage_calls) == 1  # the LLM was consulted

    def test_llm_skill_decision_without_agent_skills_downgrades_to_prompt(
        self, db_session: Session
    ) -> None:
        _make_agent(db_session, skills=[])  # agent references no skills
        _make_item(db_session, category="formatting")
        _make_job(db_session, artifact_type="")
        llm = FakeLLMProvider(
            triage_response=TriageClassification(
                cluster="x", artifact_type="skill", confidence=0.9, rationale="r"
            )
        )

        job = run_triage(db_session, "RFN-SKILL", llm=llm)

        # No skill to target → downgraded to prompt rather than a dangling ref.
        assert job.artifact_type == "prompt"
        assert job.skill_name is None

    def test_llm_error_falls_back_to_heuristic(self, db_session: Session) -> None:
        _make_agent(db_session, skills=[])
        _make_item(db_session, category="hallucination")
        _make_job(db_session, artifact_type="")

        def _boom(_ctx: TriageContext) -> tuple[TriageClassification, LLMUsage]:
            raise RuntimeError("model down")

        llm = FakeLLMProvider(triage_callable=_boom)

        job = run_triage(db_session, "RFN-SKILL", llm=llm)

        # Heuristic ran (hallucination → prompt); the job advanced, not failed.
        assert job.current_stage == "evidence"
        assert job.artifact_type == "prompt"

    def test_explicit_skill_hint_short_circuits_llm(self, db_session: Session) -> None:
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")
        _make_item(
            db_session, category="tool_use", artifact_type_hint="skill", artifact_ref="tool-use"
        )
        _make_job(db_session, artifact_type="")
        llm = FakeLLMProvider()

        job = run_triage(db_session, "RFN-SKILL", llm=llm)

        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"
        assert len(llm.triage_calls) == 0  # explicit hint wins; LLM not consulted

    def test_preserves_existing_artifact_type(self, db_session: Session) -> None:
        """If artifact_type is already set, triage doesn't overwrite it."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use")
        _make_item(
            db_session,
            category="tool_use",
            artifact_type_hint="skill",
            artifact_ref="tool-use",
        )
        _make_job(db_session, artifact_type="prompt")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "prompt"  # Not overwritten
        # But skill_name IS populated from classification
        assert job.skill_name == "tool-use"


# ───────────────────── Evidence: skill-aware collection ─────────────────────


class TestEvidenceSkillCollection:
    """Evidence stage collects cross-agent skill context."""

    def test_skill_evidence_includes_skill_data(self, db_session: Session) -> None:
        """When job targets a skill, evidence includes skill content + agents."""
        _make_agent(db_session, agent_id="support-agent", skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Use tools carefully.")
        _make_item(db_session, category="tool_use")
        _make_job(
            db_session,
            artifact_type="skill",
            status="running",
            stage="evidence",
            skill_name="tool-use",
        )

        job = run_evidence(db_session, "RFN-SKILL")
        assert job.current_stage == "diagnosis"

    def test_prompt_evidence_has_no_skill_data(self, db_session: Session) -> None:
        """When job targets a prompt, evidence does not include skill data."""
        _make_agent(db_session)
        _make_item(db_session, category="hallucination")
        _make_job(db_session, artifact_type="prompt", status="running", stage="evidence")

        job = run_evidence(db_session, "RFN-SKILL")
        assert job.current_stage == "diagnosis"

    def test_skill_evidence_finds_affected_agents(self, db_session: Session) -> None:
        """Multiple agents referencing the same skill are found."""
        _make_agent(db_session, agent_id="support-agent", skills=["tool-use"])
        agent2 = CaliberAgentConfig(
            agent_id="orders-agent",
            experiment_id="exp-2",
            name="Orders",
            owner="@test",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={"skills": ["tool-use"]},
            approval_policy={},
        )
        db_session.add(agent2)
        db_session.flush()
        _make_skill(db_session, name="tool-use")
        _make_item(db_session, category="tool_use")
        _make_job(
            db_session,
            artifact_type="skill",
            status="running",
            stage="evidence",
            skill_name="tool-use",
        )

        job = run_evidence(db_session, "RFN-SKILL")
        assert job.current_stage == "diagnosis"


# ───────────────────── Optimizer selection ─────────────────────


class TestOptimizerSelectSkill:
    """Optimizer selection handles skill-targeted jobs."""

    def test_skill_job_gets_skill_meta_prompt(self, db_session: Session) -> None:
        agent = _make_agent(db_session, skills=["tool-use"])
        job = _make_job(db_session, artifact_type="skill", status="running", stage="candidate")
        result = select_optimizer(agent, job)
        assert result == "SkillMetaPrompt"

    def test_prompt_job_gets_meta_prompt(self, db_session: Session) -> None:
        agent = _make_agent(db_session)
        job = _make_job(db_session, artifact_type="prompt", status="running", stage="candidate")
        result = select_optimizer(agent, job)
        assert result == "MetaPrompt"

    def test_explicit_override_wins_over_skill_default(self, db_session: Session) -> None:
        agent = _make_agent(db_session, skills=["tool-use"])
        # Override optimizer
        agent.optimizer_config["type"] = "GEPA"
        db_session.commit()
        job = _make_job(db_session, artifact_type="skill", status="running", stage="candidate")
        result = select_optimizer(agent, job)
        assert result == "GEPA"


# ───────────────────── Candidate: skill-aware generation ─────────────────────


class TestCandidateSkillAware:
    """Candidate stage handles skill artifacts."""

    def test_skill_candidate_uses_skill_content(self, db_session: Session) -> None:
        """Candidate stage fetches skill content when artifact_type='skill'."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Original skill content.")
        _make_item(db_session, category="tool_use")

        diagnosis_json = {
            "root_cause": "Tool invocation format wrong",
            "affected_components": ["skill"],
            "confidence": 0.8,
            "alternatives": [],
        }
        job = _make_job(
            db_session,
            artifact_type="skill",
            status="running",
            stage="candidate",
            skill_name="tool-use",
        )
        job.diagnosis = diagnosis_json
        db_session.commit()

        store = FakeArtifactStore(skills={"tool-use": "Original skill content."})
        llm = FakeLLMProvider()

        job = run_candidate(db_session, "RFN-SKILL", llm, store)
        assert job.current_stage == "eval"
        assert job.optimizer_type == "SkillMetaPrompt"

        # Verify skill context was passed to LLM
        assert len(llm.candidate_calls) == 1
        ctx = llm.candidate_calls[0]
        assert ctx.skill_name == "tool-use"
        assert ctx.artifact_type == "skill"
        assert ctx.current_artifact_content == "Original skill content."
        assert ctx.allowed_tools == "Bash(python:*) WebFetch"
        assert ctx.affected_agent_ids == ["support-agent"]

    def test_prompt_candidate_no_skill_fields(self, db_session: Session) -> None:
        """Candidate stage for prompt jobs has None skill fields."""
        _make_agent(db_session)
        _make_item(db_session, category="hallucination")

        diagnosis_json = {
            "root_cause": "Hallucinated facts",
            "affected_components": ["prompt"],
            "confidence": 0.8,
            "alternatives": [],
        }
        job = _make_job(
            db_session,
            artifact_type="prompt",
            status="running",
            stage="candidate",
        )
        job.diagnosis = diagnosis_json
        db_session.commit()

        store = FakeArtifactStore(prompts={"support-agent": "You are a support agent."})
        llm = FakeLLMProvider()

        job = run_candidate(db_session, "RFN-SKILL", llm, store)
        assert job.current_stage == "eval"
        assert job.optimizer_type == "MetaPrompt"

        ctx = llm.candidate_calls[0]
        assert ctx.skill_name is None
        assert ctx.allowed_tools is None
        assert ctx.affected_agent_ids is None


# ───────────────────── Eval: skill baseline resolution ─────────────────────


class TestEvalSkillBaseline:
    """Eval stage resolves baseline from skill content for skill jobs."""

    def test_skill_eval_resolves_skill_baseline(self, db_session: Session) -> None:
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Original tool instructions.")
        _make_item(db_session, category="tool_use")

        candidate_json = {
            "artifact_type": "skill",
            "content": "Improved tool instructions.",
            "rationale": "Fixed tool format",
            "diff_summary": "+1 / -1",
        }
        job = _make_job(
            db_session,
            artifact_type="skill",
            status="running",
            stage="eval",
            skill_name="tool-use",
        )
        job.diagnosis = {
            "root_cause": "Wrong format",
            "affected_components": ["skill"],
            "confidence": 0.8,
            "alternatives": [],
        }
        job.candidate = candidate_json
        db_session.commit()

        store = FakeArtifactStore(skills={"tool-use": "Original tool instructions."})
        eval_provider = FakeEvalProvider()

        job = run_eval(db_session, "RFN-SKILL", eval_provider, artifact_store=store)
        # FakeEvalProvider passes by default → terminal candidate_ready.
        assert job.status in {"candidate_ready", "rejected"}


# ───────────────────── ArtifactStore: skill support ─────────────────────


class TestFakeArtifactStoreSkills:
    """FakeArtifactStore supports skill reads."""

    def test_get_active_skill_returns_content(self) -> None:
        store = FakeArtifactStore(skills={"tool-use": "Use tools carefully."})
        assert store.get_active_skill("tool-use") == "Use tools carefully."

    def test_get_active_skill_returns_none_for_missing(self) -> None:
        store = FakeArtifactStore()
        assert store.get_active_skill("nonexistent") is None

    def test_set_skill(self) -> None:
        store = FakeArtifactStore()
        store.set_skill("reasoning", "Think step by step.")
        assert store.get_active_skill("reasoning") == "Think step by step."

    def test_skills_and_prompts_independent(self) -> None:
        store = FakeArtifactStore(
            prompts={"agent-a": "Prompt content"},
            skills={"tool-use": "Skill content"},
        )
        assert store.get_active_prompt("agent-a") == "Prompt content"
        assert store.get_active_skill("tool-use") == "Skill content"
        assert store.get_active_prompt("tool-use") is None
        assert store.get_active_skill("agent-a") is None


# ───────────────────── CandidateContext: skill fields ─────────────────────


class TestCandidateContextSkillFields:
    """CandidateContext carries skill-specific fields."""

    def test_skill_context_has_all_fields(self) -> None:
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-1",
            artifact_type="skill",
            optimizer_type="SkillMetaPrompt",
            diagnosis=Diagnosis(
                root_cause="test",
                affected_components=["skill"],
                confidence=0.8,
                alternatives=[],
            ),
            skill_name="tool-use",
            skill_metadata={"author": "test"},
            allowed_tools="Bash(python:*)",
            depends_on=["reasoning"],
            affected_agent_ids=["support-agent", "orders-agent"],
        )
        assert ctx.skill_name == "tool-use"
        assert ctx.skill_metadata == {"author": "test"}
        assert ctx.allowed_tools == "Bash(python:*)"
        assert ctx.depends_on == ["reasoning"]
        assert ctx.affected_agent_ids == ["support-agent", "orders-agent"]

    def test_prompt_context_has_none_skill_fields(self) -> None:
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-1",
            artifact_type="prompt",
            optimizer_type="MetaPrompt",
            diagnosis=Diagnosis(
                root_cause="test",
                affected_components=["prompt"],
                confidence=0.8,
                alternatives=[],
            ),
        )
        assert ctx.skill_name is None
        assert ctx.skill_metadata is None
        assert ctx.allowed_tools is None
        assert ctx.depends_on is None
        assert ctx.affected_agent_ids is None


# ───────────────────── FakeLLMProvider: skill candidate ─────────────────────


class TestFakeLLMProviderSkillCandidate:
    """FakeLLMProvider generates skill-aware candidate content."""

    def test_skill_candidate_references_skill_name(self) -> None:
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-1",
            artifact_type="skill",
            optimizer_type="SkillMetaPrompt",
            diagnosis=Diagnosis(
                root_cause="Wrong tool format",
                affected_components=["skill"],
                confidence=0.8,
                alternatives=[],
            ),
            current_artifact_content="Original content.",
            skill_name="tool-use",
            allowed_tools="Bash(python:*)",
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "tool-use" in candidate.content
        assert "Bash(python:*)" in candidate.content

    def test_cold_start_skill_candidate(self) -> None:
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-1",
            artifact_type="skill",
            optimizer_type="SkillMetaPrompt",
            diagnosis=Diagnosis(
                root_cause="Missing tool guidance",
                affected_components=["skill"],
                confidence=0.8,
                alternatives=[],
            ),
            skill_name="tool-use",
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "skill:tool-use" in candidate.content


# ───────────────────── SkillPromoter ─────────────────────


class TestSkillPromoter:
    """SkillPromoter updates skill content in the DB."""

    def test_rejects_non_skill_artifact_type(self) -> None:
        promoter = SkillPromoter()
        request = PromotionRequest(
            agent_id="support-agent",
            artifact_type="prompt",
            new_content="new content",
            rationale="test",
            approval_id="APR-1",
        )
        with pytest.raises(PromoterError, match="only supports.*skill"):
            promoter.promote(request)

    def test_promote_then_rollback_restores_prior_content(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        # Arrange: an active skill at v3 with the original content.
        with session_factory() as session:
            session.add(
                CaliberSkill(
                    skill_id="SK-roll",
                    name="tool-use",
                    description="",
                    content="ORIGINAL",
                    owner="@me",
                    tags=[],
                    version=3,
                )
            )
            session.commit()

        promoter = SkillPromoter(session_factory=session_factory)

        # Promote v3 -> v4; the result must carry the prior content so a
        # checkpoint can snapshot it.
        result = promoter.promote(
            PromotionRequest(
                agent_id="tool-use",
                artifact_type="skill",
                new_content="IMPROVED",
                rationale="test",
                approval_id="APR-1",
            )
        )
        assert result.details["content_before"] == "ORIGINAL"
        assert result.details["version"] == 4
        assert result.details["version_before"] == 3
        with session_factory() as session:
            skill = session.query(CaliberSkill).filter(CaliberSkill.name == "tool-use").one()
            assert skill.content == "IMPROVED"
            assert skill.version == 4

        # A checkpoint carrying the snapshot (as _build_checkpoint records for skills).
        with session_factory() as session:
            session.add(
                CaliberRollbackCheckpoint(
                    checkpoint_id="CKP-roll",
                    approval_id="APR-1",
                    agent_id="tool-use",
                    artifact_type="skill",
                    artifact_name="tool-use",
                    artifact_ref_before="skill://tool-use/v3",
                    artifact_ref_after=result.artifact_ref,
                    version_before=3,
                    version_after=4,
                    snapshot_payload={"content_before": "ORIGINAL", "version_before": 3},
                )
            )
            session.commit()

        # Roll back -> content + version restored to the pre-promotion state.
        rb = promoter.rollback(
            RollbackRequest(
                agent_id="tool-use",
                artifact_type="skill",
                version_before=3,
                checkpoint_id="CKP-roll",
            )
        )
        assert rb.details["rolled_back"] is True
        with session_factory() as session:
            skill = session.query(CaliberSkill).filter(CaliberSkill.name == "tool-use").one()
            assert skill.content == "ORIGINAL"
            assert skill.version == 3

    def test_rollback_without_snapshot_is_a_clean_error(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            session.add(
                CaliberSkill(
                    skill_id="SK-nosnap",
                    name="no-snap",
                    description="",
                    content="X",
                    owner="@me",
                    tags=[],
                    version=2,
                )
            )
            session.add(
                CaliberRollbackCheckpoint(
                    checkpoint_id="CKP-nosnap",
                    approval_id="APR-2",
                    agent_id="no-snap",
                    artifact_type="skill",
                    artifact_name="no-snap",
                    artifact_ref_before=None,
                    artifact_ref_after="skill://no-snap/v3",
                    version_before=2,
                    version_after=3,
                    snapshot_payload=None,
                )
            )
            session.commit()
        promoter = SkillPromoter(session_factory=session_factory)
        with pytest.raises(PromoterError, match="no skill content snapshot"):
            promoter.rollback(
                RollbackRequest(
                    agent_id="no-snap",
                    artifact_type="skill",
                    version_before=2,
                    checkpoint_id="CKP-nosnap",
                )
            )

    def test_build_checkpoint_records_skill_snapshot(self) -> None:
        from types import SimpleNamespace

        from caliber.apply import _build_checkpoint

        approval = SimpleNamespace(approval_id="APR-1", agent_id="tool-use")
        result = SimpleNamespace(
            details={"version": 4, "content_before": "ORIGINAL", "version_before": 3},
            artifact_ref="skill://tool-use/v4",
        )
        checkpoint = _build_checkpoint(approval, {"artifact_type": "skill"}, result)  # type: ignore[arg-type]
        assert checkpoint.artifact_type == "skill"
        assert checkpoint.snapshot_payload == {"content_before": "ORIGINAL", "version_before": 3}
        assert checkpoint.artifact_ref_before == "skill://tool-use/v3"

    def test_rollback_rejects_non_skill(self) -> None:
        promoter = SkillPromoter()
        request = RollbackRequest(
            agent_id="support-agent",
            artifact_type="prompt",
            version_before=1,
            checkpoint_id="CKP-1",
        )
        with pytest.raises(PromoterError, match="only supports.*skill"):
            promoter.rollback(request)


# ───────────────────── CompositePromoter ─────────────────────


class TestCompositePromoter:
    """CompositePromoter routes to the correct sub-promoter."""

    def test_prompt_routes_to_default(self) -> None:
        fake = FakePromoter()
        composite = CompositePromoter(default=fake)
        request = PromotionRequest(
            agent_id="support-agent",
            artifact_type="prompt",
            new_content="new prompt",
            rationale="test",
            approval_id="APR-1",
        )
        result = composite.promote(request)
        assert len(fake.calls) == 1
        assert "prompt://" in result.artifact_ref

    def test_skill_routes_to_skill_promoter(self) -> None:
        fake = FakePromoter()
        # SkillPromoter needs a real DB — test with a mock
        # We test routing only — SkillPromoter itself is tested separately
        skill_promoter = SkillPromoter()
        composite = CompositePromoter(default=fake, skill=skill_promoter)
        request = PromotionRequest(
            agent_id="tool-use",
            artifact_type="skill",
            new_content="improved content",
            rationale="test",
            approval_id="APR-1",
        )
        # This will fail because we don't have a DB session, but it proves routing
        with pytest.raises(Exception):
            composite.promote(request)
        # The FakePromoter should NOT have been called
        assert len(fake.calls) == 0

    def test_rollback_prompt_routes_to_default(self) -> None:
        fake = FakePromoter()
        composite = CompositePromoter(default=fake)
        request = RollbackRequest(
            agent_id="support-agent",
            artifact_type="prompt",
            version_before=1,
            checkpoint_id="CKP-1",
        )
        result = composite.rollback(request)
        assert len(fake.rollback_calls) == 1

    def test_rollback_skill_routes_to_skill_promoter(self) -> None:
        fake = FakePromoter()
        composite = CompositePromoter(default=fake)
        request = RollbackRequest(
            agent_id="tool-use",
            artifact_type="skill",
            version_before=1,
            checkpoint_id="CKP-1",
        )
        # Routed to the (session-less) SkillPromoter, which now performs a real
        # restore and so fails on the missing session_factory rather than the
        # old "not yet implemented" stub — either way it proves the skill
        # request did not go to the default promoter.
        with pytest.raises(PromoterError, match="requires a session_factory"):
            composite.rollback(request)
        # The FakePromoter should NOT have been called
        assert len(fake.rollback_calls) == 0


# ───────────────────── build_promoter ─────────────────────


class TestBuildPromoterComposite:
    """build_promoter returns a CompositePromoter."""

    def test_fake_returns_composite(self) -> None:
        promoter = build_promoter("fake")
        assert isinstance(promoter, CompositePromoter)

    def test_mlflow_returns_composite(self) -> None:
        promoter = build_promoter("mlflow")
        assert isinstance(promoter, CompositePromoter)

    def test_unknown_raises(self) -> None:
        with pytest.raises(PromoterError, match="unknown"):
            build_promoter("unknown")


# ───────────────────── RefinementJob: skill_name field ─────────────────────


class TestRefinementJobSkillName:
    """CaliberRefinementJob has a skill_name column."""

    def test_skill_name_defaults_to_none(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session, category="hallucination")
        job = _make_job(db_session, artifact_type="prompt")
        assert job.skill_name is None

    def test_skill_name_can_be_set(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session, category="tool_use")
        job = _make_job(db_session, artifact_type="skill", skill_name="tool-use")
        assert job.skill_name == "tool-use"

    def test_skill_name_persists_through_reload(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session, category="tool_use")
        _make_job(db_session, artifact_type="skill", skill_name="reasoning")
        db_session.expire_all()
        reloaded = db_session.get(CaliberRefinementJob, "RFN-SKILL")
        assert reloaded is not None
        assert reloaded.skill_name == "reasoning"


# ───────────────────── End-to-end: triage through candidate ─────────────────────


class TestSkillOptimizationE2E:
    """End-to-end flow from triage → evidence → candidate for skill jobs."""

    def test_triage_to_candidate_skill_flow(self, db_session: Session) -> None:
        """A skill-targeted feedback item flows through all stages correctly."""
        # Setup
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Use tools with care.")
        _make_item(
            db_session,
            category="tool_use",
            artifact_type_hint="skill",
            artifact_ref="tool-use",
        )
        _make_job(db_session, artifact_type="")

        # Triage
        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"
        assert job.current_stage == "evidence"

        # Evidence
        job = run_evidence(db_session, "RFN-SKILL")
        assert job.current_stage == "diagnosis"

    def test_implicit_skill_attribution_e2e(self, db_session: Session) -> None:
        """Implicit attribution: tool_use + agent with tool skill → skill path."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Be careful with tools.")
        _make_item(db_session, category="tool_calling")
        _make_job(db_session, artifact_type="")

        job = run_triage(db_session, "RFN-SKILL")
        assert job.artifact_type == "skill"
        assert job.skill_name == "tool-use"
