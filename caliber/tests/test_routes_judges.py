"""Integration tests for ``/caliber/judges`` — custom LLM judges (make_judge)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.judges as judges_route
from caliber.db.models import CaliberAuditLog, CaliberJudge
from caliber.eval.judge_scorer import JudgeError
from caliber.routes.judges import (
    ALIGNMENT_PATH,
    DETAIL_PATH,
    LIST_PATH,
    TEST_RUN_PATH,
)

_GOOD = {
    "name": "tone-judge",
    "description": "Scores reply tone.",
    "instructions": "Rate whether {{ outputs }} answers {{ inputs }} politely.",
    "model": "openai:/gpt-4o-mini",
    "feedback_value_type": "bool",
    "tags": ["tone"],
}


def _seed(session: Session, **overrides: object) -> CaliberJudge:
    defaults: dict[str, object] = {
        "judge_id": "JDG-test",
        "name": "tone-judge",
        "description": "d",
        "instructions": "Rate {{ outputs }}.",
        "model": "openai:/gpt-4o-mini",
        "owner": "@sarah",
        "tags": [],
        "status": "active",
    }
    defaults.update(overrides)
    judge = CaliberJudge(**defaults)
    session.add(judge)
    session.commit()
    return judge


def test_create_judge_happy_path(client: TestClient) -> None:
    response = client.post(LIST_PATH, json=_GOOD)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["judge_id"].startswith("JDG-")
    assert data["name"] == "tone-judge"
    assert data["feedback_value_type"] == "bool"


def test_create_judge_rejects_instructions_without_template_var(client: TestClient) -> None:
    bad = {**_GOOD, "instructions": "Just say yes."}
    response = client.post(LIST_PATH, json=bad)
    assert response.status_code == 400
    assert "evaluation variable" in response.text


def test_create_judge_rejects_unknown_value_type(client: TestClient) -> None:
    bad = {**_GOOD, "feedback_value_type": "datetime"}
    response = client.post(LIST_PATH, json=bad)
    assert response.status_code == 400


def test_create_judge_409_on_duplicate_name(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.post(LIST_PATH, json=_GOOD)
    assert response.status_code == 409


def test_list_judges_filters_status(client: TestClient, db_session: Session) -> None:
    _seed(db_session, judge_id="JDG-a", name="active-j", status="active")
    _seed(db_session, judge_id="JDG-b", name="archived-j", status="archived")
    names = {j["name"] for j in client.get(LIST_PATH).json()["data"]}
    assert names == {"active-j"}
    names = {j["name"] for j in client.get(LIST_PATH, params={"status": "all"}).json()["data"]}
    assert names == {"active-j", "archived-j"}


def test_get_judge_404(client: TestClient) -> None:
    assert client.get(DETAIL_PATH.replace("{judge_id}", "JDG-missing")).status_code == 404


def test_get_judge_scoped_to_visibility(client: TestClient, db_session: Session) -> None:
    # A project-scoped judge owned by another user, in another project.
    _seed(
        db_session,
        judge_id="JDG-priv",
        name="private-judge",
        owner="@other",
        project_id="proj-x",
        visibility="project",
    )
    detail = DETAIL_PATH.replace("{judge_id}", "JDG-priv")
    # Admin (default test user) sees everything.
    assert client.get(detail).status_code == 200
    # A different non-admin user must NOT be able to read it by id — 404, not 403,
    # so existence isn't leaked (the detail GET now goes through get_visible).
    resp = client.get(detail, headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 404


def test_update_judge_archive_and_audit(client: TestClient, db_session: Session) -> None:
    _seed(db_session, judge_id="JDG-1")
    response = client.patch(DETAIL_PATH.replace("{judge_id}", "JDG-1"), json={"status": "archived"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"
    actions = [
        row.action
        for row in db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "JDG-1")
        )
        .scalars()
        .all()
    ]
    assert "update_judge" in actions


def test_update_judge_rejects_bad_instructions(client: TestClient, db_session: Session) -> None:
    _seed(db_session, judge_id="JDG-1")
    response = client.patch(
        DETAIL_PATH.replace("{judge_id}", "JDG-1"),
        json={"instructions": "no variables here"},
    )
    assert response.status_code == 400


# --- "Try it" playground (POST /judges/{id}/test-run) -----------------------


class _FakeFeedback:
    def __init__(self, value: object, rationale: str | None = None) -> None:
        self.value = value
        self.rationale = rationale


def test_test_run_judge_returns_score(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, judge_id="JDG-try")
    captured: dict[str, object] = {}

    def fake_build_judge(name, instructions, *, model=None, feedback_value_type=None):  # type: ignore[no-untyped-def]
        def judge(**kwargs: object) -> _FakeFeedback:
            captured.update(kwargs)
            return _FakeFeedback(value=0.8, rationale="mostly grounded")

        return judge

    monkeypatch.setattr(judges_route, "build_judge", fake_build_judge)
    resp = client.post(
        TEST_RUN_PATH.replace("{judge_id}", "JDG-try"),
        json={"inputs": {"q": "hi"}, "outputs": "hello there", "expectations": {"a": "greet"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["score"] == 0.8
    assert data["rationale"] == "mostly grounded"
    # The sample fields were threaded into the judge call.
    assert captured["outputs"] == "hello there"
    assert captured["inputs"] == {"q": "hi"}


def test_test_run_judge_unknown_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = client.post(
        TEST_RUN_PATH.replace("{judge_id}", "JDG-missing"),
        json={"outputs": "x"},
    )
    assert resp.status_code == 404


def test_test_run_judge_502_on_build_failure(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, judge_id="JDG-broken")

    def boom(*_a: object, **_kw: object) -> object:
        raise JudgeError("mlflow.genai unavailable")

    monkeypatch.setattr(judges_route, "build_judge", boom)
    resp = client.post(
        TEST_RUN_PATH.replace("{judge_id}", "JDG-broken"),
        json={"outputs": "x"},
    )
    assert resp.status_code == 502


# --- Human alignment (judge vs human labels: agreement + Cohen's kappa) ------


def test_alignment_reports_agreement_and_kappa(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, judge_id="JDG-align")

    def fake_build_judge(name, instructions, *, model=None, feedback_value_type=None):  # type: ignore[no-untyped-def]
        # Judge scores 1.0 when the output contains "good", else 0.0.
        def judge(**kwargs: object) -> _FakeFeedback:
            return _FakeFeedback(value="good" in str(kwargs.get("outputs", "")).lower())

        return judge

    monkeypatch.setattr(judges_route, "build_judge", fake_build_judge)
    resp = client.post(
        ALIGNMENT_PATH.replace("{judge_id}", "JDG-align"),
        json={
            "examples": [
                {"outputs": "good answer", "label": True},  # judge True, human True → agree
                {"outputs": "good but wrong", "label": False},  # judge True, human False → disagree
                {"outputs": "bad answer", "label": False},  # judge False, human False → agree
                {"outputs": "terrible", "label": False},  # judge False, human False → agree
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["n"] == 4
    assert data["scored"] == 4
    assert data["agreement_rate"] == 0.75  # 3 of 4 agree
    assert -1.0 <= data["cohen_kappa"] <= 1.0
    assert data["confusion"]["false_pos"] == 1
    assert len(data["per_example"]) == 4


def test_alignment_unknown_judge_404(client: TestClient) -> None:
    resp = client.post(
        ALIGNMENT_PATH.replace("{judge_id}", "JDG-missing"),
        json={"examples": [{"outputs": "x", "label": True}]},
    )
    assert resp.status_code == 404


def test_alignment_requires_examples(client: TestClient, db_session: Session) -> None:
    _seed(db_session, judge_id="JDG-align2")
    resp = client.post(
        ALIGNMENT_PATH.replace("{judge_id}", "JDG-align2"),
        json={"examples": []},
    )
    assert resp.status_code == 400
