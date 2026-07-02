"""Coverage-focused tests for ``caliber.orchestrator.candidate``.

Targets branches left uncovered by ``tests/test_orchestrator_candidate.py``:

* the missing-agent lookup in :func:`run_candidate`,
* the manual prompt-optimization baseline/dataset-pin helpers
  (``_prompt_optimization_baseline`` / ``_prompt_optimization_dataset_pin``),
* the DSPy trainset loader's pinned-dataset and pinned-version branches
  (:func:`_load_trainset`),
* the MLflow-unavailable and malformed-version-object fallback paths of
  ``_register_candidate_prompt_draft``, and
* the ``_import_mlflow`` / ``_resolve_prompt_api`` internals.

These are exercised both end-to-end (through :func:`run_candidate`) and, where
DB plumbing would add noise without adding coverage, via direct calls to the
module's private helpers -- they live in the module under test, not another
test module, so importing them directly is fair game.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.orchestrator.candidate import (
    _import_mlflow,
    _load_trainset,
    _prompt_optimization_baseline,
    _prompt_optimization_dataset_pin,
    _register_candidate_prompt_draft,
    _resolve_prompt_api,
    run_candidate,
)

_DIAGNOSIS = {
    "root_cause": "Prompt allows skipping lookup_policy.",
    "affected_components": ["prompt"],
    "confidence": 0.85,
    "alternatives": [],
}


def _seed_agent(
    session: Session,
    *,
    agent_id: str = "support-agent",
    optimizer_config: dict[str, object] | None = None,
) -> CaliberAgentConfig:
    agent = CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id=f"exp-{agent_id}",
        name="Support",
        owner="@sarah",
        artifact_types=["prompt"],
        eval_thresholds={},
        optimizer_config=optimizer_config or {},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    return agent


def _seed_item(
    session: Session,
    *,
    item_id: str = "FB-C",
    agent_id: str = "support-agent",
    submitted_context: dict[str, object] | None = None,
) -> CaliberVerificationItem:
    item = CaliberVerificationItem(
        item_id=item_id,
        agent_id=agent_id,
        category="hallucination",
        free_text="Agent invented refund timeline.",
        severity="critical",
        status="verified",
        submitted_context=submitted_context,
    )
    session.add(item)
    session.flush()
    return item


def _seed_job(
    session: Session,
    *,
    job_id: str = "RFN-C",
    agent_id: str = "support-agent",
    item_id: str = "FB-C",
    status: str = "running",
    stage: str = "candidate",
    optimizer_type: str | None = None,
) -> CaliberRefinementJob:
    job = CaliberRefinementJob(
        job_id=job_id,
        agent_id=agent_id,
        primary_item_id=item_id,
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
        optimizer_type=optimizer_type,
        diagnosis=dict(_DIAGNOSIS),
    )
    session.add(job)
    session.commit()
    return job


# ---------------------------------------------------------------------------
# run_candidate: missing agent (line 127)
# ---------------------------------------------------------------------------


def test_run_candidate_raises_when_agent_missing(db_session: Session) -> None:
    """No ``CaliberAgentConfig`` row for the job's ``agent_id`` -> LookupError."""
    _seed_item(db_session, agent_id="ghost-agent")
    job = _seed_job(db_session, agent_id="ghost-agent")

    with pytest.raises(LookupError, match=r"agent 'ghost-agent' not found for job 'RFN-C'"):
        run_candidate(db_session, job.job_id, FakeLLMProvider(), FakeArtifactStore())


# ---------------------------------------------------------------------------
# _prompt_optimization_baseline (lines 283-286, 288-289)
# ---------------------------------------------------------------------------


