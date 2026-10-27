"""B11 public path: one project cannot take another's MLflow experiment, by any name or id (B11.registry, #665).

A real (SQLite-backed) MLflow tracking store sits behind the reference-profile app. Editors of two projects register
agents through the public API: the first project's experiment is created for it by CALIBER, the second project is
refused whether it names the experiment or uses its numeric id, an experiment that already existed in MLflow is
refused to an ordinary editor and adopted only by an administrator. On PostgreSQL (the profile) the registry rows
and the experiment's owner tags are asserted too; SQLite (development) keeps the first-come behaviour for names that
resolve to nothing, and the cross-project refusals hold in both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mlflow
import pytest
from mlflow.tracking import fluent
from sqlalchemy import select

from caliber.db.models import CaliberExperimentOwner
from caliber.observability.trace_access import clear_resolution_cache
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    ReferenceApp,
    Workspaces,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
ADMIN = "@egi-admin"


@pytest.fixture
def tracking_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real MLflow tracking store, with the suite's hermetic stand-ins for MLflow lookups put back."""
    previous = mlflow.get_tracking_uri()
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    mlflow.set_tracking_uri(uri)
    monkeypatch.setattr(mlflow, "get_experiment_by_name", fluent.get_experiment_by_name)
    monkeypatch.setattr(mlflow, "get_experiment", fluent.get_experiment)
    clear_resolution_cache()
    try:
        yield uri
    finally:
        mlflow.set_tracking_uri(previous)
        clear_resolution_cache()


def _post(app: ReferenceApp, user: str, project: str, path: str, body: dict[str, Any]) -> Any:
    headers = app.headers(user, project)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return app.client.post(
        f"{PREFIX}{path}", json=body, headers={**headers, "X-CALIBER-CSRF": token}
    )


def _register(app: ReferenceApp, user: str, project: str, agent_id: str, experiment: str) -> Any:
    body = {"agent_id": agent_id, "experiment_id": experiment, "name": agent_id, "owner": user}
    return _post(app, user, project, "/agents", body)


def _workflow(app: ReferenceApp, user: str, project: str, name: str, experiment: str) -> Any:
    return _post(
        app, user, project, "/workflows", {"name": name, "default_experiment_id": experiment}
    )


def test_a_second_project_cannot_take_a_first_projects_experiment_under_any_alias(
    reference_app: ReferenceApp, two_workspaces: Workspaces, tracking_store: str
) -> None:
    reference = reference_app.profile == "reference"
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"admin_users": ADMIN}
    )

    first = _register(reference_app, EDITOR_A, PROJECT_A, "agent-a", "support-traces")
    assert first.status_code == 201, first.text
    experiment = mlflow.get_experiment_by_name("support-traces")
    if reference:
        assert experiment is not None, (
            "the reference profile creates the experiment for the claimant"
        )
        assert experiment.tags["caliber.owner_id"] == PROJECT_A
        with reference_app.session_factory() as session:
            row = session.execute(select(CaliberExperimentOwner)).scalars().one()
        assert (row.experiment_id, row.scope_kind, row.scope_id, row.provenance) == (
            experiment.experiment_id,
            "project",
            PROJECT_A,
            "created",
        )
        by_id = _register(
            reference_app, EDITOR_B, PROJECT_B, "agent-b-id", experiment.experiment_id
        )
        assert by_id.status_code == 409, by_id.text

    by_name = _register(reference_app, EDITOR_B, PROJECT_B, "agent-b-name", "support-traces")
    assert by_name.status_code == 409, by_name.text

    # Agents and workflows share one registry: the owner's workflow may use it, the other project's may not.
    mine = _workflow(reference_app, EDITOR_A, PROJECT_A, "wf-a", "support-traces")
    assert mine.status_code == 201, mine.text
    theirs = _workflow(reference_app, EDITOR_B, PROJECT_B, "wf-b", "support-traces")
    assert theirs.status_code == 409, theirs.text


def test_an_experiment_that_already_exists_is_not_the_first_editors_to_take(
    reference_app: ReferenceApp, two_workspaces: Workspaces, tracking_store: str
) -> None:
    if reference_app.profile != "reference":
        pytest.skip("trusted first-claim provenance is the reference profile's rule")
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"admin_users": ADMIN}
    )
    existing = mlflow.create_experiment("pre-existing-team-traces")

    refused = _register(reference_app, EDITOR_B, PROJECT_B, "agent-b", "pre-existing-team-traces")
    assert refused.status_code == 409, refused.text
    refused_by_id = _register(reference_app, EDITOR_B, PROJECT_B, "agent-b2", existing)
    assert refused_by_id.status_code == 409, refused_by_id.text
    with reference_app.session_factory() as session:
        assert session.execute(select(CaliberExperimentOwner)).first() is None, (
            "nothing was appropriated"
        )

    adopted = _register(reference_app, ADMIN, "", "agent-admin", "pre-existing-team-traces")
    assert adopted.status_code == 201, adopted.text
    with reference_app.session_factory() as session:
        row = session.execute(select(CaliberExperimentOwner)).scalars().one()
    assert (row.experiment_id, row.scope_kind, row.scope_id, row.provenance) == (
        existing,
        "user",
        ADMIN,
        "adopted",
    )
    assert _register(reference_app, EDITOR_A, PROJECT_A, "agent-a", existing).status_code == 409
