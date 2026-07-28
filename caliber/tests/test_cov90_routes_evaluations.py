"""Targeted coverage tests for ``caliber.routes.evaluations``.

Complements ``tests/test_routes_evaluations.py`` (the main behavioural suite for
this module) by exercising branches that suite doesn't reach: real
``load_prompt_template`` error/success paths (it monkeypatches the function
away entirely), the historical dataset-version pin, the real
``_build_workflow_predict`` compile/execute paths (it monkeypatches the whole
function away), the judge-build failure branch, the invalid ``limit`` query
param, and the ``max_examples`` truncation branch.
"""

from __future__ import annotations

from types import SimpleNamespace

import mlflow
import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberJudge,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.eval.judge_scorer import JudgeError
from caliber.routes.evaluations import LIST_PATH


def _seed_dataset_with_examples(session: Session, dataset_id: str = "ED-cov") -> str:
    """Seed a 2-example dataset: EX-1 first appeared at version 2, EX-2 at
    version 3 (mirrors ``test_routes_evaluations._seed_dataset_with_examples``,
    parameterised on ``dataset_id`` so tests in this file don't collide)."""
    ds = CaliberEvalDataset(
        dataset_id=dataset_id,
        name="capitals",
        description="",
        owner="@sarah",
        tags=[],
        status="active",
        version=3,
    )
    session.add(ds)
    session.add_all(
        [
            CaliberEvalDatasetExample(
                example_id=f"{dataset_id}-EX-1",
                dataset_id=dataset_id,
                dataset_version=2,
                input={"question": "capital of France"},
                expected={"expected": "Paris"},
                weight=1.0,
                tags=[],
            ),
            CaliberEvalDatasetExample(
                example_id=f"{dataset_id}-EX-2",
                dataset_id=dataset_id,
                dataset_version=3,
                input={"question": "2+2"},
                expected={"expected": "4"},
                weight=1.0,
                tags=[],
            ),
        ]
    )
    session.commit()
    return dataset_id


def _seed_judge(session: Session, judge_id: str = "JDG-cov", status: str = "active") -> str:
    session.add(
        CaliberJudge(
            judge_id=judge_id,
            name="cov-judge",
            description="",
            instructions="Return true when {{ outputs }} mentions Paris.",
            model="openai:/gpt-4o-mini",
            feedback_value_type="bool",
            owner="@sarah",
            tags=[],
            status=status,
        )
    )
    session.commit()
    return judge_id


def _seed_workflow_version(session: Session, version_id: str = "WFV-cov") -> str:
    session.add(
        CaliberWorkflow(
            workflow_id="WF-cov",
            name="cov workflow",
            description="",
            owner="@sarah",
            status="active",
        )
    )
    session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id="WF-cov",
            version_number=1,
            status="published",
            manifest={},
            manifest_hash="",
            created_by="@sarah",
        )
    )
    session.commit()
    return version_id


def _fake_completion(_config):  # type: ignore[no-untyped-def]
    def complete(_system: str, user: str) -> str:
        return {"capital of France": "Paris"}.get(user.strip(), "I don't know")

    return complete


def _echo_system_completion(_config):  # type: ignore[no-untyped-def]
    """A fake completion that echoes its system instruction, so a test can
    prove the artifact (prompt template) was rendered as the system prompt."""

    def complete(system: str, user: str) -> str:
        return f"SYS[{system}] USER[{user}]"

    return complete


# --- load_prompt_template: real function, no monkeypatch (lines 130-156) ----


def test_load_prompt_template_missing_name_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "prompt",
            # partitions to an empty name before "@" -> the name-required 400.
            "subject_ref": "@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "must include a name" in resp.json()["detail"]


