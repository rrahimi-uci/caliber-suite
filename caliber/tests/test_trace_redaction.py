"""Who may read a trace's payload (B11.4a, #672): ``trace.payload.read`` and ``can_read_payloads``.

Seeing that a trace exists is decided by the experiment's visibility (``visible_experiments``); reading what is in it
is stricter, because span inputs, outputs and tool arguments can hold personal data. The experiment's owner scope is
resolved (registry first, then the agent and workflow claims) and a project owner is checked through
``decide_project_access`` for the caller's current role.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import mlflow
import pytest
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import RESOURCE_DOES_NOT_EXIST
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberExperimentOwner,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
)
from caliber.observability.experiment_registry import tracking_backend
from caliber.observability.trace_access import can_read_payloads, clear_resolution_cache
from caliber.resource_access import (
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    decide_project_access,
    permissions_for_role,
)

PROJECT = "P-payload"
ROLES = {
    "@owner": ROLE_OWNER,
    "@editor": ROLE_EDITOR,
    "@reviewer": ROLE_REVIEWER,
    "@viewer": ROLE_VIEWER,
}


def _identity(user: str, *scopes: str) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user,
        scopes=frozenset(scopes or {SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_APPROVER}),
        active_project_id=PROJECT,
    )


@pytest.fixture
def world(db_session: Session) -> Session:
    db_session.add(CaliberProject(project_id=PROJECT, name="payload", owner="@owner"))
    db_session.flush()
    for index, (user, role) in enumerate(ROLES.items()):
        db_session.add(
            CaliberProjectMember(
                member_id=f"M-pay-{index}",
                project_id=PROJECT,
                user_id=user,
                role=role,
                created_by="@test",
            )
        )
    db_session.add(
        CaliberAgentConfig(
            agent_id="ag-pay",
            experiment_id="payload-exp",
            name="ag-pay",
            owner="@owner",
            project_id=PROJECT,
            visibility="project",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db_session.commit()
    return db_session


@pytest.fixture
def tracking(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A tracking backend that knows ``payload-exp`` as id 11 and ``other-exp`` as id 12."""
    names = {"payload-exp": "11", "other-exp": "12"}
    state: dict[str, Any] = {"down": False}

    def by_name(name: str) -> Any:
        if state["down"]:
            raise MlflowException("down", error_code=1)
        return SimpleNamespace(experiment_id=names[name], name=name) if name in names else None

    def by_id(experiment_id: str) -> Any:
        if state["down"]:
            raise MlflowException("down", error_code=1)
        for name, found in names.items():
            if found == experiment_id:
                return SimpleNamespace(experiment_id=found, name=name, tags={})
        raise MlflowException("none", error_code=RESOURCE_DOES_NOT_EXIST)

    monkeypatch.setattr(mlflow, "get_experiment_by_name", by_name)
    monkeypatch.setattr(mlflow, "get_experiment", by_id)
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: "http://mlflow.example.invalid:5000")
    clear_resolution_cache()
    names["state"] = state  # type: ignore[assignment]
    return names


# ---------------------------------------------------------------- the permission


def test_the_permission_is_owner_and_editor_only() -> None:
    assert PROJECT_ACTIONS["trace.payload.read"] == {ROLE_OWNER, ROLE_EDITOR}
    for role, allowed in (
        (ROLE_OWNER, True),
        (ROLE_EDITOR, True),
        (ROLE_REVIEWER, False),
        (ROLE_VIEWER, False),
    ):
        assert ("trace.payload.read" in permissions_for_role(role)) is allowed


def test_payload_permission_by_role(world: Session, tracking: dict[str, str]) -> None:
    for user, expected in (
        ("@owner", True),
        ("@editor", True),
        ("@reviewer", False),
        ("@viewer", False),
    ):
        assert can_read_payloads(world, _identity(user), "payload-exp") is expected, user
        assert can_read_payloads(world, _identity(user), "11") is expected, (
            f"{user} by canonical id"
        )
    assert can_read_payloads(world, _identity("@stranger"), "payload-exp") is False, "not a member"


