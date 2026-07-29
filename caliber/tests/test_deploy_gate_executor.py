"""The deploy gate must be graded by the *configured* model, and say which one.

Independent validation (L1) found that the normal deployment route always graded
with the deterministic fake:

    routes/workflow_deployments.py calls promote(..., config=config) without an
    executor, while promoter.promote() replaced a missing executor with
    build_executor(None) — not build_executor(config, manifest=manifest). A real
    provider in application configuration therefore never reached the route.

The consequence was not a crash but a *false claim*: a stored production
``gate_result`` looked identical whether a real model had answered or a scripted
double had. Three properties close it, and each is asserted separately here:

1. ``promote()`` builds its executor from ``config`` and the version's manifest, so
   configuration reaches the gate;
2. every gate verdict records the executor identity that produced it, so the
   evidence is self-describing; and
3. a production-class promotion graded deterministically is **refused** by default,
   because presenting a scripted verdict as release evidence is the defect itself.

A misconfigured provider fails closed with an actionable message rather than
silently downgrading to the fake — which would restore the original defect while
appearing to work.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.workflows import promoter
from caliber.workflows.promoter import (
    DeployError,
    build_executor,
    describe_executor,
    promote,
    requires_graded_executor,
)
from tests.workflow_helpers import make_manifest

# ---------------------------------------------------------------------------
# Executor identity
# ---------------------------------------------------------------------------


def test_the_fake_executor_is_reported_as_deterministic() -> None:
    identity = describe_executor(build_executor(None), None)
    assert identity["deterministic"] is True
    assert identity["provider"] == "fake"
    assert identity["executor"] == "FakeWorkflowExecutor"
    # No model is named, because none answered. Reporting the configured model here
    # would be the exact overclaim this record exists to prevent.
    assert identity["model"] is None


def test_a_non_deterministic_executor_reports_provider_and_model() -> None:
    """Identity is derived from the executor *class*, not from configuration alone.

    A stand-in class is used rather than a real provider client so the assertion
    needs no API key: the property under test is "what actually ran is what gets
    recorded", and that must not depend on network access.
    """

    class OpenAIChatWorkflowExecutorStub:  # name does not matter, class identity does
        pass

    identity = describe_executor(
        OpenAIChatWorkflowExecutorStub(),  # type: ignore[arg-type]
        CaliberConfig(llm_provider="openai", llm_diagnosis_model="gpt-4.1-mini"),
    )
    assert identity["deterministic"] is False
    assert identity["provider"] == "openai"
    assert identity["model"] == "gpt-4.1-mini"


def test_config_cannot_relabel_the_fake_as_a_real_provider() -> None:
    """The dangerous direction: config says ``openai``, the fake actually ran.

    Trusting config here would let a deployment that failed over to the fake publish
    gate evidence naming a model that never answered.
    """
    identity = describe_executor(build_executor(None), CaliberConfig(llm_provider="openai"))
    assert identity["deterministic"] is True
    assert identity["provider"] == "fake"
    assert identity["model"] is None


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def test_graded_executor_is_required_for_production_classes_by_default() -> None:
    config = CaliberConfig()
    assert requires_graded_executor("prod", config) is True
    # Keyed to the environment *class*, so every spelling of production is covered —
    # the same defect that made alias-literal matching wrong for MCP isolation.
    assert requires_graded_executor("production", config) is True
    assert requires_graded_executor("prod-eu", config) is True
    assert requires_graded_executor("dev", config) is False
    assert requires_graded_executor("staging", config) is False


def test_the_requirement_can_be_disabled_for_deliberately_fake_installs() -> None:
    config = CaliberConfig(release_require_graded_executor_for_environment_classes="")
    assert requires_graded_executor("prod", config) is False


def test_a_missing_config_falls_back_to_requiring_it() -> None:
    """``None`` config must not mean "no policy" — it must mean the shipped default."""
    assert requires_graded_executor("prod", None) is True


# ---------------------------------------------------------------------------
# End-to-end through promote()
# ---------------------------------------------------------------------------


def _seed_gated_version(session: Session, *, alias: str = "prod") -> CaliberWorkflowVersion:
    dataset = CaliberEvalDataset(
        dataset_id="eval-exec",
        name="exec_data",
        owner="@test",
        status="active",
        version=1,
    )
    session.add(dataset)
    session.add(
        CaliberEvalDatasetExample(
            example_id="exec-0",
            dataset_id=dataset.dataset_id,
            dataset_version=1,
            input={"input": "question"},
            expected={"expected": "processed"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    session.add(CaliberWorkflow(workflow_id="wf-exec", name="Exec", owner="@test"))
    version = CaliberWorkflowVersion(
        version_id="wfv-exec",
        workflow_id="wf-exec",
        version_number=1,
        status="published",
        manifest=make_manifest(
            "wf-exec",
            artifacts={"eval_datasets": {"exec_data": {"dataset_name": "exec_data"}}},
            deploy_gates={
                "quality": {
                    "type": "deploy_gate",
                    "dataset_ref": "exec_data",
                    "required_for_aliases": [alias],
                    "thresholds": {"min_completion_rate": 1.0},
                }
            },
        ),
        manifest_hash="hash-exec",
    )
    session.add(version)
    session.flush()
    return version


def test_production_refuses_a_gate_graded_by_the_deterministic_fake(
    db_session: Session,
) -> None:
    """The L1 closure, end to end: no executor is passed, so ``promote()`` builds one
    from config. With ``llm_provider=fake`` that is the deterministic executor, and a
    production promotion must refuse it rather than record its verdict as evidence."""
    version = _seed_gated_version(db_session)

    with pytest.raises(DeployError) as excinfo:
        promote(
            db_session,
            "wf-exec",
            "prod",
            version,
            actor="@ops",
            config=CaliberConfig(llm_provider="fake"),
        )
    message = str(excinfo.value)
    assert "deterministic" in message
    assert "FakeWorkflowExecutor" in message
    # The message must name the escape hatch, or the operator's only option is to
    # guess. A fail-closed control that cannot be configured gets bypassed elsewhere.
    assert "CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES" in message


def test_a_non_production_alias_still_deploys_with_deterministic_grading(
    db_session: Session,
) -> None:
    """Development must stay frictionless: the policy targets production evidence,
    not every promotion."""
    version = _seed_gated_version(db_session, alias="dev")

    result = promote(
        db_session,
        "wf-exec",
        "dev",
        version,
        actor="@ops",
        config=CaliberConfig(llm_provider="fake"),
    )
    assert result.rotated is True
    assert result.gate_result.passed is True
    # The evidence still names what graded it, even where the policy does not apply.
    assert result.gate_result.executor is not None
    assert result.gate_result.executor["deterministic"] is True


def test_the_gate_verdict_records_its_executor_in_the_stored_dict(
    db_session: Session,
) -> None:
    """``to_dict()`` is what reaches the audit row and the promotion record, so the
    identity has to survive serialization — not just live on the dataclass."""
    version = _seed_gated_version(db_session, alias="dev")
    result = promote(
        db_session,
        "wf-exec",
        "dev",
        version,
        actor="@ops",
        config=CaliberConfig(llm_provider="fake"),
    )
    stored = result.gate_result.to_dict()
    assert stored["executor"]["executor"] == "FakeWorkflowExecutor"
    assert stored["executor"]["deterministic"] is True


def test_promote_builds_the_executor_from_config_and_the_manifest(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precise regression: ``build_executor`` must receive the real config and the
    parsed manifest. ``build_executor(None)`` — the previous call — could never select
    a configured provider, and without the manifest a workflow-scoped OpenAI runtime
    override would be ignored.
    """
    version = _seed_gated_version(db_session, alias="dev")
    seen: dict[str, object] = {}
    real_build = promoter.build_executor

    def _spy(config, *, manifest=None, ir=None):
        seen["config"] = config
        seen["manifest"] = manifest
        return real_build(None)

    monkeypatch.setattr(promoter, "build_executor", _spy)
    config = CaliberConfig(llm_provider="fake")
    promote(db_session, "wf-exec", "dev", version, actor="@ops", config=config)

    assert seen["config"] is config, "promote() must pass the application config"
    assert seen["manifest"] is not None, "promote() must pass the parsed manifest"
    assert getattr(seen["manifest"], "workflow_id", None) == "wf-exec"