def test_prompt_optimization_baseline_none_when_source_mismatch(db_session: Session) -> None:
    """``submitted_context`` is a dict, but not a prompt-optimization submission."""
    item = _seed_item(
        db_session,
        submitted_context={"source": "manual_feedback", "prompt_optimization": {}},
    )
    result = _prompt_optimization_baseline(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert result is None


def test_prompt_optimization_baseline_none_when_raw_not_dict(db_session: Session) -> None:
    """``prompt_optimization`` key present but not a dict -> treated as absent."""
    item = _seed_item(
        db_session,
        submitted_context={"source": "prompt_optimization", "prompt_optimization": "not-a-dict"},
    )
    result = _prompt_optimization_baseline(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert result is None


def test_prompt_optimization_baseline_returns_baseline_content(db_session: Session) -> None:
    item = _seed_item(
        db_session,
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {"baseline_content": "old prompt v3"},
        },
    )
    result = _prompt_optimization_baseline(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert result == "old prompt v3"


def test_prompt_optimization_baseline_none_when_baseline_content_empty(db_session: Session) -> None:
    """Non-string/empty ``baseline_content`` falls back to ``None`` (line 289 else)."""
    item = _seed_item(
        db_session,
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {"baseline_content": ""},
        },
    )
    result = _prompt_optimization_baseline(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert result is None


# ---------------------------------------------------------------------------
# _prompt_optimization_dataset_pin (lines 307-310, 312-315, 320)
# ---------------------------------------------------------------------------


def test_prompt_optimization_dataset_pin_none_when_source_mismatch(db_session: Session) -> None:
    item = _seed_item(
        db_session,
        submitted_context={"source": "manual_feedback"},
    )
    dataset_id, version = _prompt_optimization_dataset_pin(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert dataset_id is None
    assert version is None


def test_prompt_optimization_dataset_pin_returns_pin_when_valid(db_session: Session) -> None:
    item = _seed_item(
        db_session,
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {"eval_dataset_id": "DS-1", "eval_dataset_version": 4},
        },
    )
    dataset_id, version = _prompt_optimization_dataset_pin(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert dataset_id == "DS-1"
    assert version == 4


def test_prompt_optimization_dataset_pin_ignores_invalid_version(db_session: Session) -> None:
    """A boolean or sub-1 version fails the ``int and not bool and >= 1`` guard."""
    item = _seed_item(
        db_session,
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {"eval_dataset_id": "DS-1", "eval_dataset_version": True},
        },
    )
    dataset_id, version = _prompt_optimization_dataset_pin(
        db_session, SimpleNamespace(primary_item_id=item.item_id)
    )
    assert dataset_id == "DS-1"
    assert version is None


# ---------------------------------------------------------------------------
# run_candidate integration: baseline + dataset pin flow into a DSPy job
# (exercises the same lines end-to-end through the real pipeline)
# ---------------------------------------------------------------------------


def test_run_candidate_honors_pinned_baseline_and_dataset_for_dspy(db_session: Session) -> None:
    _seed_agent(db_session)
    db_session.add(CaliberEvalDataset(dataset_id="DS-PIN", name="pinned-set", owner="@sarah"))
    db_session.flush()
    db_session.add_all(
        [
            CaliberEvalDatasetExample(
                example_id="EX-1",
                dataset_id="DS-PIN",
                dataset_version=1,
                input={"marker": "keep-v1"},
                expected={},
                weight=1.0,
            ),
            CaliberEvalDatasetExample(
                example_id="EX-2",
                dataset_id="DS-PIN",
                dataset_version=2,
                input={"marker": "keep-v2"},
                expected={},
                weight=1.0,
            ),
            CaliberEvalDatasetExample(
                example_id="EX-3",
                dataset_id="DS-PIN",
                dataset_version=3,
                input={"marker": "too-new"},
                expected={},
                weight=1.0,
            ),
        ]
    )
    db_session.flush()
    _seed_item(
        db_session,
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {
                "baseline_content": "old prompt text",
                "eval_dataset_id": "DS-PIN",
                "eval_dataset_version": 2,
            },
        },
    )
    job = _seed_job(db_session, optimizer_type="DSPyBootstrapFewShot")

    provider = FakeLLMProvider()
    run_candidate(db_session, job.job_id, provider, FakeArtifactStore())

    assert len(provider.candidate_calls) == 1
    ctx = provider.candidate_calls[0]
    # The manual-run baseline (not the artifact store) supplied the content.
    assert ctx.current_artifact_content == "old prompt text"
    assert ctx.optimizer_type == "DSPyBootstrapFewShot"
    assert ctx.trainset is not None
    markers = {row["input"]["marker"] for row in ctx.trainset}
    assert markers == {"keep-v1", "keep-v2"}


# ---------------------------------------------------------------------------
# _load_trainset (lines 433, 461)
# ---------------------------------------------------------------------------


def test_load_trainset_pinned_dataset_id_takes_precedence(db_session: Session) -> None:
    """A pinned dataset id short-circuits the agent-level dataset resolution."""
    db_session.add(CaliberEvalDataset(dataset_id="DS-A", name="dataset-a", owner="@sarah"))
    db_session.flush()
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EX-A1",
            dataset_id="DS-A",
            dataset_version=1,
            input={"marker": "a1"},
            expected={"answer": "yes"},
            weight=2.0,
        )
    )
    db_session.flush()

    # eval_thresholds points nowhere useful -- proves the pin (not agent
    # resolution) drove the lookup.
    agent = CaliberAgentConfig(agent_id="agent-x", eval_thresholds={"eval_dataset_id": "unused"})

    rows = _load_trainset(db_session, agent, pinned_dataset_id="DS-A")
    assert rows == [{"input": {"marker": "a1"}, "expected": {"answer": "yes"}, "weight": 2.0}]


