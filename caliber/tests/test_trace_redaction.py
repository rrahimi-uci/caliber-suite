"""Who may read a trace's payload (B11.4a #672, B11.4b #677): ``trace.payload.read``, ``can_read_payloads`` and the
redaction of governed fields in the trace routes.

Seeing that a trace exists is decided by the experiment's visibility (``visible_experiments``); reading what is in it
is stricter, because span inputs, outputs and tool arguments can hold personal data. The experiment's owner scope is
resolved (registry first, then the agent and workflow claims) and a project owner is checked through
``decide_project_access`` for the caller's current role.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import mlflow
import pytest
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import RESOURCE_DOES_NOT_EXIST
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberExperimentOwner,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
)
from caliber.observability.experiment_registry import tracking_backend
from caliber.observability.trace_access import (
    can_read_payloads,
    clear_resolution_cache,
    redact_trace_detail,
)
from caliber.profile import GOVERNED_TRACE_FIELDS, payload_governance_active
from caliber.resource_access import (
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    decide_project_access,
    permissions_for_role,
)
from tests.test_trace_project_scoping import ALICE, OBS, PROJECT_A, FakeMlflow, _as, _trace
from tests.test_trace_project_scoping import world as scoping_world  # noqa: F401 - a fixture

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


# ---------------------------------------------------------------- redaction in the routes (B11.4b, #677)

SECRET = "SECRET-PAYLOAD-4417"
VIEWER, REVIEWER = "@viewer-trace", "@reviewer-trace"


def _rich_span(
    span_id: str, parent: str | None, name: str, secret: str = SECRET
) -> SimpleNamespace:
    return SimpleNamespace(
        span_id=span_id,
        parent_id=parent,
        name=name,
        span_type="TOOL",
        start_time_ns=1_000_000,
        end_time_ns=3_000_000,
        status="OK",
        inputs={"question": f"{secret} question"},
        outputs={"answer": f"{secret} answer"},
        attributes={
            "caliber.run_id": "WR-1",
            "caliber.tokens": 10,
            "caliber.cost_usd": 0.5,
            "tool_arguments": json.dumps({"ssn": secret}),
            "retrieved_documents": json.dumps([f"{secret} doc"]),
            "model_output": json.dumps(f"{secret} output"),
            "caliber.model": "m",
        },
    )


@pytest.fixture
def traced(
    client: TestClient,
    scoping_world: None,  # noqa: F811 - the fixture, requested by name
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeMlflow:
    """Alice (editor of project A) plus a viewer and a reviewer of A; one rich trace in A's experiment."""
    for index, (user, role) in enumerate(((VIEWER, "viewer"), (REVIEWER, "reviewer"))):
        db_session.add(
            CaliberProjectMember(
                member_id=f"M-red-{index}",
                project_id=PROJECT_A,
                user_id=user,
                role=role,
                created_by="@t",
            )
        )
    db_session.commit()
    fake = FakeMlflow(
        monkeypatch,
        experiment_names={
            "exp-A": "exp-A"
        },  # the experiment's name resolves to its id, as in MLflow
        extra_traces={
            "tr-rich": _trace(
                "tr-rich",
                "exp-A",
                text=SECRET,
                spans=[_rich_span("s1", None, "root"), _rich_span("s2", "s1", "tool")],
            )
        },
    )
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "reference"})
    return fake


def _span_by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {span["span_id"]: span for span in body["spans"]}


def test_viewer_sees_redacted_detail(client: TestClient, traced: FakeMlflow) -> None:
    editor = client.get(f"{OBS}/traces/tr-rich", headers=_as(ALICE, PROJECT_A))
    assert editor.status_code == 200, editor.text
    assert SECRET in editor.text, "an editor reads the payload"
    for user in (VIEWER, REVIEWER):
        viewer = client.get(f"{OBS}/traces/tr-rich", headers=_as(user, PROJECT_A))
        assert viewer.status_code == 200, viewer.text
        assert SECRET not in viewer.text, "no payload anywhere in a viewer's response"
        seen, full = viewer.json()["data"], editor.json()["data"]
        for span_id, span in _span_by_id(seen).items():
            assert span["inputs"] == "[redacted:inputs]" and span["outputs"] == "[redacted:outputs]"
            for leaf in ("tool_arguments", "retrieved_documents", "model_output"):
                assert span["attributes"][leaf] == f"[redacted:attributes.{leaf}]"
            reference = _span_by_id(full)[span_id]
            for key in ("span_id", "name", "start_time_ms", "end_time_ms"):
                assert span[key] == reference[key], key
            assert (
                span["attributes"]["caliber.run_id"]
                == reference["attributes"]["caliber.run_id"]
                == "WR-1"
            )
            assert set(span) == set(reference) and set(span["attributes"]) == set(
                reference["attributes"]
            ), "no key is deleted"
        assert seen["request"] == "[redacted:request]" and seen["response"] == "[redacted:response]"
        for key in ("trace_id", "name", "status", "experiment_id", "total_tokens", "cost_usd"):
            assert seen[key] == full[key], key


