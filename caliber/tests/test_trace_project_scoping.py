"""Traces are scoped to the projects a caller can see (alpha gap P1-15, EGI B11 part 1).

MLflow files traces by experiment and knows nothing of CALIBER projects, so the observability routes
used to show every project's traces to any signed-in user. An experiment now belongs to the agents (and
workflows with a ``default_experiment_id``) whose visibility the caller can pass; any other experiment
is hidden from everyone but an admin. A trace the caller cannot see is a 404 worded exactly like a
missing trace, so a foreign id is not confirmed to exist.

The tests seed two projects with one agent each (``exp-A``, ``exp-B``), a public agent, a private
agent, a workflow with a default experiment, and an experiment owned by nobody, and put a fake MLflow
behind the routes that records what it was asked to search and what feedback it was asked to write.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
)
from caliber.observability.trace_access import is_visible, visible_experiment_ids

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OBS = PREFIX + "/observability"
GATEWAY_USAGE = PREFIX + "/gateway/usage"

PROJECT_A = "P-trace-A"
PROJECT_B = "P-trace-B"
#: Not in the suite's admin list (``conftest._PERMISSIVE_TEST_USERS``), so they have no admin scope.
ALICE = "@alice-trace"
BOB = "@bob-trace"

#: experiment id -> who may see its traces
ALICE_VISIBLE = {"exp-A", "exp-wf-A", "exp-public", "exp-alice-private"}
BOB_VISIBLE = {"exp-B", "exp-public"}
ALL_EXPERIMENTS = ["exp-A", "exp-B", "exp-wf-A", "exp-public", "exp-alice-private", "exp-orphan"]


def _agent(
    agent_id: str, experiment_id: str, owner: str, project: str | None, visibility: str
) -> Any:
    return CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id=experiment_id,
        name=agent_id,
        owner=owner,
        project_id=project,
        visibility=visibility,
        artifact_types=["prompt"],
        optimizer_config={},
    )


@pytest.fixture
def world(db_session: Session) -> None:
    """Two projects, their members, and the experiments each caller may see."""
    db_session.add_all(
        [
            CaliberProject(project_id=PROJECT_A, name="Trace project A", owner="@test"),
            CaliberProject(project_id=PROJECT_B, name="Trace project B", owner="@test"),
        ]
    )
    db_session.flush()
    for member, user, project in (("M-alice", ALICE, PROJECT_A), ("M-bob", BOB, PROJECT_B)):
        db_session.add(
            CaliberProjectMember(
                member_id=member,
                project_id=project,
                user_id=user,
                role="viewer",
                created_by="@test",
            )
        )
    db_session.add_all(
        [
            _agent("agent-a", "exp-A", "@test", PROJECT_A, "project"),
            _agent("agent-b", "exp-B", "@test", PROJECT_B, "project"),
            _agent("agent-public", "exp-public", "@test", None, "public"),
            _agent("agent-alice", "exp-alice-private", ALICE, None, "user"),
            CaliberWorkflow(
                workflow_id="wf-a",
                name="workflow a",
                owner="@test",
                project_id=PROJECT_A,
                visibility="project",
                default_experiment_id="exp-wf-A",
            ),
            CaliberWorkflow(  # a workflow with no default experiment claims nothing
                workflow_id="wf-none",
                name="workflow without experiment",
                owner="@test",
                project_id=PROJECT_A,
                visibility="project",
            ),
        ]
    )
    db_session.commit()


def _identity(user: str, project: str | None, *scopes: str) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user, scopes=frozenset(scopes or {SCOPE_VIEWER}), active_project_id=project
    )


# ---------------------------------------------------------------- visible_experiment_ids


def test_visible_experiments_for_project_user(db_session: Session, world: None) -> None:
    assert visible_experiment_ids(db_session, _identity(ALICE, PROJECT_A)) == ALICE_VISIBLE
    assert visible_experiment_ids(db_session, _identity(BOB, PROJECT_B)) == BOB_VISIBLE


def test_admin_sees_all(db_session: Session, world: None) -> None:
    assert visible_experiment_ids(db_session, _identity("@root", None, SCOPE_ADMIN)) is None


def test_without_an_active_project_only_public_and_personal_experiments_are_visible(
    db_session: Session, world: None
) -> None:
    found = visible_experiment_ids(db_session, _identity(ALICE, None))
    assert found == {"exp-public", "exp-alice-private"}


def test_naming_a_project_one_is_not_a_member_of_grants_nothing(
    db_session: Session, world: None
) -> None:
    assert visible_experiment_ids(db_session, _identity(ALICE, PROJECT_B)) == {
        "exp-public",
        "exp-alice-private",
    }


def test_a_removed_membership_hides_the_project_experiments(
    db_session: Session, world: None
) -> None:
    membership = db_session.get(CaliberProjectMember, "M-alice")
    assert membership is not None
    membership.status = "removed"
    db_session.commit()
    assert visible_experiment_ids(db_session, _identity(ALICE, PROJECT_A)) == {
        "exp-public",
        "exp-alice-private",
    }


def test_an_experiment_nobody_owns_is_visible_to_no_non_admin(
    db_session: Session, world: None
) -> None:
    for user, project in ((ALICE, PROJECT_A), (BOB, PROJECT_B), ("@stranger", None)):
        assert "exp-orphan" not in (
            visible_experiment_ids(db_session, _identity(user, project)) or set()
        )


def test_is_visible() -> None:
    assert is_visible("anything", None)
    assert is_visible(None, None)
    assert is_visible("exp-A", {"exp-A"})
    assert not is_visible("exp-B", {"exp-A"})
    assert not is_visible(None, {"exp-A"}), "a trace with no experiment is not claimed by anyone"
    assert not is_visible("exp-A", set())


# ---------------------------------------------------------------- the routes


def _trace(trace_id: str, experiment_id: str, *, ts: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            state=SimpleNamespace(name="OK"),
            request_time=ts,
            execution_duration=10,
            request_preview="",
            response_preview="",
            tags={},
            trace_metadata={},
            trace_location=SimpleNamespace(
                mlflow_experiment=SimpleNamespace(experiment_id=experiment_id)
            ),
        ),
        data=SimpleNamespace(spans=[]),
    )


class FakeMlflow:
    """Records what the routes ask MLflow for and what they write to it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, honours_locations: bool = True) -> None:
        import mlflow

        self.searched: list[list[str]] = []
        self.feedback: list[str] = []
        self.traces = {
            "tr-A": _trace("tr-A", "exp-A"),
            "tr-B": _trace("tr-B", "exp-B"),
            "tr-wf": _trace("tr-wf", "exp-wf-A"),
            "tr-public": _trace("tr-public", "exp-public"),
            "tr-orphan": _trace("tr-orphan", "exp-orphan"),
        }
        self._honours = honours_locations
        monkeypatch.setattr(
            mlflow,
            "search_experiments",
            lambda max_results=100: [
                SimpleNamespace(experiment_id=e, name=f"name-{e}") for e in ALL_EXPERIMENTS
            ],
        )
        monkeypatch.setattr(mlflow, "search_traces", self._search)
        monkeypatch.setattr(
            mlflow, "get_trace", lambda trace_id, silent=False: self.traces.get(trace_id)
        )
        monkeypatch.setattr(
            mlflow, "log_feedback", lambda **kwargs: self.feedback.append(kwargs["trace_id"])
        )

    def _search(self, *, locations: list[str], **_: object) -> list[SimpleNamespace]:
        self.searched.append(list(locations))
        every = list(self.traces.values())
        if not self._honours:
            return every
        return [
            t for t in every if t.info.trace_location.mlflow_experiment.experiment_id in locations
        ]