def test_load_trainset_pinned_version_filters_examples(db_session: Session) -> None:
    """``pinned_version`` reconstructs the active example set as of version N."""
    db_session.add(CaliberEvalDataset(dataset_id="DS-B", name="dataset-b", owner="@sarah"))
    db_session.flush()
    db_session.add_all(
        [
            # dataset_version 1, never superseded -> included at pin=2.
            CaliberEvalDatasetExample(
                example_id="EX-B1",
                dataset_id="DS-B",
                dataset_version=1,
                input={"marker": "keep-active"},
                expected={},
                weight=1.0,
            ),
            # dataset_version 2, at the pin boundary -> included.
            CaliberEvalDatasetExample(
                example_id="EX-B2",
                dataset_id="DS-B",
                dataset_version=2,
                input={"marker": "keep-boundary"},
                expected={},
                weight=1.0,
            ),
            # dataset_version 3, created after the pin -> excluded.
            CaliberEvalDatasetExample(
                example_id="EX-B3",
                dataset_id="DS-B",
                dataset_version=3,
                input={"marker": "too-new"},
                expected={},
                weight=1.0,
            ),
            # dataset_version 1, but retired at version 2 (<= pin) -> excluded.
            CaliberEvalDatasetExample(
                example_id="EX-B4",
                dataset_id="DS-B",
                dataset_version=1,
                input={"marker": "retired-before-pin"},
                expected={},
                weight=1.0,
                superseded_version=2,
            ),
            # dataset_version 1, retired after the pin -> still active at pin.
            CaliberEvalDatasetExample(
                example_id="EX-B5",
                dataset_id="DS-B",
                dataset_version=1,
                input={"marker": "retired-after-pin"},
                expected={},
                weight=1.0,
                superseded_version=5,
            ),
        ]
    )
    db_session.flush()

    agent = CaliberAgentConfig(agent_id="agent-y", eval_thresholds={})
    rows = _load_trainset(db_session, agent, pinned_dataset_id="DS-B", pinned_version=2)

    markers = {row["input"]["marker"] for row in rows}
    assert markers == {"keep-active", "keep-boundary", "retired-after-pin"}


# ---------------------------------------------------------------------------
# _register_candidate_prompt_draft: mlflow unavailable (lines 372, 376)
# ---------------------------------------------------------------------------