def test_the_payload_is_redacted_for_an_administrator_who_is_not_a_member(
    client: TestClient, traced: FakeMlflow
) -> None:
    admin = client.get(f"{OBS}/traces/tr-rich")  # the suite's default user is an admin
    assert admin.status_code == 200 and SECRET not in admin.text


def test_summaries_and_metrics_redacted(client: TestClient, traced: FakeMlflow) -> None:
    editor = client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A))
    rich = next(t for t in editor.json()["data"]["traces"] if t["trace_id"] == "tr-rich")
    assert SECRET in rich["request_preview"] and SECRET in rich["response_preview"]
    viewer = client.get(f"{OBS}/traces", headers=_as(VIEWER, PROJECT_A))
    assert viewer.status_code == 200 and SECRET not in viewer.text
    seen = next(t for t in viewer.json()["data"]["traces"] if t["trace_id"] == "tr-rich")
    assert seen["request_preview"] == "[redacted:request_preview]"
    assert seen["response_preview"] == "[redacted:response_preview]"
    assert {k: v for k, v in seen.items() if "preview" not in k} == {
        k: v for k, v in rich.items() if "preview" not in k
    }, "everything but the previews is the editor's"
    metrics = client.get(f"{OBS}/metrics", headers=_as(VIEWER, PROJECT_A))
    assert metrics.status_code == 200 and SECRET not in metrics.text
    assert metrics.json() == client.get(f"{OBS}/metrics", headers=_as(ALICE, PROJECT_A)).json(), (
        "the numbers are the same: only payloads are governed"
    )


def test_outside_the_reference_profile_nothing_is_redacted(
    client: TestClient, traced: FakeMlflow
) -> None:
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "development"})
    viewer = client.get(f"{OBS}/traces/tr-rich", headers=_as(VIEWER, PROJECT_A))
    assert viewer.status_code == 200 and SECRET in viewer.text
    assert not payload_governance_active(client.app.state.config)


def test_a_trace_the_caller_cannot_see_is_still_a_404_not_a_redacted_one(
    client: TestClient, traced: FakeMlflow
) -> None:
    assert client.get(f"{OBS}/traces/tr-B", headers=_as(VIEWER, PROJECT_A)).status_code == 404


def test_the_experiment_is_decided_once_per_request(
    client: TestClient, traced: FakeMlflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.routes import observability as routes

    calls: list[str] = []
    real = routes.can_read_payloads

    def counting(session: Any, identity: Any, experiment_id: str) -> bool:
        calls.append(experiment_id)
        return real(session, identity, experiment_id)

    monkeypatch.setattr(routes, "can_read_payloads", counting)
    response = client.get(f"{OBS}/traces", headers=_as(VIEWER, PROJECT_A))
    assert response.status_code == 200
    assert calls.count("exp-A") == 1, "two traces in one experiment, one decision"


# ---------------------------------------------------------------- the pure function


def test_redact_trace_detail_is_pure_and_never_deletes_keys() -> None:
    detail = {
        "trace_id": "t",
        "request": "r",
        "response": "x",
        "spans": [
            {
                "span_id": "s",
                "inputs": None,
                "outputs": {"a": 1},
                "attributes": {"tool_arguments": "x", "other": 1},
            },
            {"span_id": "bare"},
            "not a span",
        ],
    }
    before = json.dumps(detail, sort_keys=True)
    out = redact_trace_detail(detail, False)
    assert json.dumps(detail, sort_keys=True) == before, "the argument is not modified"
    assert redact_trace_detail(detail, True) is detail
    first = out["spans"][0]
    assert first["inputs"] == "[redacted:inputs]", (
        "a null input is redacted too (no presence signal)"
    )
    assert first["attributes"] == {
        "tool_arguments": "[redacted:attributes.tool_arguments]",
        "other": 1,
    }
    assert out["spans"][1] == {"span_id": "bare"}, "a governed field that is absent is not invented"
    assert out["spans"][2] == "not a span"
    assert out["request"] == "[redacted:request]" and out["trace_id"] == "t"
    assert set(GOVERNED_TRACE_FIELDS) == {
        "inputs",
        "outputs",
        "attributes.tool_arguments",
        "attributes.retrieved_documents",
        "attributes.model_output",
    }
    assert redact_trace_detail({"inputs": 1, "attributes": {"model_output": "m"}}, False) == {
        "inputs": "[redacted:inputs]",
        "attributes": {"model_output": "[redacted:attributes.model_output]"},
    }
    assert redact_trace_detail({"attributes": "not a dict"}, False) == {"attributes": "not a dict"}
