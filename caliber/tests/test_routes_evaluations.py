"""Integration tests for ``/caliber/evaluations`` (the scorecard surface).

The LLM predict target is faked by monkeypatching
``caliber.routes.evaluations.build_completion_fn`` so the run is deterministic
and needs no real provider.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberEvalRun,
    CaliberJudge,
    CaliberSkill,
)
from caliber.routes.evaluations import DETAIL_PATH, LIST_PATH


def _seed_dataset_with_examples(session: Session) -> str:
    ds = CaliberEvalDataset(
        dataset_id="ED-eval",
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
                example_id="EX-1",
                dataset_id="ED-eval",
                dataset_version=2,
                input={"question": "capital of France"},
                expected={"expected": "Paris"},
                weight=1.0,
                tags=[],
            ),
            CaliberEvalDatasetExample(
                example_id="EX-2",
                dataset_id="ED-eval",
                dataset_version=3,
                input={"question": "2+2"},
                expected={"expected": "4"},
                weight=1.0,
                tags=[],
            ),
        ]
    )
    session.commit()
    return "ED-eval"


def _fake_completion(_config):
    def complete(_system: str, user: str) -> str:
        return {"capital of France": "Paris"}.get(user.strip(), "I don't know")

    return complete


def test_run_evaluation_scores_examples(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-eval", "scorers": ["exact_match"], "label": "gpt baseline"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["run_id"].startswith("EVR-")
    assert data["status"] == "completed"
    assert data["n_examples"] == 2
    assert data["passed_count"] == 1
    assert data["failed_count"] == 1
    assert data["overall_score"] == 0.5
    assert data["aggregate"]["exact_match"] == 0.5
    assert data["predict_target"] == "llm"
    # Pinned to the dataset's current version when not specified.
    assert data["dataset_version"] == 3
    # Per-example rows present with predictions + scores.
    by_id = {row["example_id"]: row for row in data["results"]}
    assert by_id["EX-1"]["prediction"] == "Paris"
    assert by_id["EX-1"]["passed"] is True
    assert by_id["EX-2"]["passed"] is False


def test_run_evaluation_requires_real_provider(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", lambda _config: None)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval"})
    assert resp.status_code == 400
    assert "real LLM provider" in resp.json()["detail"]


def test_run_evaluation_unknown_dataset_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-missing"})
    assert resp.status_code == 404


def test_run_evaluation_no_examples_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        CaliberEvalDataset(
            dataset_id="ED-empty",
            name="empty",
            description="",
            owner="@x",
            tags=[],
            status="active",
            version=1,
        )
    )
    db_session.commit()
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-empty"})
    assert resp.status_code == 400
    assert "no examples" in resp.json()["detail"]


def test_run_evaluation_unknown_scorer_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    resp = client.post(
        LIST_PATH, json={"dataset_id": "ED-eval", "scorers": ["exact_match", "nope"]}
    )
    assert resp.status_code == 400
    assert "unknown scorer" in resp.json()["detail"]


def test_run_evaluation_all_errored_marks_failed(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)

    def exploding(_config):
        def complete(_system: str, _user: str) -> str:
            raise RuntimeError("provider down")

        return complete

    monkeypatch.setattr(evaluations_route, "build_completion_fn", exploding)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval"})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["passed_count"] == 0
    assert "provider down" in (data["error_message"] or "")


def test_list_and_get_evaluation(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    created = client.post(LIST_PATH, json={"dataset_id": "ED-eval", "scorers": ["exact_match"]})
    run_id = created.json()["data"]["run_id"]

    # List summaries (no heavy results array).
    listed = client.get(LIST_PATH, params={"dataset_id": "ED-eval"})
    assert listed.status_code == 200
    summaries = listed.json()["data"]
    assert [s["run_id"] for s in summaries] == [run_id]
    assert "results" not in summaries[0]

    # Detail carries the per-example rows.
    detail = client.get(DETAIL_PATH.replace("{run_id}", run_id))
    assert detail.status_code == 200
    assert len(detail.json()["data"]["results"]) == 2


def test_get_evaluation_404(client: TestClient) -> None:
    resp = client.get(DETAIL_PATH.replace("{run_id}", "EVR-missing"))
    assert resp.status_code == 404


def test_get_evaluation_cross_project_is_404(client: TestClient, db_session: Session) -> None:
    """Regression (#9): a project-scoped run from another project must NOT leak
    its full per-example results to a user in a different project. Previously a
    bare session.get returned 200 with the whole payload."""
    db_session.add(
        CaliberEvalDataset(
            dataset_id="ED-scope",
            name="scoped",
            description="",
            owner="@owner",
            tags=[],
            status="active",
            version=1,
        )
    )
    db_session.add(
        CaliberEvalRun(
            run_id="EVR-p2",
            dataset_id="ED-scope",
            dataset_version=1,
            project_id="P2",
            visibility="project",
            results=[{"example_id": "e1", "prediction": "secret"}],
            status="completed",
        )
    )
    db_session.commit()

    path = DETAIL_PATH.replace("{run_id}", "EVR-p2")
    # A non-admin user whose active project is P1 must get a clean 404 (no leak).
    resp = client.get(path, headers={"X-CALIBER-User": "@nobody", "X-CALIBER-Project": "P1"})
    assert resp.status_code == 404
    # An admin still sees it (admins bypass project scoping).
    assert client.get(path).status_code == 200


# --- Custom LLM judge scorers (the unified make_judge path) -----------------


class _FakeFeedback:
    def __init__(self, value: object, rationale: str | None = None) -> None:
        self.value = value
        self.rationale = rationale


def _seed_judge(session: Session, judge_id: str = "JDG-paris", status: str = "active") -> str:
    session.add(
        CaliberJudge(
            judge_id=judge_id,
            name="paris-judge",
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


def _fake_build_judge(name, instructions, *, model=None, feedback_value_type=None):  # type: ignore[no-untyped-def]
    """A deterministic stand-in for make_judge: scores 1.0 iff output says 'Paris'."""

    def judge(**kwargs: object) -> _FakeFeedback:
        out = str(kwargs.get("outputs", ""))
        return _FakeFeedback(value="paris" in out.lower(), rationale="checked for Paris")

    return judge


def test_run_evaluation_with_judge_scorer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_judge(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    monkeypatch.setattr(evaluations_route, "build_judge", _fake_build_judge)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-eval", "scorers": ["exact_match", "Judge.JDG-paris"]},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "completed"
    # The judge column rode through end-to-end.
    assert "Judge.JDG-paris" in data["scorers"]
    # EX-1 prediction "Paris" → judge 1.0; EX-2 "I don't know" → judge 0.0.
    assert data["aggregate"]["Judge.JDG-paris"] == 0.5
    by_id = {row["example_id"]: row for row in data["results"]}
    assert by_id["EX-1"]["scores"]["Judge.JDG-paris"] == 1.0
    assert by_id["EX-2"]["scores"]["Judge.JDG-paris"] == 0.0


def test_run_evaluation_judge_only_scorer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_judge(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    monkeypatch.setattr(evaluations_route, "build_judge", _fake_build_judge)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval", "scorers": ["Judge.JDG-paris"]})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["scorers"] == ["Judge.JDG-paris"]
    assert data["passed_count"] == 1  # only EX-1 (Paris) clears the 0.5 threshold


def test_run_evaluation_unknown_judge_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    monkeypatch.setattr(evaluations_route, "build_judge", _fake_build_judge)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval", "scorers": ["Judge.JDG-nope"]})
    assert resp.status_code == 404


def test_run_evaluation_archived_judge_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    _seed_judge(db_session, judge_id="JDG-old", status="archived")
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    monkeypatch.setattr(evaluations_route, "build_judge", _fake_build_judge)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval", "scorers": ["Judge.JDG-old"]})
    assert resp.status_code == 404


# --- Artifact-target scoring (prompt / skill / workflow) --------------------


def _echo_system_completion(_config):
    """A fake completion that echoes its system instruction, so a test can prove
    the *artifact* (prompt template / skill content) was rendered as the system."""

    def complete(system: str, user: str) -> str:
        return f"SYS[{system}] USER[{user}]"

    return complete


def test_run_evaluation_prompt_target(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    monkeypatch.setattr(
        evaluations_route, "load_prompt_template", lambda ref: "Grade: {{question}}"
    )

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-eval",
            "predict_target": "prompt",
            "subject_ref": "grader@2",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["predict_target"] == "prompt"
    assert data["subject_ref"] == "grader@2"
    # The prompt template was rendered as the system instruction (so the prompt
    # itself is what's under test) — EX-1's question filled the {{question}} var.
    by_id = {row["example_id"]: row for row in data["results"]}
    assert "SYS[Grade: capital of France]" in by_id["EX-1"]["prediction"]


def test_run_evaluation_skill_target(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    db_session.add(
        CaliberSkill(
            skill_id="SK-grader",
            name="grader-skill",
            content="Follow the grading rubric strictly.",
            owner="@sarah",
            status="active",
        )
    )
    db_session.commit()
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-eval",
            "predict_target": "skill",
            "subject_ref": "SK-grader",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["predict_target"] == "skill"
    assert data["subject_ref"] == "SK-grader"
    by_id = {row["example_id"]: row for row in data["results"]}
    assert "Follow the grading rubric strictly." in by_id["EX-1"]["prediction"]


def test_run_evaluation_skill_unknown_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-eval", "predict_target": "skill", "subject_ref": "SK-nope"},
    )
    assert resp.status_code == 404


def test_run_evaluation_workflow_target(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)

    # Stub the compiled-workflow predict (the real compile+execute path is covered
    # by the workflow runtime/promoter suites); this asserts the eval-route wiring.
    # ``identity`` is the fourth argument: the builder now resolves the version's
    # parent workflow through the caller's visibility, so an unscoped version id
    # can no longer bind another project's managed files.
    def fake_build_workflow_predict(_session, version_id, _config, _identity=None):  # type: ignore[no-untyped-def]
        def predict(inputs):  # type: ignore[no-untyped-def]
            return f"workflow {version_id} ran on {inputs.get('question', '')}"

        return predict

    monkeypatch.setattr(evaluations_route, "_build_workflow_predict", fake_build_workflow_predict)

    resp = client.post(
        LIST_PATH,
        json={
            "dataset_id": "ED-eval",
            "predict_target": "workflow",
            "subject_ref": "WFV-1",
            "scorers": ["non_empty"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["predict_target"] == "workflow"
    assert data["subject_ref"] == "WFV-1"
    assert data["status"] == "completed"
    by_id = {row["example_id"]: row for row in data["results"]}
    assert "workflow WFV-1 ran on" in by_id["EX-1"]["prediction"]
    assert by_id["EX-1"]["scores"]["non_empty"] == 1.0


def test_run_evaluation_workflow_unknown_version_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No monkeypatch → the real _build_workflow_predict runs the version lookup.
    _seed_dataset_with_examples(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _echo_system_completion)
    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-eval", "predict_target": "workflow", "subject_ref": "WFV-missing"},
    )
    assert resp.status_code == 404


def test_run_evaluation_artifact_target_requires_subject_ref(
    client: TestClient, db_session: Session
) -> None:
    _seed_dataset_with_examples(db_session)
    resp = client.post(LIST_PATH, json={"dataset_id": "ED-eval", "predict_target": "prompt"})
    # The schema validator rejects an artifact target with no subject_ref.
    assert resp.status_code == 400
    assert "subject_ref" in resp.text
