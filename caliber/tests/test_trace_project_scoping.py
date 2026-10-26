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

import sys
import time
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
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberProject,
    CaliberProjectMember,
    CaliberReviewItem,
    CaliberReviewQueue,
    CaliberWorkflow,
)
from caliber.observability.trace_access import (
    ExperimentClaimConflict,
    ExperimentLookupError,
    TraceNotReadableError,
    assert_experiment_claim_allowed,
    claim_keys,
    clear_resolution_cache,
    experiment_claim_conflicts,
    is_visible,
    require_readable_trace,
    resolve_experiment,
    visible_experiment_ids,
    visible_experiments,
)

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
                role="editor",
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
    db_session.add_all(
        [
            CaliberEvalDataset(
                dataset_id="ds-a",
                name="dataset a",
                owner="@test",
                project_id=PROJECT_A,
                visibility="project",
            ),
            CaliberReviewQueue(
                queue_id="rq-a",
                name="queue a",
                owner="@test",
                project_id=PROJECT_A,
                visibility="project",
                questions=[{"key": "ok", "type": "pass_fail"}],
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


def _span(model: str, tokens: int, cost: float) -> SimpleNamespace:
    return SimpleNamespace(
        span_type="LLM",
        name="llm",
        attributes={"caliber.model": model, "caliber.tokens": tokens, "caliber.cost_usd": cost},
    )


def _trace(
    trace_id: str,
    experiment_id: str,
    *,
    ts: int = 100,
    text: str = "",
    spans: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            state=SimpleNamespace(name="OK"),
            request_time=ts,
            execution_duration=10,
            request_preview=f"request of {text}" if text else "",
            response_preview=f"response of {text}" if text else "",
            tags={},
            trace_metadata={},
            trace_location=SimpleNamespace(
                mlflow_experiment=SimpleNamespace(experiment_id=experiment_id)
            ),
        ),
        data=SimpleNamespace(spans=spans or []),
    )


class FakeMlflow:
    """Records what the routes ask MLflow for and what they write to it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        honours_locations: bool = True,
        extra_traces: dict[str, SimpleNamespace] | None = None,
        experiment_names: dict[str, str] | None = None,
    ) -> None:
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
        self.traces["tr-A"] = _trace("tr-A", "exp-A", text="project A")
        self.traces["tr-B"] = _trace(
            "tr-B", "exp-B", text="SECRET-B", spans=[_span("secret-model-b", 9000, 12.5)]
        )
        self.traces.update(extra_traces or {})
        self._by_name = dict(experiment_names or {})
        self._honours = honours_locations
        monkeypatch.setattr(
            mlflow,
            "search_experiments",
            lambda max_results=100: [
                SimpleNamespace(experiment_id=e, name=f"name-{e}")
                for e in [*ALL_EXPERIMENTS, *self._by_name.values()]
            ],
        )
        monkeypatch.setattr(
            mlflow,
            "get_experiment_by_name",
            lambda name: (
                SimpleNamespace(experiment_id=self._by_name[name])
                if name in self._by_name
                else None
            ),
        )
        ids = [*ALL_EXPERIMENTS, *self._by_name.values()]

        def get_experiment(experiment_id: str) -> SimpleNamespace:
            from mlflow.exceptions import MlflowException
            from mlflow.protos.databricks_pb2 import RESOURCE_DOES_NOT_EXIST

            if experiment_id not in ids:
                raise MlflowException(
                    f"No Experiment with id={experiment_id} exists",
                    error_code=RESOURCE_DOES_NOT_EXIST,
                )
            return SimpleNamespace(experiment_id=experiment_id)

        monkeypatch.setattr(mlflow, "get_experiment", get_experiment)
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


def test_metrics_only_search_visible_experiments(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a store that honours the search, the narrowing itself must hold, not just the post-filter."""
    mlflow = FakeMlflow(monkeypatch)
    response = client.get(f"{OBS}/metrics", headers=_as(ALICE, PROJECT_A))
    assert response.json()["data"]["totals"]["count"] == 3
    searched = {experiment for call in mlflow.searched for experiment in call}
    assert searched == ALICE_VISIBLE & set(ALL_EXPERIMENTS)


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


def test_gateway_usage_only_searches_visible_experiments(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mlflow = FakeMlflow(monkeypatch)
    assert client.get(GATEWAY_USAGE, headers=_as(ALICE, PROJECT_A)).status_code == 200
    searched = {experiment for call in mlflow.searched for experiment in call}
    assert searched == ALICE_VISIBLE & set(ALL_EXPERIMENTS)


def test_gateway_usage_by_model_never_counts_a_foreign_trace(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project B's trace carries a model, tokens and cost: none of it may reach A's usage tab."""
    FakeMlflow(monkeypatch, honours_locations=False)
    alice = client.get(GATEWAY_USAGE, headers=_as(ALICE, PROJECT_A)).json()["data"]
    assert alice["by_model"] == []
    assert alice["totals"]["cost_usd"] == 0
    admin = client.get(GATEWAY_USAGE).json()["data"]
    assert [row["model"] for row in admin["by_model"]] == ["secret-model-b"]


# ---------------------------------------------------------------- ownership cannot be asserted


def _grant_operator(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.operator_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(
        update={"operator_users": ",".join(sorted(existing))}
    )


WORKFLOWS = PREFIX + "/workflows"
AGENTS = PREFIX + "/agents"


def test_a_workflow_cannot_claim_another_projects_experiment_on_create(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's proof of concept: naming exp-B as a default experiment used to grant its traces."""
    FakeMlflow(monkeypatch)
    _grant_operator(client, ALICE)
    grab = client.post(
        WORKFLOWS,
        json={"name": "grab", "default_experiment_id": "exp-B"},
        headers=_as(ALICE, PROJECT_A),
    )
    assert grab.status_code == 409, grab.text
    assert "already claimed" in grab.json()["detail"]
    assert client.get(f"{OBS}/traces/tr-B", headers=_as(ALICE, PROJECT_A)).status_code == 404
    assert "tr-B" not in _ids(client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)))


def test_a_workflow_cannot_claim_another_projects_experiment_on_update(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    _grant_operator(client, ALICE)
    created = client.post(WORKFLOWS, json={"name": "mine"}, headers=_as(ALICE, PROJECT_A))
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    grab = client.patch(
        f"{WORKFLOWS}/{workflow_id}",
        json={"default_experiment_id": "exp-B"},
        headers=_as(ALICE, PROJECT_A),
    )
    assert grab.status_code == 409, grab.text
    assert client.get(f"{OBS}/traces/tr-B", headers=_as(ALICE, PROJECT_A)).status_code == 404


def test_a_workflow_may_share_its_own_projects_experiment(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    _grant_operator(client, ALICE)
    shared = client.post(
        WORKFLOWS,
        json={"name": "shares", "default_experiment_id": "exp-A"},
        headers=_as(ALICE, PROJECT_A),
    )
    assert shared.status_code == 201, shared.text
    workflow_id = shared.json()["data"]["workflow_id"]
    again = client.patch(
        f"{WORKFLOWS}/{workflow_id}",
        json={"default_experiment_id": "exp-wf-A"},
        headers=_as(ALICE, PROJECT_A),
    )
    assert again.status_code == 200, again.text


def test_an_agent_cannot_register_an_experiment_a_foreign_workflow_holds(
    client: TestClient, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    _grant_operator(client, BOB)
    grab = client.post(
        AGENTS,
        json={"agent_id": "bob-grab", "experiment_id": "exp-wf-A", "name": "grab", "owner": BOB},
        headers=_as(BOB, PROJECT_B),
    )
    assert grab.status_code == 409, grab.text
    assert "exp-wf-A" not in (visible_experiment_ids_for(client, BOB, PROJECT_B) or set())


def visible_experiment_ids_for(client: TestClient, user: str, project: str) -> set[str] | None:
    factory = client.app.state.session_factory
    with factory() as session:
        return visible_experiment_ids(session, _identity(user, project))


@pytest.mark.parametrize(
    ("mine", "held", "conflicts"),
    [
        (("project", PROJECT_A, "@x"), ("project", PROJECT_A, "@y"), False),  # same project
        (("project", PROJECT_A, "@x"), ("project", PROJECT_B, "@x"), True),  # another project
        (("user", None, "@x"), ("user", None, "@x"), False),  # same user
        (("user", None, "@x"), ("user", None, "@y"), True),  # another user
        (("project", PROJECT_A, "@x"), ("public", None, "@y"), True),  # public holds it
        (("public", None, "@x"), ("project", PROJECT_A, "@y"), True),
        (("project", PROJECT_A, "@x"), ("user", None, "@x"), True),  # personal vs project
    ],
)
def test_claim_scopes(
    db_session: Session,
    mine: tuple[str, str | None, str],
    held: tuple[str, str | None, str],
    conflicts: bool,
) -> None:
    db_session.add(
        CaliberAgentConfig(
            agent_id="holder",
            experiment_id="exp-claimed",
            name="holder",
            owner=held[2],
            project_id=held[1],
            visibility=held[0],
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db_session.commit()
    visibility, project_id, owner = mine
    call = lambda: assert_experiment_claim_allowed(
        db_session, "exp-claimed", visibility=visibility, project_id=project_id, owner=owner
    )
    if conflicts:
        with pytest.raises(ExperimentClaimConflict, match="already claimed"):
            call()
    else:
        call()


def test_a_row_does_not_conflict_with_itself_and_nothing_is_not_a_claim(
    db_session: Session, world: None
) -> None:
    assert_experiment_claim_allowed(
        db_session,
        "exp-A",
        visibility="project",
        project_id=PROJECT_A,
        owner="@test",
        agent_id="agent-a",
    )
    assert_experiment_claim_allowed(
        db_session, None, visibility="project", project_id=PROJECT_B, owner="@test"
    )
    assert_experiment_claim_allowed(
        db_session, "", visibility="project", project_id=PROJECT_B, owner="@test"
    )
    with pytest.raises(ExperimentClaimConflict):  # exp-A is project A's, and this is not agent-a
        assert_experiment_claim_allowed(
            db_session, "exp-A", visibility="project", project_id=PROJECT_B, owner="@test"
        )


# ---------------------------------------------------------------- an agent registered by experiment NAME


def test_an_agent_registered_by_experiment_name_keeps_its_traces(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents may name their experiment; a trace carries its numeric id, so the name has to resolve."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="agent-named",
            experiment_id="team-a-experiment",
            name="named",
            owner="@test",
            project_id=PROJECT_A,
            visibility="project",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db_session.commit()
    FakeMlflow(
        monkeypatch,
        extra_traces={"tr-named": _trace("tr-named", "42")},
        experiment_names={"team-a-experiment": "42"},
    )
    assert client.get(f"{OBS}/traces/tr-named", headers=_as(ALICE, PROJECT_A)).status_code == 200
    assert "tr-named" in _ids(client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)))
    assert client.get(f"{OBS}/traces/tr-named", headers=_as(BOB, PROJECT_B)).status_code == 404


# ---------------------------------------------------------------- other places a trace id is accepted


def test_from_trace_never_copies_a_foreign_trace(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    _grant_operator(client, ALICE)
    url = f"{PREFIX}/eval-datasets/ds-a/examples/from-trace"

    refused = client.post(url, json={"trace_id": "tr-B"}, headers=_as(ALICE, PROJECT_A))
    assert refused.status_code == 404, refused.text
    assert "SECRET-B" not in refused.text
    missing = client.post(url, json={"trace_id": "tr-nope"}, headers=_as(ALICE, PROJECT_A))
    assert missing.status_code == 404
    assert missing.json()["detail"].split("'")[2] == refused.json()["detail"].split("'")[2]

    # With an explicit input the example is saved, but nothing of the foreign trace is copied.
    override = client.post(
        url, json={"trace_id": "tr-B", "input": {"q": "mine"}}, headers=_as(ALICE, PROJECT_A)
    )
    assert override.status_code == 201, override.text
    stored = db_session.execute(select(CaliberEvalDatasetExample)).scalars().all()
    assert stored and all("SECRET-B" not in f"{row.input} {row.expected}" for row in stored)

    own = client.post(url, json={"trace_id": "tr-A"}, headers=_as(ALICE, PROJECT_A))
    assert own.status_code == 201, own.text
    own_rows = db_session.execute(select(CaliberEvalDatasetExample)).scalars().all()
    assert any("project A" in f"{row.input}" for row in own_rows)


def test_review_queue_add_items_refuses_a_foreign_or_unknown_trace(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    _grant_operator(client, ALICE)
    url = f"{PREFIX}/review-queues/rq-a/items"

    foreign = client.post(url, json={"trace_ids": ["tr-B"]}, headers=_as(ALICE, PROJECT_A))
    assert foreign.status_code == 404, foreign.text
    unknown = client.post(url, json={"trace_ids": ["tr-nope"]}, headers=_as(ALICE, PROJECT_A))
    assert unknown.status_code == 404
    assert db_session.execute(select(CaliberReviewItem)).scalars().all() == []

    own = client.post(url, json={"trace_ids": ["tr-A"]}, headers=_as(ALICE, PROJECT_A))
    assert own.status_code == 201, own.text
    assert [i.trace_id for i in db_session.execute(select(CaliberReviewItem)).scalars()] == ["tr-A"]


def test_review_queue_add_items_for_an_admin_is_unchanged(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeMlflow(monkeypatch)
    response = client.post(f"{PREFIX}/review-queues/rq-a/items", json={"trace_ids": ["tr-B"]})
    assert response.status_code == 201, response.text


def test_the_runtime_enqueue_path_does_not_verify_traces(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow runtime enqueues its own run's trace, which may not be flushed yet."""
    from caliber.routes.review_queues import add_review_items_records

    FakeMlflow(monkeypatch)
    created = add_review_items_records(
        db_session,
        queue_id="rq-a",
        trace_ids=["tr-not-flushed-yet"],
        experiment_id=None,
        assigned_to=None,
        actor="workflow:wf-a",
        identity=_identity(ALICE, PROJECT_A),
    )
    assert [item.trace_id for item in created] == ["tr-not-flushed-yet"]
    with pytest.raises(TraceNotReadableError):
        add_review_items_records(
            db_session,
            queue_id="rq-a",
            trace_ids=["tr-not-flushed-either"],
            experiment_id=None,
            assigned_to=None,
            actor=ALICE,
            identity=_identity(ALICE, PROJECT_A),
            verify_traces=True,
        )


def test_require_readable_trace(world: None, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMlflow(monkeypatch)
    require_readable_trace("tr-B", None)  # an admin: no lookup
    require_readable_trace("tr-A", {"exp-A"})
    with pytest.raises(TraceNotReadableError, match="'tr-B' not found"):
        require_readable_trace("tr-B", {"exp-A"})
    with pytest.raises(TraceNotReadableError, match="'tr-nope' not found"):
        require_readable_trace("tr-nope", {"exp-A"})


# ---------------------------------------------------------------- canonical identity: one experiment, any alias

#: Project B's experiment is known to MLflow as id "7" and as the name "billing-exp"; project A never owned it.
BILLING = "billing-exp"
BILLING_ID = "7"
SECOND_NAME = "billing-experiment-v2"
BILLING_TRACE = {"tr-billing": _trace("tr-billing", BILLING_ID, text="SECRET-BILLING")}


def _billing_world(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> FakeMlflow:
    return FakeMlflow(
        monkeypatch,
        extra_traces=BILLING_TRACE,
        experiment_names={BILLING: BILLING_ID, SECOND_NAME: BILLING_ID},
        **kwargs,
    )


def _bind_agent(
    db_session: Session, ref: str, project: str, agent_id: str = "agent-billing"
) -> None:
    db_session.add(_agent(agent_id, ref, "@test", project, "project"))
    db_session.commit()


def test_resolve_experiment_maps_names_and_ids_to_one_canonical_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _billing_world(monkeypatch)
    assert resolve_experiment(BILLING) == BILLING_ID
    assert resolve_experiment(SECOND_NAME) == BILLING_ID
    assert resolve_experiment(BILLING_ID) == BILLING_ID
    assert resolve_experiment("not-an-experiment") is None
    assert resolve_experiment("999") is None, "a numeric id MLflow does not know"
    assert claim_keys(BILLING) == {BILLING, BILLING_ID}
    assert claim_keys("not-an-experiment") == {"not-an-experiment"}


@pytest.mark.parametrize(
    ("held_by", "claimed_as"),
    [
        (BILLING, BILLING_ID),  # held by name, claimed by id
        (BILLING_ID, BILLING),  # held by id, claimed by name
        (BILLING, SECOND_NAME),  # held by one name, claimed by another name of the same experiment
        (BILLING_ID, BILLING_ID),  # the same text, for completeness
    ],
)
def test_an_alias_of_another_projects_experiment_cannot_be_claimed(
    db_session: Session,
    world: None,
    monkeypatch: pytest.MonkeyPatch,
    held_by: str,
    claimed_as: str,
) -> None:
    _billing_world(monkeypatch)
    _bind_agent(db_session, held_by, PROJECT_B)
    with pytest.raises(ExperimentClaimConflict, match="already claimed"):
        assert_experiment_claim_allowed(
            db_session, claimed_as, visibility="project", project_id=PROJECT_A, owner="@test"
        )


def test_the_same_project_may_use_any_alias_of_its_own_experiment(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _bind_agent(db_session, BILLING, PROJECT_B)
    for alias in (BILLING, BILLING_ID, SECOND_NAME):
        assert_experiment_claim_allowed(
            db_session, alias, visibility="project", project_id=PROJECT_B, owner="@test"
        )


def test_a_workflow_cannot_claim_the_id_of_an_experiment_named_by_another_project(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route-level proof, through create and update, as an ordinary editor of project A."""
    _billing_world(monkeypatch)
    _bind_agent(db_session, BILLING, PROJECT_B)
    _grant_operator(client, ALICE)
    grab = client.post(
        WORKFLOWS,
        json={"name": "grab", "default_experiment_id": BILLING_ID},
        headers=_as(ALICE, PROJECT_A),
    )
    assert grab.status_code == 409, grab.text
    created = client.post(WORKFLOWS, json={"name": "mine"}, headers=_as(ALICE, PROJECT_A))
    workflow_id = created.json()["data"]["workflow_id"]
    for alias in (BILLING_ID, SECOND_NAME):
        update = client.patch(
            f"{WORKFLOWS}/{workflow_id}",
            json={"default_experiment_id": alias},
            headers=_as(ALICE, PROJECT_A),
        )
        assert update.status_code == 409, update.text
    assert client.get(f"{OBS}/traces/tr-billing", headers=_as(ALICE, PROJECT_A)).status_code == 404
    assert "tr-billing" not in _ids(client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)))


def test_an_agent_cannot_register_an_alias_of_another_projects_experiment(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _bind_agent(db_session, BILLING_ID, PROJECT_B)
    _grant_operator(client, ALICE)
    grab = client.post(
        AGENTS,
        json={"agent_id": "alice-grab", "experiment_id": BILLING, "name": "grab", "owner": ALICE},
        headers=_as(ALICE, PROJECT_A),
    )
    assert grab.status_code == 409, grab.text


def test_an_owner_sees_its_experiment_under_every_alias_and_nobody_else_does(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _bind_agent(db_session, BILLING, PROJECT_B)
    assert BILLING_ID in (visible_experiments(db_session, _identity(BOB, PROJECT_B)) or set())
    assert BILLING_ID not in (visible_experiments(db_session, _identity(ALICE, PROJECT_A)) or set())
    assert "tr-billing" in _ids(client.get(f"{OBS}/traces", headers=_as(BOB, PROJECT_B)))
    assert "tr-billing" not in _ids(client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)))
    assert client.get(f"{OBS}/traces/tr-billing", headers=_as(BOB, PROJECT_B)).status_code == 200


def test_a_resolution_is_cached_for_a_short_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    calls: list[str] = []

    def lookup(name: str) -> SimpleNamespace:
        calls.append(name)
        return SimpleNamespace(experiment_id="3")

    monkeypatch.setattr(mlflow, "get_experiment_by_name", lookup)
    assert resolve_experiment("cached") == "3"
    assert resolve_experiment("cached") == "3"
    assert calls == ["cached"], "the second answer came from the cache"
    clear_resolution_cache()
    assert resolve_experiment("cached") == "3"
    assert calls == ["cached", "cached"]
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 3600)
    assert resolve_experiment("cached") == "3"
    assert len(calls) == 3, "an expired entry is looked up again"


# ---------------------------------------------------------------- legacy conflicting claims are quarantined


def _legacy_conflict(db_session: Session) -> None:
    """Two projects already claim one experiment, one by name and one by id (data from before the check)."""
    _bind_agent(db_session, BILLING, PROJECT_B, "agent-b-billing")
    db_session.add(_agent("agent-a-billing", BILLING_ID, "@test", PROJECT_A, "project"))
    db_session.commit()


def test_legacy_conflicting_claims_hide_the_experiment_from_both_projects(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _legacy_conflict(db_session)
    for user, project in ((ALICE, PROJECT_A), (BOB, PROJECT_B)):
        assert BILLING_ID not in (
            visible_experiments(db_session, _identity(user, project)) or set()
        )
        assert "tr-billing" not in _ids(client.get(f"{OBS}/traces", headers=_as(user, project)))
        assert client.get(f"{OBS}/traces/tr-billing", headers=_as(user, project)).status_code == 404
    admin = {"X-CALIBER-User": "@test"}
    assert "tr-billing" in _ids(client.get(f"{OBS}/traces", headers=admin)), (
        "an admin still sees it"
    )
    # An unrelated, unconflicted experiment is unaffected.
    assert "tr-A" in _ids(client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)))


def test_the_operator_lists_the_conflicts_and_no_winner_is_picked(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _legacy_conflict(db_session)
    listed = client.get(f"{OBS}/experiment-claim-conflicts", headers={"X-CALIBER-User": "@test"})
    assert listed.status_code == 200, listed.text
    conflicts = {c["experiment"]: c for c in listed.json()["data"]["conflicts"]}
    assert set(conflicts) == {BILLING_ID}
    claims = conflicts[BILLING_ID]["claims"]
    assert {(c["claim"], c["scope"]["id"], c["agent_id"]) for c in claims} == {
        (BILLING, PROJECT_B, "agent-b-billing"),
        (BILLING_ID, PROJECT_A, "agent-a-billing"),
    }
    # Removing the claim that should not exist resolves it.
    db_session.delete(db_session.get(CaliberAgentConfig, "agent-a-billing"))
    db_session.commit()
    assert (
        client.get(f"{OBS}/experiment-claim-conflicts", headers={"X-CALIBER-User": "@test"}).json()[
            "data"
        ]["conflicts"]
        == []
    )
    assert BILLING_ID in (visible_experiments(db_session, _identity(BOB, PROJECT_B)) or set())


def test_only_an_admin_may_list_the_conflicts(client: TestClient, world: None) -> None:
    assert (
        client.get(f"{OBS}/experiment-claim-conflicts", headers=_as(ALICE, PROJECT_A)).status_code
        == 403
    )


def test_an_experiment_named_only_by_an_empty_string_is_not_a_claim(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    db_session.add(
        CaliberWorkflow(
            workflow_id="wf-empty",
            name="empty",
            owner="@test",
            project_id=PROJECT_B,
            visibility="project",
            default_experiment_id="",
        )
    )
    db_session.commit()
    assert experiment_claim_conflicts(db_session) == []
    assert_experiment_claim_allowed(
        db_session, BILLING, visibility="project", project_id=PROJECT_A, owner="@test"
    )


# ---------------------------------------------------------------- an unresolvable claim fails closed


SECRET_HOST = "http://user:pw@mlflow.internal:5000"


def _backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    def down(_ref: str) -> None:
        raise ConnectionError(f"tracking {SECRET_HOST} refused")

    monkeypatch.setattr(mlflow, "get_experiment_by_name", down)
    monkeypatch.setattr(mlflow, "get_experiment", down)
    clear_resolution_cache()


def test_a_lookup_failure_denies_every_route_and_never_leaks_the_backend_error(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _bind_agent(db_session, BILLING, PROJECT_B)
    _backend_down(monkeypatch)
    _grant_operator(client, ALICE)
    created = client.post(WORKFLOWS, json={"name": "mine"}, headers=_as(ALICE, PROJECT_A))
    workflow_id = created.json()["data"]["workflow_id"] if created.status_code == 201 else None
    attempts = [
        client.post(
            WORKFLOWS,
            json={"name": "w", "default_experiment_id": BILLING},
            headers=_as(ALICE, PROJECT_A),
        ),
        client.post(
            AGENTS,
            json={
                "agent_id": "a-down",
                "experiment_id": "fresh-exp",
                "name": "down",
                "owner": ALICE,
            },
            headers=_as(ALICE, PROJECT_A),
        ),
        client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A)),
        client.get(f"{OBS}/traces/tr-billing", headers=_as(ALICE, PROJECT_A)),
        client.get(f"{OBS}/metrics", headers=_as(ALICE, PROJECT_A)),
        client.get(f"{OBS}/experiments", headers=_as(ALICE, PROJECT_A)),
        client.get(f"{OBS}/experiment-claim-conflicts", headers={"X-CALIBER-User": "@test"}),
    ]
    if workflow_id:
        attempts.append(
            client.patch(
                f"{WORKFLOWS}/{workflow_id}",
                json={"default_experiment_id": BILLING},
                headers=_as(ALICE, PROJECT_A),
            )
        )
    for response in attempts:
        assert response.status_code == 503, (response.request.url, response.text)
        assert "mlflow.internal" not in response.text and "user:pw" not in response.text
        assert "did not answer" in response.text
    with pytest.raises(ExperimentLookupError, match="did not answer"):
        visible_experiments(db_session, _identity(ALICE, PROJECT_A))


def test_an_unresolvable_existing_claim_blocks_a_new_claim_rather_than_being_compared_as_written(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B holds ``billing-exp`` (experiment 7). With that lookup failing, project C's claim on "7" is not allowed
    on the strength of the text differing: it could be the same experiment."""
    import mlflow

    _billing_world(monkeypatch)
    real = mlflow.get_experiment_by_name
    _bind_agent(db_session, BILLING, PROJECT_B)
    assert_experiment_claim_allowed(  # healthy: an unrelated claim is allowed
        db_session, "exp-A", visibility="project", project_id=PROJECT_A, owner="@test"
    )

    def one_name_fails(name: str) -> object:
        if name == BILLING:
            raise ConnectionError("down for this one")
        return real(name)

    monkeypatch.setattr(mlflow, "get_experiment_by_name", one_name_fails)
    clear_resolution_cache()
    with pytest.raises(ExperimentLookupError):
        assert_experiment_claim_allowed(
            db_session, BILLING_ID, visibility="project", project_id=PROJECT_A, owner="@test"
        )
    with pytest.raises(ExperimentLookupError):
        experiment_claim_conflicts(db_session)


def test_the_conflict_listing_fails_closed_when_a_claim_cannot_be_resolved(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _billing_world(monkeypatch)
    _legacy_conflict(db_session)
    _backend_down(monkeypatch)
    with pytest.raises(ExperimentLookupError):
        experiment_claim_conflicts(db_session)


@pytest.mark.parametrize("failure", ["import", "mlflow-error", "other"])
def test_resolve_experiment_fails_closed(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import mlflow
    from mlflow.exceptions import MlflowException

    if failure == "import":
        monkeypatch.setitem(sys.modules, "mlflow", None)
        with pytest.raises(ExperimentLookupError, match="did not answer"):
            resolve_experiment("anything")
        return

    def broken(name: str) -> None:
        if failure == "mlflow-error":
            raise MlflowException("server error", error_code="INTERNAL_ERROR")
        raise OSError("disk gone")

    monkeypatch.setattr(mlflow, "get_experiment_by_name", broken)
    with pytest.raises(ExperimentLookupError, match="did not answer") as raised:
        resolve_experiment("x")
    assert "disk gone" not in str(raised.value) and "server error" not in str(raised.value)
    assert raised.value.__cause__ is not None, "the cause is kept for the log, not the message"


def test_a_lookup_failure_is_logged_with_its_cause(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "get_experiment_by_name", lambda name: (_ for _ in ()).throw(OSError("disk gone"))
    )
    with caplog.at_level("WARNING", logger="caliber.observability.trace_access"):
        with pytest.raises(ExperimentLookupError):
            resolve_experiment("logged")
    assert "disk gone" in caplog.text


def test_resolve_experiment_asks_the_right_question_for_a_name_and_for_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mlflow

    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mlflow, "get_experiment_by_name", lambda name: asked.append(("name", name)) or None
    )

    def get_experiment(experiment_id: str) -> SimpleNamespace:
        asked.append(("id", experiment_id))
        return SimpleNamespace(experiment_id=experiment_id)

    monkeypatch.setattr(mlflow, "get_experiment", get_experiment)
    assert resolve_experiment("7") == "7"
    assert resolve_experiment("seven") is None
    assert asked == [("id", "7"), ("name", "seven")]


# ---------------------------------------------------------------- the cache: bounded, jittered, failures remembered briefly


def test_a_failure_is_remembered_for_a_moment_then_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    calls: list[str] = []

    def down(name: str) -> None:
        calls.append(name)
        raise ConnectionError("down")

    monkeypatch.setattr(mlflow, "get_experiment_by_name", down)
    for _ in range(3):
        with pytest.raises(ExperimentLookupError):
            resolve_experiment("flapping")
    assert calls == ["flapping"], (
        "a backend that is down is probed once per window, not once per request"
    )
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 3)
    with pytest.raises(ExperimentLookupError):
        resolve_experiment("flapping")
    assert calls == ["flapping", "flapping"], "after the window it is probed again"


def test_the_cache_is_bounded_and_evicts_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    from caliber.observability import trace_access

    monkeypatch.setattr(trace_access, "_CACHE_LIMIT", 3)
    calls: list[str] = []
    monkeypatch.setattr(
        mlflow,
        "get_experiment_by_name",
        lambda name: calls.append(name) or SimpleNamespace(experiment_id="1"),
    )
    for name in ("a", "b", "c", "d"):
        resolve_experiment(name)
    assert len(trace_access._resolutions) == 3
    assert "a" not in trace_access._resolutions, "the oldest entry was evicted"
    resolve_experiment("a")
    assert calls[-1] == "a", "an evicted reference is looked up again"


def test_resolution_lifetimes_are_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.observability import trace_access

    monkeypatch.setattr(trace_access.random, "uniform", lambda low, high: high)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    trace_access._remember("hi", "1")
    monkeypatch.setattr(trace_access.random, "uniform", lambda low, high: low)
    trace_access._remember("lo", "1")
    assert trace_access._resolutions["hi"][0] - trace_access._resolutions["lo"][0] == pytest.approx(
        15.0 * 0.4
    )


# ---------------------------------------------------------------- many claims: one listing, not one call each


def _many_claims(db_session: Session, count: int) -> None:
    for n in range(count):
        db_session.add(_agent(f"agent-bulk-{n}", f"bulk-{n}", "@test", PROJECT_B, "project"))
    db_session.commit()


def test_many_uncached_claims_are_resolved_with_one_paged_listing(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    _billing_world(monkeypatch)
    _many_claims(db_session, 25)
    listing_calls: list[str | None] = []
    single_calls: list[str] = []
    everything = [SimpleNamespace(experiment_id=str(100 + n), name=f"bulk-{n}") for n in range(25)]
    everything.append(SimpleNamespace(experiment_id=BILLING_ID, name=BILLING))

    class FakeClient:
        def search_experiments(
            self, view_type: object, max_results: int, page_token: str | None = None
        ) -> object:
            listing_calls.append(page_token)
            pages = [everything[:13], everything[13:]]
            index = 0 if page_token is None else 1
            return _Page(pages[index], "next" if index == 0 else None)

    monkeypatch.setattr(mlflow, "MlflowClient", FakeClient)

    def by_name(name: str) -> object:
        single_calls.append(name)
        return None

    monkeypatch.setattr(mlflow, "get_experiment_by_name", by_name)
    clear_resolution_cache()
    contested = experiment_claim_conflicts(db_session)
    assert contested == []
    assert listing_calls == [None, "next"], "one listing, two pages"
    assert single_calls == [], "no per-claim lookups once the listing answered"
    assert resolve_experiment("bulk-3") == "103"
    assert single_calls == [], "the answer came from the listing"


class _Page(list):
    """A paged MLflow listing: a list with a ``token`` for the next page."""

    def __init__(self, items: list[object], token: str | None) -> None:
        super().__init__(items)
        self.token = token


def test_a_failed_bulk_listing_falls_back_to_one_lookup_per_claim(
    db_session: Session,
    world: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import mlflow

    _billing_world(monkeypatch)
    _many_claims(db_session, 25)

    class BrokenClient:
        def search_experiments(self, **_: object) -> None:
            raise OSError("listing refused")

    monkeypatch.setattr(mlflow, "MlflowClient", BrokenClient)
    singles: list[str] = []
    monkeypatch.setattr(mlflow, "get_experiment_by_name", lambda name: singles.append(name) or None)
    clear_resolution_cache()
    with caplog.at_level("WARNING", logger="caliber.observability.trace_access"):
        assert experiment_claim_conflicts(db_session) == []
    assert "could not list experiments" in caplog.text
    assert len(singles) >= 25, "each claim was looked up on its own"


# ---------------------------------------------------------------- claim scopes (public, project, user)


@pytest.mark.parametrize(
    ("mine", "held", "conflicts"),
    [
        (
            ("public", None, "@x"),
            ("public", None, "@y"),
            False,
        ),  # everyone owns a public experiment
        (("public", None, "@x"), ("public", None, "@x"), False),
        (("project", PROJECT_A, "@x"), ("public", None, "@x"), True),
        (("user", None, "@x"), ("public", None, "@x"), True),
        (("public", None, "@x"), ("user", None, "@x"), True),
        (
            ("project", None, "@x"),
            ("project", PROJECT_A, "@x"),
            True,
        ),  # no project is not project A
    ],
)
def test_public_user_and_project_scopes_differ_in_who_may_share(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    mine: tuple[str, str | None, str],
    held: tuple[str, str | None, str],
    conflicts: bool,
) -> None:
    _billing_world(monkeypatch)
    db_session.add(
        CaliberAgentConfig(
            agent_id="holder",
            experiment_id=BILLING,
            name="holder",
            owner=held[2],
            project_id=held[1],
            visibility=held[0],
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db_session.commit()
    visibility, project_id, owner = mine
    call = lambda: assert_experiment_claim_allowed(
        db_session, BILLING_ID, visibility=visibility, project_id=project_id, owner=owner
    )
    if conflicts:
        with pytest.raises(ExperimentClaimConflict):
            call()
    else:
        call()


def test_metrics_do_not_reveal_an_experiment_claimed_under_another_alias(
    client: TestClient, world: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project B names its experiment by name; the trace carries the id. B's metrics count it, A's do not."""
    _billing_world(monkeypatch, honours_locations=False)
    _bind_agent(db_session, BILLING, PROJECT_B)

    def count(user: str, project: str) -> int:
        body = client.get(f"{OBS}/metrics", headers=_as(user, project)).json()
        return int(body["data"]["totals"]["count"])

    assert count(BOB, PROJECT_B) == 3, "B: exp-B, exp-public and the billing trace filed under id 7"
    assert count(ALICE, PROJECT_A) == 3, "A: exp-A, exp-wf-A, exp-public; never the billing trace"
    foreign = client.get(
        f"{OBS}/metrics", params={"experiment_id": BILLING_ID}, headers=_as(ALICE, PROJECT_A)
    )
    assert foreign.status_code == 404
