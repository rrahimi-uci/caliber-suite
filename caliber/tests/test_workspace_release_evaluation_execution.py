"""B10.2 execution tests: a release evaluates only its own immutable pins."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from caliber import workspace_release_evaluation_plan as plan_module
from caliber.db import Base
from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.eval.evidence import build_release_gate_evidence
from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import EvalComparison, ScoreSet
from caliber.orchestrator import workspace_release_evaluator as evaluator_module
from caliber.orchestrator.workspace_release_evaluator import (
    ClaimedEvaluation,
    WorkspaceReleaseEvaluator,
)
from caliber.ownership import LeaseLost
from caliber.workspace_release_adapters import (
    SnapshotPin,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_evaluation_plan import (
    WorkspaceReleaseEvaluationPlanError,
    resolve_workspace_release_evaluation_plan,
)
from caliber.workspace_release_service import (
    EVALUATION_FAILED,
    EVALUATION_SUCCEEDED,
    create_workspace_release,
    create_workspace_release_evaluation,
    start_workspace_release_evaluation,
)

HEX = "a" * 64
PROJECT_ID = "PRJ-b10-2"
ENVIRONMENT_ID = "WSE-b10-2"
REVISION_ID = "WSR-b10-2"


@dataclass(frozen=True)
class _ResolvedCandidate:
    content: str


@dataclass(frozen=True)
class _ResolvedDataset:
    dataset_id: str
    version: int
    name: str = "pinned-set"


@dataclass(frozen=True)
class _ResolvedJudge:
    name: str
    instructions: str = "Grade outputs"
    model: str | None = None
    feedback_value_type: str | None = "bool"


class _StaticAdapter:
    def __init__(
        self, resource_type: str, resource_id: str, version_ref: str, resolved: object, digest: str
    ) -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.version_ref = version_ref
        self.resolved = resolved
        self.digest = digest

    def resolve(
        self, _session: object, _workspace: object, declaration: object, _identity: object
    ) -> object:
        assert declaration == {"resource_id": self.resource_id, "version_ref": self.version_ref}
        return self.resolved

    def snapshot(self, _session: object, _resolved: object) -> SnapshotPin:
        return SnapshotPin(
            resource_id=self.resource_id,
            version_ref=self.version_ref,
            content_sha256=self.digest,
            provider_ref=f"test:{self.resource_type}:{self.resource_id}",
            resolution={"strategy": "test"},
        )

    def validate(
        self, _session: object, _pin: object, _environment: object = None
    ) -> dict[str, object]:
        return {"valid": True}


def _factory(tmp_path: Any) -> sessionmaker[Session]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'b10-2.sqlite'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed(
    factory: sessionmaker[Session],
    *,
    evaluation_config: dict[str, object] | None = None,
    candidate_type: str = "prompt",
    derive_digests: bool = True,
) -> tuple[WorkspaceResourceAdapterRegistry, FakeEvalProvider]:
    candidate_digest = _sha("candidate")
    dataset_digest = _sha("dataset")
    judge_digest = _sha("judge")
    with factory() as session:
        session.add(CaliberProject(project_id=PROJECT_ID, name="B10.2", owner="dev"))
        session.add(
            CaliberWorkspaceEnvironment(
                environment_id=ENVIRONMENT_ID,
                project_id=PROJECT_ID,
                name="dev",
                environment_class="development",
                promotion_order=10,
                status="active",
                created_by="dev",
            )
        )
        session.add(
            CaliberWorkspaceRevision(
                revision_id=REVISION_ID,
                project_id=PROJECT_ID,
                revision_number=1,
                source_kind="managed",
                manifest={},
                manifest_sha256=None,
                source_bundle_sha256=None,
                source_attestation="caller_attested",
                revision_sha256=HEX,
                status="ready",
                created_by="dev",
            )
        )
        release_evaluation_config = {
            "role": "dataset",
            "provider": "fake",
            "min_aggregate_score": 0.8,
            **(evaluation_config or {}),
        }
        session.add_all(
            [
                CaliberWorkspaceRevisionResource(
                    resource_pin_id="PIN-candidate",
                    revision_id=REVISION_ID,
                    resource_type=candidate_type,
                    logical_name="candidate",
                    resource_id="prompt-candidate",
                    version_ref="7",
                    content_sha256=candidate_digest,
                    purpose="candidate",
                    resolution={"release_evaluation": {"role": "candidate"}},
                ),
                CaliberWorkspaceRevisionResource(
                    resource_pin_id="PIN-dataset",
                    revision_id=REVISION_ID,
                    resource_type="eval_dataset",
                    logical_name="dataset",
                    resource_id="DS-pinned",
                    version_ref="7",
                    content_sha256=dataset_digest,
                    purpose="evaluation",
                    resolution={"release_evaluation": release_evaluation_config},
                ),
                CaliberWorkspaceRevisionResource(
                    resource_pin_id="PIN-judge",
                    revision_id=REVISION_ID,
                    resource_type="judge",
                    logical_name="judge",
                    resource_id="JDG-pinned",
                    version_ref=judge_digest,
                    content_sha256=judge_digest,
                    purpose="evaluation",
                    resolution={"release_evaluation": {"role": "judge"}},
                ),
            ]
        )
        session.flush()
        release = create_workspace_release(
            session,
            project_id=PROJECT_ID,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            environment_config_sha256=HEX,
            runtime_dependencies_sha256=HEX,
            policy_sha256=HEX,
            request_idempotency_key="release-b10-2",
            requested_by="dev",
        )
        start_workspace_release_evaluation(
            session, release, actor="dev", expected_lock_version=release.lock_version
        )
        evaluation = create_workspace_release_evaluation(
            session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key="fixture",
            evaluation_plan_sha256=HEX,
            input_sha256=HEX,
            requested_by="dev",
        )
        provider = FakeEvalProvider(
            candidate_scores=ScoreSet(overall=0.91, dimensions={"Judge.pinned": 0.91}),
            n_examples=3,
        )
        registry = WorkspaceResourceAdapterRegistry(
            {
                "prompt": _StaticAdapter(
                    "prompt",
                    "prompt-candidate",
                    "7",
                    _ResolvedCandidate("candidate"),
                    candidate_digest,
                ),
                "eval_dataset": _StaticAdapter(
                    "eval_dataset",
                    "DS-pinned",
                    "7",
                    _ResolvedDataset("DS-pinned", 7),
                    dataset_digest,
                ),
                "judge": _StaticAdapter(
                    "judge", "JDG-pinned", judge_digest, _ResolvedJudge("pinned"), judge_digest
                ),
            }
        )
        if derive_digests:
            probe = resolve_workspace_release_evaluation_plan(
                session,
                release=release,
                evaluation=evaluation,
                identity=SimpleNamespace(
                    user_id="dev", scopes=frozenset(), active_project_id=PROJECT_ID
                ),
                adapter_registry=registry,
                provider=provider,
                config=SimpleNamespace(eval_provider="fake"),
                verify_digests=False,
            )
            evaluation.evaluation_plan_sha256 = probe.plan_digest
            evaluation.input_sha256 = probe.input_digest
        session.commit()
    return registry, provider


def test_uses_pinned_digests_and_completes_the_release(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )

    claimed = worker.tick()
    assert claimed is not None
    assert provider.calls[0].eval_dataset_id == "DS-pinned"
    assert provider.calls[0].eval_dataset_version == 7
    assert provider.calls[0].candidate_content == "candidate"
    assert provider.calls[0].scorer_names == ["Judge.pinned"]
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        release = session.get(CaliberWorkspaceRelease, claimed.workspace_release_id)
        assert evaluation is not None and release is not None
        assert evaluation.status == EVALUATION_SUCCEEDED
        assert evaluation.gate_verdict_id is None
        assert evaluation.error_code is None
        assert evaluation.linked_evaluation_run_ids == []
        assert evaluation.evaluation_plan_sha256 != HEX
        assert evaluation.input_sha256 != HEX
        assert evaluation.completed_at is not None
        assert release.status == "approved"
        assert release.evaluation_evidence_sha256 and len(release.evaluation_evidence_sha256) == 64


def test_gate_digest_persisted(tmp_path: Any) -> None:
    """The persisted release digest is reproducible from stored evidence rows."""
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )
    claimed = worker.tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        release = session.get(CaliberWorkspaceRelease, claimed.workspace_release_id)
        assert evaluation is not None and release is not None
        stored = evaluation.gate_evidence
        assert stored is not None
        _, recomputed = build_release_gate_evidence(
            scored_rows=stored["results"]["rows"],
            dataset_id=stored["dataset"]["id"],
            dataset_version=stored["dataset"]["version"],
            plan_sha256=stored["plan_sha256"],
            input_sha256=stored["input_sha256"],
            evaluation_id=stored["evaluation_id"],
            provider=stored["provider"],
            candidate=stored["candidate"],
            baseline=stored["baseline"],
            deltas=stored["deltas"],
            n_examples=stored["results"]["n_examples"],
            min_aggregate_score=stored["gate"]["min_aggregate_score"],
            min_aggregate_delta=stored["gate"]["min_aggregate_delta"],
            result=stored["gate"]["result"],
        )
        assert recomputed == release.evaluation_evidence_sha256
        assert stored["results"]["scorer_denominators"] == {
            "Judge.pinned": {"valid_rows": 3, "weight_sum": 3.0}
        }


def test_digest_mismatch_blocks_without_calling_provider(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    with factory() as session:
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        evaluation.input_sha256 = HEX
        session.commit()
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )

    claimed = worker.tick()
    assert claimed is not None
    assert provider.calls == []
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        release = session.get(CaliberWorkspaceRelease, claimed.workspace_release_id)
        assert evaluation is not None and release is not None
        assert evaluation.status == EVALUATION_FAILED
        assert evaluation.error_code == "evaluation_input_digest_mismatch"
        assert release.status == "blocked"


def test_missing_gate_rows_block_without_fabricated_evidence(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    provider.eval_callable = lambda request: EvalComparison(
        candidate=ScoreSet(overall=0.95, dimensions={"Judge.pinned": 0.95}),
        baseline=None,
        eval_dataset_id=request.eval_dataset_id,
        n_examples=1,
    )
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )

    claimed = worker.tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        release = session.get(CaliberWorkspaceRelease, claimed.workspace_release_id)
        assert evaluation is not None and release is not None
        assert evaluation.status == EVALUATION_FAILED
        assert evaluation.error_code == "evaluation_execution_failed"
        assert evaluation.gate_evidence is None
        assert release.status == "blocked"
        assert release.evaluation_evidence_sha256 is None


def _plan_rows(
    factory: sessionmaker[Session],
) -> tuple[CaliberWorkspaceRelease, CaliberWorkspaceReleaseEvaluation]:
    with factory() as session:
        release = session.execute(select(CaliberWorkspaceRelease)).scalar_one()
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        session.expunge(release)
        session.expunge(evaluation)
        return release, evaluation


def _plan_call(
    factory: sessionmaker[Session],
    registry: WorkspaceResourceAdapterRegistry,
    provider: object,
    *,
    config: object | None = None,
    verify_digests: bool = True,
) -> object:
    release, evaluation = _plan_rows(factory)
    with factory() as session:
        return resolve_workspace_release_evaluation_plan(
            session,
            release=release,
            evaluation=evaluation,
            identity=SimpleNamespace(
                user_id="dev", scopes=frozenset(), active_project_id=PROJECT_ID
            ),
            adapter_registry=registry,
            provider=provider,
            config=config or SimpleNamespace(eval_provider="fake"),
            verify_digests=verify_digests,
        )


def test_plan_helpers_fail_closed_for_malformed_inputs() -> None:
    pin = SimpleNamespace(resolution={}, purpose="evaluation", resource_type="other")
    assert plan_module._role(pin) is None
    assert (
        plan_module._role(
            SimpleNamespace(resolution={}, purpose="candidate", resource_type="prompt")
        )
        == "candidate"
    )
    assert (
        plan_module._role(
            SimpleNamespace(resolution={}, purpose="baseline", resource_type="prompt")
        )
        == "baseline"
    )
    assert (
        plan_module._role(
            SimpleNamespace(resolution={}, purpose="evaluation", resource_type="eval_dataset")
        )
        == "dataset"
    )
    assert (
        plan_module._role(
            SimpleNamespace(resolution={}, purpose="evaluation", resource_type="judge")
        )
        == "judge"
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="must be an object"):
        plan_module._mapping(None, label="test")
    dataset_pin = SimpleNamespace(resolution={}, purpose="evaluation", resource_type="eval_dataset")
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="multiple"):
        plan_module._one([dataset_pin, dataset_pin], role="dataset")
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="missing"):
        plan_module._one([], role="dataset")


def test_plan_helpers_extract_adapter_owned_content_and_validate_numbers() -> None:
    assert (
        plan_module._content(
            SimpleNamespace(template="template"), SimpleNamespace(resource_type="prompt")
        )
        == "template"
    )
    assert (
        plan_module._content(
            SimpleNamespace(manifest={"b": 1}), SimpleNamespace(resource_type="workflow")
        )
        == '{"b":1}'
    )
    assert (
        plan_module._content(
            SimpleNamespace(content="content"), SimpleNamespace(resource_type="skill")
        )
        == "content"
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="content"):
        plan_module._content(SimpleNamespace(), SimpleNamespace(resource_type="prompt"))
    assert plan_module._optional_number({}, "delta") is None
    assert plan_module._number({"score": 1}, "score", 0.0) == 1.0
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="numeric"):
        plan_module._number({"score": True}, "score", 0.0)
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="between"):
        plan_module._number({"score": 2}, "score", 0.0)


def test_plan_rejects_missing_ambiguous_and_unsupported_release_inputs(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    with factory() as session:
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        evaluation.project_id = "PRJ-other"
        session.commit()
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="different projects"):
        _plan_call(factory, registry, provider, verify_digests=False)

    factory = _factory(tmp_path / "revision")
    registry, provider = _seed(factory)
    with factory() as session:
        release = session.execute(select(CaliberWorkspaceRelease)).scalar_one()
        release.revision_id = "WSR-missing"
        session.commit()
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="missing, foreign, or not ready"):
        _plan_call(factory, registry, provider, verify_digests=False)

    factory = _factory(tmp_path / "pins")
    registry, provider = _seed(factory)
    with factory() as session:
        session.query(CaliberWorkspaceRevisionResource).filter_by(
            resource_pin_id="PIN-dataset"
        ).delete()
        session.commit()
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="missing its dataset"):
        _plan_call(factory, registry, provider, verify_digests=False)


def test_plan_rejects_unavailable_changed_and_invalid_adapter_results(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="could not resolve"):
        _plan_call(factory, WorkspaceResourceAdapterRegistry({}), provider, verify_digests=False)

    factory = _factory(tmp_path / "changed")
    registry, provider = _seed(factory)
    changed = _StaticAdapter(
        "prompt", "prompt-candidate", "7", _ResolvedCandidate("candidate"), _sha("other")
    )
    changed_registry = WorkspaceResourceAdapterRegistry(
        {
            "prompt": changed,
            "eval_dataset": registry.require("eval_dataset"),
            "judge": registry.require("judge"),
        }
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="content no longer matches"):
        _plan_call(factory, changed_registry, provider, verify_digests=False)

    factory = _factory(tmp_path / "invalid")
    _, provider = _seed(factory)
    invalid_registry = WorkspaceResourceAdapterRegistry(
        {
            "prompt": _StaticAdapter(
                "prompt", "prompt-candidate", "7", SimpleNamespace(), _sha("candidate")
            ),
            "eval_dataset": _StaticAdapter(
                "eval_dataset", "DS-pinned", "7", _ResolvedDataset("DS-pinned", 7), _sha("dataset")
            ),
            "judge": _StaticAdapter(
                "judge", "JDG-pinned", _sha("judge"), _ResolvedJudge("pinned"), _sha("judge")
            ),
        }
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="content"):
        _plan_call(factory, invalid_registry, provider, verify_digests=False)


@pytest.mark.parametrize(
    ("field", "message"),
    [("provider", "provider does not match"), ("config", "worker configuration")],
)
def test_plan_rejects_provider_mismatch_and_plan_digest_mismatch(
    tmp_path: Any, field: str, message: str
) -> None:
    factory = _factory(tmp_path / field)
    registry, provider = _seed(factory)
    config = (
        SimpleNamespace(eval_provider="other")
        if field == "config"
        else SimpleNamespace(eval_provider="fake")
    )
    if field == "provider":
        provider.provider_name = "other"
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match=message):
        _plan_call(factory, registry, provider, config=config, verify_digests=False)

    factory = _factory(tmp_path / f"{field}-digest")
    registry, provider = _seed(factory)
    with factory() as session:
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        evaluation.evaluation_plan_sha256 = HEX
        session.commit()
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="plan digest"):
        _plan_call(factory, registry, provider)


def test_plan_rejects_invalid_dataset_and_judge_identity(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    invalid_dataset_registry = WorkspaceResourceAdapterRegistry(
        {
            "prompt": registry.require("prompt"),
            "eval_dataset": _StaticAdapter(
                "eval_dataset", "DS-pinned", "7", SimpleNamespace(), _sha("dataset")
            ),
            "judge": registry.require("judge"),
        }
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="incomplete identity"):
        _plan_call(factory, invalid_dataset_registry, provider, verify_digests=False)


def test_no_go_and_provider_failure_are_recorded_without_fabricated_evidence(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    provider.candidate_scores = ScoreSet(overall=0.2, dimensions={"Judge.pinned": 0.2})
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )
    claimed = worker.tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        release = session.get(CaliberWorkspaceRelease, claimed.workspace_release_id)
        assert evaluation is not None and release is not None
        assert evaluation.status == EVALUATION_SUCCEEDED
        assert release.status == "rejected"
        assert release.evaluation_evidence_sha256 is not None

    factory = _factory(tmp_path / "provider-error")
    registry, provider = _seed(factory)
    provider.eval_callable = lambda _request: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")
    )
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )
    claimed = worker.tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        assert evaluation is not None and evaluation.status == EVALUATION_FAILED
        assert evaluation.error_code == "evaluation_execution_failed"


def test_gate_rejects_invalid_provider_result_and_missing_baseline(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    provider.eval_callable = lambda request: EvalComparison(
        candidate=ScoreSet(overall=2.0),
        baseline=None,
        eval_dataset_id=request.eval_dataset_id,
        n_examples=1,
    )
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )
    claimed = worker.tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        assert evaluation is not None and evaluation.status == EVALUATION_FAILED

    factory = _factory(tmp_path / "delta")
    registry, provider = _seed(factory, evaluation_config={"min_aggregate_delta": 0.1})
    with factory() as session:
        release = session.execute(select(CaliberWorkspaceRelease)).scalar_one()
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        probe = resolve_workspace_release_evaluation_plan(
            session,
            release=release,
            evaluation=evaluation,
            identity=SimpleNamespace(
                user_id="dev", scopes=frozenset(), active_project_id=PROJECT_ID
            ),
            adapter_registry=registry,
            provider=provider,
            config=SimpleNamespace(eval_provider="fake"),
            verify_digests=False,
        )
        evaluation.evaluation_plan_sha256 = probe.plan_digest
        evaluation.input_sha256 = probe.input_digest
        session.commit()
    claimed = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    ).tick()
    assert claimed is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claimed.evaluation_id)
        assert evaluation is not None and evaluation.status == EVALUATION_FAILED


def test_worker_fences_execution_completion_and_gate_failure_paths(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    plan = _plan_call(factory, registry, provider, verify_digests=False)
    claim = ClaimedEvaluation("eval-b10-2", PROJECT_ID, "release-b10-2", 1, "worker", None)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )

    with factory() as session, pytest.raises(LeaseLost):
        WorkspaceReleaseEvaluator(
            factory,
            SimpleNamespace(
                workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30
            ),
            lease_provider=lambda: None,
            eval_provider=provider,
            adapter_registry=registry,
        )._assert_execution_owner(session)
    fenced: list[object] = []
    monkeypatch.setattr(
        evaluator_module, "assert_fenced", lambda _session, lease: fenced.append(lease)
    )
    with factory() as session:
        WorkspaceReleaseEvaluator(
            factory,
            SimpleNamespace(
                workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30
            ),
            lease_provider=lambda: "lease",
            eval_provider=provider,
            adapter_registry=registry,
        )._assert_execution_owner(session)
    assert fenced == ["lease"]

    with pytest.raises(ValueError, match="pinned dataset"):
        worker._gate_result(
            plan,
            EvalComparison(
                candidate=ScoreSet(overall=0.9),
                baseline=None,
                eval_dataset_id="wrong",
                n_examples=1,
            ),
            claim,
        )
    with pytest.raises(ValueError, match="baseline score"):
        worker._gate_result(
            plan,
            EvalComparison(
                candidate=ScoreSet(overall=0.9),
                baseline=ScoreSet(overall=2.0),
                eval_dataset_id=plan.request.eval_dataset_id,
                n_examples=1,
            ),
            claim,
        )
    delta_plan = replace(plan, min_aggregate_delta=0.05)
    result, evidence, gate_evidence = worker._gate_result(
        delta_plan,
        EvalComparison(
            candidate=ScoreSet(overall=0.9),
            baseline=ScoreSet(overall=0.8),
            eval_dataset_id=plan.request.eval_dataset_id,
            n_examples=1,
            evidence_rows=(
                {
                    "example_id": "delta-1",
                    "input": {"q": "q"},
                    "expected": {"a": "a"},
                    "scores": {"Judge.pinned": 0.9},
                    "score": 0.9,
                    "passed": True,
                },
            ),
        ),
        claim,
    )
    assert result == "pass" and len(evidence) == 64
    assert gate_evidence["results"]["n_examples"] == 1

    missing_plan, missing_lock, missing_error = worker._prepare_claim(claim)
    assert missing_plan is None and missing_lock is None and missing_error is not None
    worker._complete_blocked(claim, expected_lock_version=None, error=RuntimeError("missing"))
    worker._complete(claim, expected_lock_version=1, result="blocked")

    claim_worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=300, execution_lease_seconds=30),
    )
    real_claim = claim_worker.tick()
    assert real_claim is not None
    worker._assert_execution_owner = lambda _session: (_ for _ in ()).throw(LeaseLost("lost"))
    worker._complete(real_claim, expected_lock_version=1, result="blocked")
    monkeypatch.setattr(
        evaluator_module,
        "complete_workspace_release_evaluation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("completion failed")),
    )
    worker._assert_execution_owner = lambda _session: None
    worker._complete(real_claim, expected_lock_version=1, result="blocked")

    async def lifecycle() -> None:
        await worker.start()
        await worker.stop()

    asyncio.run(lifecycle())


class _RaisingValidationAdapter(_StaticAdapter):
    def validate(self, _session: object, _pin: object, _environment: object = None) -> object:
        raise RuntimeError("adapter unavailable")


def test_plan_resolver_covers_fail_closed_pin_and_coordinate_guards(tmp_path: Any) -> None:
    factory = _factory(tmp_path / "config")
    registry, provider = _seed(factory)
    pin = SimpleNamespace(resolution={}, purpose="evaluation", resource_type="other")
    assert plan_module._config(SimpleNamespace(manifest={}), pin) == {}
    assert (
        plan_module._role(
            SimpleNamespace(
                resolution={"release_evaluation": {"role": "candidate"}},
                purpose="evaluation",
                resource_type="other",
            )
        )
        == "candidate"
    )
    assert (
        plan_module._role(
            SimpleNamespace(resolution={}, purpose="evaluation", resource_type="other")
        )
        is None
    )

    raising_registry = WorkspaceResourceAdapterRegistry(
        {
            "prompt": _RaisingValidationAdapter(
                "prompt",
                "prompt-candidate",
                "7",
                _ResolvedCandidate("candidate"),
                _sha("candidate"),
            ),
            "eval_dataset": registry.require("eval_dataset"),
            "judge": registry.require("judge"),
        }
    )
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="could not resolve"):
        _plan_call(factory, raising_registry, provider, verify_digests=False)

    factory = _factory(tmp_path / "snapshot-type")
    registry, provider = _seed(factory)
    bad_snapshot = _StaticAdapter(
        "prompt", "prompt-candidate", "7", _ResolvedCandidate("candidate"), _sha("candidate")
    )
    bad_snapshot.snapshot = lambda *_args: object()  # type: ignore[method-assign]
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="snapshot pin"):
        _plan_call(
            factory,
            WorkspaceResourceAdapterRegistry(
                {
                    "prompt": bad_snapshot,
                    "eval_dataset": registry.require("eval_dataset"),
                    "judge": registry.require("judge"),
                }
            ),
            provider,
            verify_digests=False,
        )

    factory = _factory(tmp_path / "snapshot-identity")
    registry, provider = _seed(factory)
    changed_snapshot = _StaticAdapter(
        "prompt", "prompt-candidate", "7", _ResolvedCandidate("candidate"), _sha("candidate")
    )
    changed_snapshot.snapshot = lambda *_args: SnapshotPin(
        resource_id="other", version_ref="7", content_sha256=_sha("candidate")
    )  # type: ignore[method-assign]
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="identity changed"):
        _plan_call(
            factory,
            WorkspaceResourceAdapterRegistry(
                {
                    "prompt": changed_snapshot,
                    "eval_dataset": registry.require("eval_dataset"),
                    "judge": registry.require("judge"),
                }
            ),
            provider,
            verify_digests=False,
        )

    factory = _factory(tmp_path / "coordinates")
    registry, provider = _seed(factory)
    with factory() as session:
        release = session.execute(select(CaliberWorkspaceRelease)).scalar_one()
        release.environment_id = "WSE-missing"
        session.commit()
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="project or environment"):
        _plan_call(factory, registry, provider, verify_digests=False)

    factory = _factory(tmp_path / "candidate")
    registry, provider = _seed(factory, candidate_type="test_set", derive_digests=False)
    with pytest.raises(WorkspaceReleaseEvaluationPlanError, match="cannot be evaluated"):
        _plan_call(factory, registry, provider, verify_digests=False)