def test_run_candidate_skips_registration_when_mlflow_import_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``job.mlflow_run_id`` is set (so registration is attempted), but the
    module-level import hook reports MLflow as unavailable."""
    _seed_agent(db_session)
    _seed_item(db_session)
    job = _seed_job(db_session)
    job.mlflow_run_id = "run-789"
    db_session.commit()

    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: None)

    with caplog.at_level(logging.DEBUG, logger="caliber.orchestrator.candidate"):
        result = run_candidate(db_session, job.job_id, FakeLLMProvider(), FakeArtifactStore())

    assert result.current_stage == "eval"
    assert result.candidate is not None
    assert result.candidate.get("mlflow_candidate_prompt_ref") is None
    assert any("mlflow unavailable" in record.message for record in caplog.records)


def test_register_candidate_prompt_draft_none_when_mlflow_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-call variant of the same branch, isolated from the DB/pipeline."""
    job = SimpleNamespace(artifact_type="prompt", mlflow_run_id="run-1", agent_id="a1", job_id="j1")
    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: None)

    result = _register_candidate_prompt_draft(job=job, candidate_content="new content")
    assert result is None


# ---------------------------------------------------------------------------
# _register_candidate_prompt_draft: version-number fallback (lines 401-406)
# ---------------------------------------------------------------------------


def test_register_candidate_prompt_draft_falls_back_to_version_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable ``.uri`` on the returned version object -> derive a
    ``prompts:/{agent}/{version}`` ref from ``.version`` instead."""
    job = SimpleNamespace(
        artifact_type="prompt", mlflow_run_id="run-1", agent_id="agent-z", job_id="j1"
    )

    def _register_prompt(**_kwargs: object) -> object:
        return SimpleNamespace(uri=None, version=7)

    fake_mlflow = SimpleNamespace(genai=SimpleNamespace(register_prompt=_register_prompt))
    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: fake_mlflow)

    result = _register_candidate_prompt_draft(job=job, candidate_content="new content")
    assert result == "prompts:/agent-z/7"


def test_register_candidate_prompt_draft_none_when_version_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither a usable ``.uri`` nor an int-convertible ``.version`` -> None."""
    job = SimpleNamespace(
        artifact_type="prompt", mlflow_run_id="run-1", agent_id="agent-z", job_id="j1"
    )

    def _register_prompt(**_kwargs: object) -> object:
        return SimpleNamespace(uri="", version=None)

    fake_mlflow = SimpleNamespace(genai=SimpleNamespace(register_prompt=_register_prompt))
    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: fake_mlflow)

    result = _register_candidate_prompt_draft(job=job, candidate_content="new content")
    assert result is None


# ---------------------------------------------------------------------------
# _import_mlflow (lines 476-477)
# ---------------------------------------------------------------------------


def test_import_mlflow_returns_none_when_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.modules["mlflow"] = None`` forces the ``import mlflow`` statement
    inside ``_import_mlflow`` to raise ``ImportError`` (a standard stdlib
    mechanism for simulating an unavailable module)."""
    monkeypatch.setitem(sys.modules, "mlflow", None)
    assert _import_mlflow() is None


# ---------------------------------------------------------------------------
# _resolve_prompt_api (lines 485-488)
# ---------------------------------------------------------------------------


def test_resolve_prompt_api_falls_back_to_legacy_module_level_function() -> None:
    calls: list[str] = []
    mlflow_mod = SimpleNamespace(
        genai=SimpleNamespace(),  # no register_prompt on genai
        register_prompt=lambda: calls.append("legacy"),
    )
    fn = _resolve_prompt_api(mlflow_mod, "register_prompt")
    fn()
    assert calls == ["legacy"]


def test_resolve_prompt_api_raises_when_neither_available() -> None:
    mlflow_mod = SimpleNamespace(genai=SimpleNamespace())
    with pytest.raises(AttributeError, match="not available"):
        _resolve_prompt_api(mlflow_mod, "register_prompt")