def _as(user: str, project: str | None) -> dict[str, str]:
    headers = {"X-CALIBER-User": user}
    if project:
        headers["X-CALIBER-Project"] = project
    return headers


def _ids(response: Any) -> set[str]:
    return {t["trace_id"] for t in response.json()["data"]["traces"]}


def test_list_excludes_foreign_project(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mlflow = FakeMlflow(monkeypatch)
    response = client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A))
    assert response.status_code == 200, response.text
    assert _ids(response) == {"tr-A", "tr-wf", "tr-public"}
    searched = {experiment for call in mlflow.searched for experiment in call}
    assert searched <= ALICE_VISIBLE, (
        f"the search was not limited to visible experiments: {searched}"
    )
    assert {"exp-B", "exp-orphan"}.isdisjoint(searched)

    other = client.get(f"{OBS}/traces", headers=_as(BOB, PROJECT_B))
    assert _ids(other) == {"tr-B", "tr-public"}


def test_list_filters_what_the_store_returns_even_when_it_ignores_the_search_locations(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch, honours_locations=False)
    response = client.get(f"{OBS}/traces", headers=_as(BOB, PROJECT_B))
    assert _ids(response) == {"tr-B", "tr-public"}


def test_admin_sees_every_project(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    response = client.get(f"{OBS}/traces")  # the suite's default user is an admin
    assert _ids(response) == {"tr-A", "tr-B", "tr-wf", "tr-public", "tr-orphan"}


def test_an_explicit_foreign_experiment_filter_is_404_not_503(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    response = client.get(
        f"{OBS}/traces", params={"experiment_id": "exp-B"}, headers=_as(ALICE, PROJECT_A)
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "experiment 'exp-B' not found"
    own = client.get(
        f"{OBS}/traces", params={"experiment_id": "exp-A"}, headers=_as(ALICE, PROJECT_A)
    )
    assert _ids(own) == {"tr-A"}


def test_get_foreign_trace_404(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    foreign = client.get(f"{OBS}/traces/tr-B", headers=_as(ALICE, PROJECT_A))
    missing = client.get(f"{OBS}/traces/tr-missing", headers=_as(ALICE, PROJECT_A))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"] == "trace 'tr-B' not found"
    assert set(foreign.json()) == set(missing.json()) == {"detail", "status_code"}, (
        "no trace fields leak"
    )

    assert client.get(f"{OBS}/traces/tr-A", headers=_as(ALICE, PROJECT_A)).status_code == 200
    assert client.get(f"{OBS}/traces/tr-orphan", headers=_as(ALICE, PROJECT_A)).status_code == 404
    assert client.get(f"{OBS}/traces/tr-B").status_code == 200, "an admin may open any trace"


def test_feedback_on_foreign_trace_404(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    mlflow = FakeMlflow(monkeypatch)
    denied = client.post(
        f"{OBS}/traces/tr-B/feedback", json={"value": True}, headers=_as(ALICE, PROJECT_A)
    )
    assert denied.status_code == 404, denied.text
    assert mlflow.feedback == [], "nothing was written to MLflow for a trace the caller cannot see"
    audited = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "trace.feedback")
        )
        .scalars()
        .all()
    )
    assert audited == []

    allowed = client.post(
        f"{OBS}/traces/tr-A/feedback", json={"value": True}, headers=_as(ALICE, PROJECT_A)
    )
    assert allowed.status_code == 200, allowed.text
    assert mlflow.feedback == ["tr-A"]
    admin = client.post(f"{OBS}/traces/tr-B/feedback", json={"value": 1})
    assert admin.status_code == 200 and mlflow.feedback == ["tr-A", "tr-B"]


def test_feedback_for_an_unknown_trace_by_a_non_admin_is_404(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mlflow = FakeMlflow(monkeypatch)
    response = client.post(
        f"{OBS}/traces/tr-missing/feedback", json={"value": True}, headers=_as(ALICE, PROJECT_A)
    )
    assert response.status_code == 404
    assert mlflow.feedback == []


def test_a_trace_store_outage_is_503_not_a_scoping_answer(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    def down(*_a: object, **_k: object) -> object:
        raise RuntimeError("connection refused")

    FakeMlflow(monkeypatch)
    monkeypatch.setattr(mlflow, "get_trace", down)
    response = client.post(
        f"{OBS}/traces/tr-A/feedback", json={"value": True}, headers=_as(ALICE, PROJECT_A)
    )
    assert response.status_code == 503, response.text


def test_metrics_exclude_foreign_project(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch, honours_locations=False)
    assert (
        client.get(f"{OBS}/metrics", headers=_as(ALICE, PROJECT_A)).json()["data"]["totals"][
            "count"
        ]
        == 3
    )
    assert (
        client.get(f"{OBS}/metrics", headers=_as(BOB, PROJECT_B)).json()["data"]["totals"]["count"]
        == 2
    )
    assert client.get(f"{OBS}/metrics").json()["data"]["totals"]["count"] == 5

    foreign = client.get(
        f"{OBS}/metrics", params={"experiment_id": "exp-B"}, headers=_as(ALICE, PROJECT_A)
    )
    assert foreign.status_code == 404


def test_the_experiment_picker_lists_only_visible_experiments(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    listed = client.get(f"{OBS}/experiments", headers=_as(ALICE, PROJECT_A)).json()["data"][
        "experiments"
    ]
    assert {e["experiment_id"] for e in listed} == ALICE_VISIBLE
    everything = client.get(f"{OBS}/experiments").json()["data"]["experiments"]
    assert {e["experiment_id"] for e in everything} == set(ALL_EXPERIMENTS)


def test_gateway_usage_excludes_foreign_project(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch, honours_locations=False)
    alice = client.get(GATEWAY_USAGE, headers=_as(ALICE, PROJECT_A))
    assert alice.status_code == 200, alice.text
    assert alice.json()["data"]["totals"]["count"] == 3
    assert (
        client.get(GATEWAY_USAGE, headers=_as(BOB, PROJECT_B)).json()["data"]["totals"]["count"]
        == 2
    )
    assert client.get(GATEWAY_USAGE).json()["data"]["totals"]["count"] == 5
    foreign = client.get(
        GATEWAY_USAGE, params={"experiment_id": "exp-B"}, headers=_as(ALICE, PROJECT_A)
    )
    assert foreign.status_code == 404