def test_an_explicitly_passed_executor_is_still_honoured(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers that supply an executor (eval harnesses, candidate rotation) must keep
    control; the config-derived build is only the default."""
    version = _seed_gated_version(db_session, alias="dev")

    def _never(*args, **kwargs):  # pragma: no cover - asserted by not being called
        raise AssertionError("build_executor must not be called when one was passed")

    monkeypatch.setattr(promoter, "build_executor", _never)
    result = promote(
        db_session,
        "wf-exec",
        "dev",
        version,
        actor="@ops",
        executor=real_fake_executor(),
        config=CaliberConfig(llm_provider="fake"),
    )
    assert result.rotated is True


def real_fake_executor():
    """The deterministic executor, imported lazily so monkeypatching promoter's
    ``build_executor`` above cannot affect how this test obtains one."""
    from caliber.workflows.runtime import FakeWorkflowExecutor

    return FakeWorkflowExecutor()


def test_a_misconfigured_provider_fails_closed_instead_of_using_the_fake(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_executor`` raises when a provider is selected with no resolvable key.

    Falling back to the fake there would be the worst outcome: the promotion would
    succeed and record evidence, having graded nothing real. It must surface as a
    DeployError the route turns into an actionable 400.
    """
    version = _seed_gated_version(db_session, alias="dev")

    def _raise(*args, **kwargs):
        raise RuntimeError("CALIBER_LLM_PROVIDER=openai requires a secret at 'OPENAI_API_KEY'")

    monkeypatch.setattr(promoter, "build_executor", _raise)

    with pytest.raises(DeployError) as excinfo:
        promote(
            db_session,
            "wf-exec",
            "dev",
            version,
            actor="@ops",
            config=CaliberConfig(llm_provider="openai"),
        )
    assert "cannot grade the deploy gate" in str(excinfo.value)
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_no_gate_means_no_executor_requirement(db_session: Session) -> None:
    """With the quality-gate requirement relaxed and no gate attached there is no
    verdict to misrepresent, so the graded-executor policy must not fire — otherwise
    it would block promotions it has nothing to say about."""
    db_session.add(CaliberWorkflow(workflow_id="wf-plain", name="Plain", owner="@test"))
    version = CaliberWorkflowVersion(
        version_id="wfv-plain",
        workflow_id="wf-plain",
        version_number=1,
        status="published",
        manifest=make_manifest("wf-plain"),
        manifest_hash="hash-plain",
    )
    db_session.add(version)
    db_session.flush()

    result = promote(
        db_session,
        "wf-plain",
        "prod",
        version,
        actor="@ops",
        config=CaliberConfig(
            llm_provider="fake",
            release_require_quality_gate_for_environment_classes="",
        ),
    )
    assert result.rotated is True
    assert result.gate_result.has_gate is False
    assert result.gate_result.executor is None