def test_load_prompt_template_registry_unavailable_503(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    # Simulate an mlflow build with no prompt-registry API.
    monkeypatch.setattr(mlflow, "load_prompt", None, raising=False)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "prompt",
            "subject_ref": "grader@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 503, resp.text
    assert "prompt registry API not available" in resp.json()["detail"]


def test_load_prompt_template_not_found_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    monkeypatch.setattr(mlflow, "load_prompt", lambda ref, allow_missing=True: None, raising=False)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "prompt",
            "subject_ref": "grader@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


def test_load_prompt_template_empty_content_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    monkeypatch.setattr(
        mlflow,
        "load_prompt",
        lambda ref, allow_missing=True: SimpleNamespace(template="", content=""),
        raising=False,
    )

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "prompt",
            "subject_ref": "grader@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "no template content" in resp.json()["detail"]


def test_load_prompt_template_success_renders_as_system(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    monkeypatch.setattr(
        mlflow,
        "load_prompt",
        lambda ref, allow_missing=True: SimpleNamespace(template="Grade: {{question}}"),
        raising=False,
    )

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "prompt",
            "subject_ref": "grader@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["predict_target"] == "prompt"
    by_id = {row["example_id"]: row for row in data["results"]}
    assert "SYS[Grade: capital of France]" in by_id["ED-cov-EX-1"]["prediction"]


# --- Historical dataset-version pin (line 105) -------------------------------


def test_create_evaluation_pins_historical_dataset_version(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-cov", "dataset_version": 2, "scorers": ["exact_match"]},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["dataset_version"] == 2
    # EX-2 first appeared at version 3, so "as of version 2" only EX-1 exists.
    assert data["n_examples"] == 1
    by_id = {row["example_id"]: row for row in data["results"]}
    assert list(by_id) == ["ED-cov-EX-1"]


# --- Real _build_workflow_predict compile/execute paths (221-233) -----------


def test_build_workflow_predict_compile_error_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_workflow_version(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)

    def _boom_build_plan(session, version, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("bad manifest")

    monkeypatch.setattr("caliber.workflows.promoter.build_plan", _boom_build_plan)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "workflow",
            "subject_ref": "WFV-cov",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "failed to compile workflow" in resp.json()["detail"]


def test_build_workflow_predict_success_scores_examples(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_workflow_version(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)

    def _fake_build_plan(session, version, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(ir=SimpleNamespace(nodes={}))

    def _fake_build_executor(config, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace()

    def _fake_execute(plan, input_text, *, executor, preview=False, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(output=f"ran[{input_text}]")

    monkeypatch.setattr("caliber.workflows.promoter.build_plan", _fake_build_plan)
    monkeypatch.setattr("caliber.workflows.promoter.build_executor", _fake_build_executor)
    monkeypatch.setattr("caliber.workflows.runtime.execute", _fake_execute)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-cov",
            "predict_target": "workflow",
            "subject_ref": "WFV-cov",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "completed"
    by_id = {row["example_id"]: row for row in data["results"]}
    assert by_id["ED-cov-EX-1"]["prediction"].startswith("ran[")
    assert by_id["ED-cov-EX-1"]["scores"]["non_empty"] == 1.0


# --- Judge build failure (277-278) -------------------------------------------


def test_hydrate_judge_runners_build_judge_error_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_judge(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    def _boom_build_judge(name, instructions, *, model=None, feedback_value_type=None):  # type: ignore[no-untyped-def]
        raise JudgeError("bad judge instructions")

    monkeypatch.setattr(evaluations_route, "build_judge", _boom_build_judge)

    resp = client.post(LIST_PATH, json={"dataset_id": "ED-cov", "scorers": ["Judge.JDG-cov"]})
    assert resp.status_code == 400, resp.text
    assert "failed to build judge" in resp.json()["detail"]


# --- list_evaluations invalid `limit` query param (292-293) -----------------


def test_list_evaluations_invalid_limit_falls_back_to_default(client: TestClient) -> None:
    resp = client.get(LIST_PATH, params={"limit": "not-a-number"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


# --- max_examples truncation (line 384) --------------------------------------


def test_create_evaluation_truncates_rows_to_max_examples(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-cov", "scorers": ["exact_match"], "max_examples": 1},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["n_examples"] == 1
    assert len(data["results"]) == 1
