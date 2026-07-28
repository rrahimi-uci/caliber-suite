"""Regression tests for the three agent-configuration defects the review named.

From the report's Agent-configuration row: "Experiment existence/connectivity is
not verified, skill resolution is globally unscoped, explicit ``null`` PATCH values
can reach non-null columns and return 500". Its decision ledger separately
rejected calling the non-empty-string check "experiment-binding preflight" as an
overclaim.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.agents as agents_route
from caliber.db.models import CaliberAgentConfig, CaliberProject, CaliberSkill

BASE = "/ajax-api/2.0/mlflow/caliber/agents"

# The default test identity holds ``caliber.admin``, which sees every project by
# design. Scoping assertions must therefore run as a non-admin, or they pass
# vacuously — the exact fixture blind spot the report flagged.
NON_ADMIN = "@viewer"


def _agent(session: Session, *, agent_id: str = "AG-1", project_id: str | None = None) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name="Support agent",
            owner="@test",
            project_id=project_id,
            visibility="project" if project_id else "user",
            artifact_types=["prompt", "skill"],
            optimizer_config={"skills": ["tone"]},
            enabled=True,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Explicit-null PATCH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "owner",
        "artifact_types",
        "eval_thresholds",
        "optimizer_config",
        "approval_policy",
        "optimize_for",
        "enabled",
        "required_approvals",
    ],
)
def test_an_explicit_null_on_a_non_null_column_is_a_400_not_a_500(
    client: TestClient, db_session: Session, field: str
) -> None:
    """A client input error must not surface as a server fault. Writing the null
    reached the flush, so SQLAlchemy raised and the caller got a 500."""
    _agent(db_session)
    response = client.patch(f"{BASE}/AG-1", json={field: None})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert field in detail
    assert "cannot be set to null" in detail
    # Nothing was mutated.
    db_session.expire_all()
    agent = db_session.get(CaliberAgentConfig, "AG-1")
    assert agent is not None
    assert agent.name == "Support agent"


def test_clearing_a_nullable_column_still_works(client: TestClient, db_session: Session) -> None:
    """The guard must reject only nulls the schema cannot hold.
    ``collaboration_mode`` is nullable, so clearing it is a legitimate edit."""
    _agent(db_session)
    assert client.patch(f"{BASE}/AG-1", json={"collaboration_mode": "pair"}).status_code == 200
    response = client.patch(f"{BASE}/AG-1", json={"collaboration_mode": None})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["collaboration_mode"] is None


def test_an_omitted_field_is_still_left_unchanged(client: TestClient, db_session: Session) -> None:
    _agent(db_session)
    response = client.patch(f"{BASE}/AG-1", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Renamed"
    assert data["owner"] == "@test"  # untouched


# ---------------------------------------------------------------------------
# Skill resolution scoping
# ---------------------------------------------------------------------------


def test_skill_resolution_does_not_reach_another_projects_skill(
    client: TestClient, db_session: Session
) -> None:
    """Regression: names were matched globally, so an agent citing a common name
    (``tone``, ``formatting``) returned another project's skill body."""
    db_session.add_all(
        [
            CaliberProject(project_id="PRJ-a", tenant_id="t", name="A", owner="@test"),
            CaliberProject(project_id="PRJ-b", tenant_id="t", name="B", owner="@other"),
            CaliberSkill(
                skill_id="SK-other",
                name="tone",
                content="OTHER PROJECT SECRET INSTRUCTIONS",
                owner="@other",
                project_id="PRJ-b",
                visibility="project",
                status="active",
            ),
        ]
    )
    _agent(db_session, project_id="PRJ-a")
    db_session.execute(
        CaliberAgentConfig.__table__.update()
        .where(CaliberAgentConfig.agent_id == "AG-1")
        .values(owner=NON_ADMIN)
    )
    db_session.commit()

    response = client.get(
        f"{BASE}/AG-1/skills",
        headers={"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PRJ-a"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # Reported as an unresolved reference for *this* caller, and the foreign body
    # is not disclosed.
    assert data["skills"] == []
    assert data["missing"] == ["tone"]
    assert "OTHER PROJECT SECRET" not in response.text


def test_skill_resolution_still_resolves_a_visible_skill(
    client: TestClient, db_session: Session
) -> None:
    db_session.add_all(
        [
            CaliberProject(project_id="PRJ-a", tenant_id="t", name="A", owner="@test"),
            CaliberSkill(
                skill_id="SK-mine",
                name="tone",
                content="Be concise.",
                owner="@test",
                project_id="PRJ-a",
                visibility="project",
                status="active",
            ),
        ]
    )
    _agent(db_session, project_id="PRJ-a")

    response = client.get(f"{BASE}/AG-1/skills", headers={"X-CALIBER-Project": "PRJ-a"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [skill["skill_id"] for skill in data["skills"]] == ["SK-mine"]
    assert data["missing"] == []


# ---------------------------------------------------------------------------
# Experiment reachability
# ---------------------------------------------------------------------------


class _FakeExperiment:
    def __init__(self, experiment_id: str, name: str, stage: str = "active") -> None:
        self.experiment_id = experiment_id
        self.name = name
        self.lifecycle_stage = stage


def _patch_mlflow(monkeypatch: pytest.MonkeyPatch, client_impl: Any) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "MlflowClient", lambda *a, **k: client_impl, raising=False)


def test_a_resolvable_experiment_reports_reachable(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _agent(db_session)

    class _Client:
        def get_experiment(self, value: str) -> Any:  # pragma: no cover - name path used
            raise AssertionError("non-numeric id must resolve by name")

        def get_experiment_by_name(self, value: str) -> Any:
            assert value == "exp-AG-1"
            return _FakeExperiment("7", "exp-AG-1")

    _patch_mlflow(monkeypatch, _Client())
    data = client.get(f"{BASE}/AG-1/experiment").json()["data"]
    assert data["status"] == "reachable"
    assert data["experiment_id"] == "7"
    assert data["configured_experiment_id"] == "exp-AG-1"


def test_a_numeric_id_resolves_by_id(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        CaliberAgentConfig(
            agent_id="AG-num",
            experiment_id="42",
            name="Numeric",
            owner="@test",
            artifact_types=[],
            optimizer_config={},
        )
    )
    db_session.commit()

    class _Client:
        def get_experiment(self, value: str) -> Any:
            assert value == "42"
            return _FakeExperiment("42", "numbered")

        def get_experiment_by_name(self, value: str) -> Any:  # pragma: no cover
            raise AssertionError("a numeric id must not resolve by name")

    _patch_mlflow(monkeypatch, _Client())
    data = client.get(f"{BASE}/AG-num/experiment").json()["data"]
    assert data["status"] == "reachable"


def test_an_absent_experiment_reports_missing(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete overclaim: a typo'd id passed a check that only measured
    string length."""
    _agent(db_session)

    class _Client:
        def get_experiment_by_name(self, _value: str) -> Any:
            return None

    _patch_mlflow(monkeypatch, _Client())
    data = client.get(f"{BASE}/AG-1/experiment").json()["data"]
    assert data["status"] == "missing"
    assert "no experiment" in data["detail"]


def test_a_deleted_experiment_reports_missing_not_reachable(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trashed experiment resolves but cannot receive runs, so treating it as a
    working binding would be the same overclaim in a new place."""
    _agent(db_session)

    class _Client:
        def get_experiment_by_name(self, _value: str) -> Any:
            return _FakeExperiment("7", "exp-AG-1", stage="deleted")

    _patch_mlflow(monkeypatch, _Client())
    data = client.get(f"{BASE}/AG-1/experiment").json()["data"]
    assert data["status"] == "missing"
    assert "deleted" in data["detail"]


def test_a_registry_outage_reports_unverified_rather_than_missing(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "We could not check" is different information from "it is not there", and
    conflating them would make an outage look like a misconfiguration."""
    _agent(db_session)

    class _Client:
        def get_experiment_by_name(self, _value: str) -> Any:
            raise ConnectionError("connection refused")

    _patch_mlflow(monkeypatch, _Client())
    data = client.get(f"{BASE}/AG-1/experiment").json()["data"]
    assert data["status"] == "unverified"
    assert "could not reach MLflow" in data["detail"]


def test_an_empty_configured_id_is_missing_without_touching_the_registry() -> None:
    assert agents_route.resolve_experiment("")["status"] == "missing"
    assert agents_route.resolve_experiment("   ")["status"] == "missing"


def test_the_experiment_endpoint_is_visibility_scoped(
    client: TestClient, db_session: Session
) -> None:
    db_session.add_all(
        [
            CaliberProject(project_id="PRJ-b", tenant_id="t", name="B", owner="@other"),
            CaliberAgentConfig(
                agent_id="AG-foreign",
                experiment_id="exp-foreign",
                name="Foreign",
                owner="@other",
                project_id="PRJ-b",
                visibility="project",
                artifact_types=[],
                optimizer_config={},
            ),
        ]
    )
    db_session.commit()
    assert (
        client.get(
            f"{BASE}/AG-foreign/experiment", headers={"X-CALIBER-User": NON_ADMIN}
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Cross-project evaluation target (architecture: scoping is a cross-cutting
# concern, not per-handler discipline)
# ---------------------------------------------------------------------------


def test_an_evaluation_cannot_bind_another_projects_workflow_version(
    client: TestClient, db_session: Session
) -> None:
    """Regression: the version lookup was a bare primary-key fetch, and the
    managed-file binding then trusted *that* workflow's ``project_id`` — so an
    unscoped version id let an evaluation read another project's file content."""
    from caliber.db.models import (
        CaliberEvalDataset,
        CaliberEvalDatasetExample,
        CaliberWorkflow,
        CaliberWorkflowVersion,
    )
    from tests.workflow_helpers import make_manifest

    db_session.add_all(
        [
            CaliberProject(project_id="PRJ-mine", tenant_id="t", name="Mine", owner=NON_ADMIN),
            CaliberProject(project_id="PRJ-theirs", tenant_id="t", name="Theirs", owner="@other"),
            CaliberWorkflow(
                workflow_id="WF-theirs",
                name="Theirs",
                owner="@other",
                project_id="PRJ-theirs",
                visibility="project",
            ),
            CaliberWorkflowVersion(
                version_id="WFV-theirs",
                workflow_id="WF-theirs",
                version_number=1,
                status="published",
                manifest=make_manifest("WF-theirs"),
                manifest_hash="hash-theirs",
            ),
            CaliberEvalDataset(
                dataset_id="ED-x",
                name="x",
                owner=NON_ADMIN,
                status="active",
                version=1,
                project_id="PRJ-mine",
                visibility="project",
            ),
            CaliberEvalDatasetExample(
                example_id="EX-x",
                dataset_id="ED-x",
                dataset_version=1,
                input={"input": "hello"},
                expected={"expected": "hello"},
            ),
        ]
    )
    db_session.commit()
    # A non-admin *operator*: creating an evaluation needs the operator scope, and
    # an admin would legitimately see every project, making the assertion vacuous.
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": NON_ADMIN}
    )

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/evaluations",
        json={
            "dataset_id": "ED-x",
            "scorers": ["exact_match"],
            "predict_target": "workflow",
            "subject_ref": "WFV-theirs",
        },
        headers={"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PRJ-mine"},
    )
    # Same 404 as an unknown version: "exists but forbidden" is a disclosure.
    assert response.status_code == 404, response.text
    assert "not found" in response.json()["detail"]