def test_the_decision_matches_decide_project_access(
    world: Session, tracking: dict[str, str]
) -> None:
    project = world.get(CaliberProject, PROJECT)
    for user in ROLES:
        identity = _identity(user)
        decided = decide_project_access(world, identity, project, "trace.payload.read")
        assert can_read_payloads(world, identity, "payload-exp") is decided.allowed


def test_an_owner_who_lost_the_platform_scopes_is_narrowed_to_editor_and_still_reads(
    world: Session, tracking: dict[str, str]
) -> None:
    assert can_read_payloads(world, _identity("@owner", SCOPE_VIEWER), "payload-exp") is True


def test_an_administrator_is_not_an_implicit_project_role(
    world: Session, tracking: dict[str, str]
) -> None:
    admin = _identity("@root", SCOPE_ADMIN, SCOPE_OPERATOR, SCOPE_APPROVER, SCOPE_VIEWER)
    assert can_read_payloads(world, admin, "payload-exp") is False


# ---------------------------------------------------------------- who owns the experiment


def test_the_registry_decides_over_the_text_claims(
    world: Session, tracking: dict[str, str]
) -> None:
    world.add(
        CaliberExperimentOwner(
            tracking_backend=tracking_backend(),
            experiment_id="11",
            scope_kind="user",
            scope_id="@viewer",
            status="active",
            provenance="resolved",
            claimed_by="@root",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@viewer"), "payload-exp") is True, (
        "the registered user owns it"
    )
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is False, (
        "the project's text claim lost"
    )


def test_a_quarantined_experiment_has_no_owner(world: Session, tracking: dict[str, str]) -> None:
    world.add(
        CaliberExperimentOwner(
            tracking_backend=tracking_backend(),
            experiment_id="11",
            scope_kind="",
            scope_id="",
            status="quarantined",
            provenance="legacy",
            claimed_by="@root",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is False


def test_a_user_owned_experiment_belongs_to_that_user_only(
    world: Session, tracking: dict[str, str]
) -> None:
    world.add(
        CaliberAgentConfig(
            agent_id="ag-mine",
            experiment_id="other-exp",
            name="mine",
            owner="@editor",
            project_id=None,
            visibility="user",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@editor"), "other-exp") is True
    assert can_read_payloads(world, _identity("@owner"), "other-exp") is False
    assert can_read_payloads(world, _identity("@root", SCOPE_ADMIN), "other-exp") is False


def test_a_public_experiment_has_no_payload_readers(
    world: Session, tracking: dict[str, str]
) -> None:
    world.add(
        CaliberWorkflow(
            workflow_id="wf-pub",
            name="pub",
            owner="@owner",
            visibility="public",
            default_experiment_id="other-exp",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@owner"), "other-exp") is False


def test_an_experiment_two_scopes_claim_has_no_owner_before_the_migration(
    world: Session, tracking: dict[str, str]
) -> None:
    world.add(
        CaliberWorkflow(
            workflow_id="wf-rival",
            name="rival",
            owner="@editor",
            visibility="user",
            default_experiment_id="11",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is False
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is False


def test_an_experiment_nobody_claims_has_no_payload_readers(
    world: Session, tracking: dict[str, str]
) -> None:
    assert can_read_payloads(world, _identity("@owner"), "other-exp") is False
    assert can_read_payloads(world, _identity("@owner"), "no-such-experiment") is False


def test_an_empty_reference_is_refused(world: Session, tracking: dict[str, str]) -> None:
    assert can_read_payloads(world, _identity("@owner"), "") is False


def test_a_project_that_no_longer_exists_is_refused(
    world: Session, tracking: dict[str, str]
) -> None:
    world.add(
        CaliberExperimentOwner(
            tracking_backend=tracking_backend(),
            experiment_id="12",
            scope_kind="project",
            scope_id="P-gone",
            status="active",
            provenance="legacy",
            claimed_by="@root",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@owner"), "other-exp") is False


def test_an_unreachable_tracking_backend_fails_closed(
    world: Session, tracking: dict[str, str]
) -> None:
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is True
    tracking["state"]["down"] = True  # type: ignore[index]
    clear_resolution_cache()
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is False, (
        "no guess, no exception"
    )


def test_the_decision_follows_the_membership_as_it_is_now(
    world: Session, tracking: dict[str, str]
) -> None:
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is True
    member = world.query(CaliberProjectMember).filter_by(user_id="@editor").one()
    member.role = ROLE_VIEWER
    world.commit()
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is False, (
        "demoted: refused at once"
    )
    world.delete(member)
    world.commit()
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is False, (
        "removed: refused"
    )


# ---------------------------------------------------------------- findings of the independent review (#956)


def test_a_caller_acting_in_another_project_or_on_a_credential_bound_to_one_is_refused(
    world: Session, tracking: dict[str, str]
) -> None:
    editor = _identity("@editor")
    assert can_read_payloads(world, editor, "payload-exp") is True
    for other in (
        replace(editor, active_project_id="P-other"),
        replace(editor, active_project_id=None),
        replace(editor, credential_project_id="P-other"),
        replace(editor, active_project_id="P-other", credential_project_id="P-other"),
    ):
        assert can_read_payloads(world, other, "payload-exp") is False, other
    assert (
        can_read_payloads(world, replace(editor, credential_project_id=PROJECT), "payload-exp")
        is True
    )


def _claim_agent(world: Session, agent_id: str, experiment: str) -> None:
    world.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=experiment,
            name=agent_id,
            owner="@owner",
            project_id=PROJECT,
            visibility="project",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    world.commit()


def test_a_reference_that_names_no_existing_experiment_gives_no_access_even_to_its_claimant(
    world: Session, tracking: dict[str, str]
) -> None:
    _claim_agent(world, "ag-future", "99")
    assert can_read_payloads(world, _identity("@owner"), "99") is False


def test_a_stale_cached_miss_cannot_hand_the_old_claimant_access(
    world: Session, tracking: dict[str, str]
) -> None:
    from caliber.observability.trace_access import resolve_experiment

    _claim_agent(world, "ag-foo", "foo")
    assert resolve_experiment("foo") is None  # cached as "no such experiment"
    tracking["foo"] = "13"
    world.add(
        CaliberExperimentOwner(
            tracking_backend=tracking_backend(),
            experiment_id="13",
            scope_kind="user",
            scope_id="@viewer",
            status="active",
            provenance="resolved",
            claimed_by="@root",
        )
    )
    world.commit()
    assert can_read_payloads(world, _identity("@owner"), "foo") is False, (
        "fails closed on the stale miss"
    )
    clear_resolution_cache()
    assert can_read_payloads(world, _identity("@viewer"), "foo") is True
    assert can_read_payloads(world, _identity("@owner"), "foo") is False


def test_garbage_and_database_errors_are_refusals_not_exceptions(
    world: Session, tracking: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in (["payload-exp"], {"a": 1}, 11, None, b"11", "\x00"):
        assert can_read_payloads(world, _identity("@owner"), bad) is False  # type: ignore[arg-type]

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database down")

    from caliber.observability import experiment_registry

    monkeypatch.setattr(experiment_registry, "registry_row", broken)
    assert can_read_payloads(world, _identity("@owner"), "payload-exp") is False


def test_a_deactivated_member_and_an_archived_project(
    world: Session, tracking: dict[str, str]
) -> None:
    member = world.query(CaliberProjectMember).filter_by(user_id="@editor").one()
    member.status = "inactive"
    world.commit()
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is False
    member.status = "active"
    project = world.get(CaliberProject, PROJECT)
    assert project is not None
    project.status = "archived"
    world.commit()
    assert can_read_payloads(world, _identity("@editor"), "payload-exp") is True, (
        "as every read path"
    )
