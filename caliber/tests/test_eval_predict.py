"""Tests for the functional eval gate — default predict_fn + DB dataset loader.

These cover the wiring that makes ``MLflowEvalProvider`` run without a
per-agent ``register_predict_fn`` call (previously it raised on every job):

* the default predict_fn renders the candidate prompt + calls a completion fn,
* the DB-backed loader shapes CALIBER eval datasets for ``mlflow.genai.evaluate``,
* ``MLflowEvalProvider.evaluate`` uses the default factory for an unregistered
  agent and folds the returned metrics (mlflow.genai.evaluate is stubbed so no
  network/LLM is needed).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import mlflow.genai
import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from caliber.eval.mlflow_runner import MLflowEvalProvider
from caliber.eval.predict import (
    build_completion_fn,
    build_db_load_dataset,
    build_default_predict_fn_factory,
)
from caliber.eval.provider import EvalRequest

# ───────────────────── default predict_fn ─────────────────────


def test_default_predict_fn_renders_template_and_picks_user_field() -> None:
    captured: dict[str, str] = {}

    def fake_complete(system: str, user: str) -> str:
        captured["system"] = system
        captured["user"] = user
        return "ANSWER"

    factory = build_default_predict_fn_factory(fake_complete)
    predict_fn = factory("You are helpful. Org: {{org}}.")

    out = predict_fn(question="how do refunds work?", org="Acme")

    assert out == "ANSWER"
    assert "Org: Acme." in captured["system"]  # {{org}} rendered from inputs
    assert captured["user"] == "how do refunds work?"  # conventional field picked


def test_default_predict_fn_user_message_fallbacks() -> None:
    seen: list[str] = []
    factory = build_default_predict_fn_factory(lambda _s, u: seen.append(u) or "x")

    factory("p")(foo="bar")  # sole string value
    factory("p")(a="x", b="y")  # multiple → JSON dump

    assert seen[0] == "bar"
    assert json.loads(seen[1]) == {"a": "x", "b": "y"}


# ───────────────────── completion builder ─────────────────────


def test_build_completion_fn_none_for_fake_provider() -> None:
    cfg = SimpleNamespace(llm_provider="fake", llm_api_key_env="X", llm_diagnosis_model="m")
    assert build_completion_fn(cfg) is None  # type: ignore[arg-type]


def test_build_completion_fn_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caliber.eval.predict.resolve_secret", lambda _env: "")
    cfg = SimpleNamespace(llm_provider="openai", llm_api_key_env="X", llm_diagnosis_model="gpt-4o")
    assert build_completion_fn(cfg) is None  # type: ignore[arg-type]


def test_build_completion_fn_openai_builds_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caliber.eval.predict.resolve_secret", lambda _env: "sk-test")
    cfg = SimpleNamespace(llm_provider="openai", llm_api_key_env="X", llm_diagnosis_model="gpt-4o")
    fn = build_completion_fn(cfg)  # type: ignore[arg-type]
    # Constructs an OpenAI client (no network) and returns a callable.
    assert callable(fn)


# ───────────────────── DB dataset loader ─────────────────────


def _seed_dataset(session: Session, dataset_id: str, *, examples: list[dict]) -> None:
    session.add(CaliberEvalDataset(dataset_id=dataset_id, name=f"name-{dataset_id}", owner="@me"))
    for i, ex in enumerate(examples):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"{dataset_id}-EX{i}",
                dataset_id=dataset_id,
                dataset_version=1,
                input=ex["input"],
                expected=ex.get("expected", {}),
                superseded_at=ex.get("superseded_at"),
            )
        )
    session.commit()


def test_load_dataset_shapes_examples_and_excludes_superseded(
    session_factory: sessionmaker[Session],
) -> None:
    from datetime import datetime, timezone

    with session_factory() as session:
        _seed_dataset(
            session,
            "DS-1",
            examples=[
                {"input": {"question": "q1"}, "expected": {"answer": "a1"}},
                {
                    "input": {"question": "old"},
                    "expected": {"answer": "x"},
                    "superseded_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
                },
            ],
        )

    data = build_db_load_dataset(session_factory)("DS-1")

    assert data == [{"inputs": {"question": "q1"}, "expectations": {"answer": "a1"}}]


def test_load_dataset_empty_raises(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _seed_dataset(session, "DS-empty", examples=[])

    with pytest.raises(ValueError, match="no active examples"):
        build_db_load_dataset(session_factory)("DS-empty")


def _seed_versioned_dataset(session: Session, dataset_id: str) -> None:
    """Seed a dataset whose history is: v1 has ex1; v2 appends ex2.

    ex1 appeared at v1 and is later retired *at v2* (``superseded_version=2``).
    ex2 appeared at v2. So the active set "as of v1" is {ex1}, and the active
    set "as of v2" (and current) is {ex2}.
    """
    from datetime import datetime, timezone

    session.add(CaliberEvalDataset(dataset_id=dataset_id, name=f"name-{dataset_id}", owner="@me"))
    session.add(
        CaliberEvalDatasetExample(
            example_id=f"{dataset_id}-EX1",
            dataset_id=dataset_id,
            dataset_version=1,
            input={"question": "v1-only"},
            expected={"answer": "a1"},
            superseded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            superseded_version=2,
        )
    )
    session.add(
        CaliberEvalDatasetExample(
            example_id=f"{dataset_id}-EX2",
            dataset_id=dataset_id,
            dataset_version=2,
            input={"question": "v2-added"},
            expected={"answer": "a2"},
        )
    )
    session.commit()


def test_load_dataset_resolves_examples_as_of_pinned_version(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_versioned_dataset(session, "DS-VER")

    load = build_db_load_dataset(session_factory)

    # Pinned to v1: only the example that existed (and was active) at v1.
    as_of_v1 = load("DS-VER", 1)
    assert as_of_v1 == [{"inputs": {"question": "v1-only"}, "expectations": {"answer": "a1"}}]

    # Pinned to v2: the v1 example was retired at v2; the v2 example is active.
    as_of_v2 = load("DS-VER", 2)
    assert as_of_v2 == [{"inputs": {"question": "v2-added"}, "expectations": {"answer": "a2"}}]

    # Unpinned (None): current active set == v2 set.
    assert load("DS-VER") == as_of_v2


def test_load_dataset_pinned_version_with_no_examples_raises(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_versioned_dataset(session, "DS-VER-EMPTY")

    # Pinning to a version below the first example's version yields nothing.
    with pytest.raises(ValueError, match="no active examples at version 0"):
        build_db_load_dataset(session_factory)("DS-VER-EMPTY", 0)


# ───────────────────── MLflowEvalProvider with the default factory ─────────────────────


def test_evaluate_uses_default_factory_for_unregistered_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the fix: an agent with no registered factory still
    runs (the default factory drives predict_fn), and metrics fold — instead of
    raising EvalProviderError as it did before."""
    invoked: list[str] = []

    class _FakeResult:
        metrics = {"Correctness/mean": 0.9, "Safety/mean": 0.8}

    def fake_evaluate(*, data, predict_fn, scorers):
        # Exercise the predict_fn exactly as mlflow.genai.evaluate would.
        for row in data:
            invoked.append(predict_fn(**row["inputs"]))
        return _FakeResult()

    monkeypatch.setattr(mlflow.genai, "evaluate", fake_evaluate)

    provider = MLflowEvalProvider(
        load_dataset=lambda _ds: [{"inputs": {"question": "q1"}, "expectations": {"a": "1"}}],
        # Non-empty explicit scorers list bypasses the mlflow.genai.scorers import.
        scorers=[object()],
        default_factory=build_default_predict_fn_factory(lambda _s, u: f"out:{u}"),
    )

    comparison = provider.evaluate(
        EvalRequest(
            agent_id="never-registered",
            job_id="J-1",
            artifact_type="prompt",
            candidate_content="You are a support agent.",
            baseline_content=None,
            eval_dataset_id="DS-1",
        )
    )

    assert invoked == ["out:q1"]  # default predict_fn actually ran
    assert comparison.candidate.overall == pytest.approx(0.85)  # (0.9 + 0.8) / 2
    assert comparison.candidate.dimensions == {"Correctness": 0.9, "Safety": 0.8}
    assert comparison.n_examples == 1


def test_evaluate_still_raises_without_factory_or_default() -> None:
    provider = MLflowEvalProvider(
        load_dataset=lambda _ds: [{"inputs": {"q": "x"}}],
        scorers=[object()],
    )
    from caliber.eval.provider import EvalProviderError

    with pytest.raises(EvalProviderError, match="no predict_fn"):
        provider.evaluate(
            EvalRequest(
                agent_id="unregistered",
                job_id="J",
                artifact_type="prompt",
                candidate_content="c",
                baseline_content=None,
                eval_dataset_id="DS",
            )
        )
