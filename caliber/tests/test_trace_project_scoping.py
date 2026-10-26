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
    TraceNotReadableError,
    assert_experiment_claim_allowed,
    is_visible,
    require_readable_trace,
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


def test_a_name_claim_mlflow_cannot_resolve_grants_only_that_name(
    db_session: Session, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    def down(_name: str) -> object:
        raise RuntimeError("store unavailable")

    db_session.add(
        CaliberAgentConfig(
            agent_id="agent-named",
            experiment_id="a-name",
            name="named",
            owner="@test",
            project_id=PROJECT_A,
            visibility="project",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db_session.commit()
    monkeypatch.setattr(mlflow, "get_experiment_by_name", down)
    found = visible_experiments(db_session, _identity(ALICE, PROJECT_A))
    assert found == ALICE_VISIBLE | {"a-name"}


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
