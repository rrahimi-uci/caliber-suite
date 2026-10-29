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
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberExperimentOwner,
    CaliberProject,
    CaliberProjectMember,
    CaliberReviewItem,
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
from caliber.trace_client import TraceDetail
from tests.test_trace_project_scoping import (
    ALICE,
    OBS,
    PROJECT_A,
    FakeMlflow,
    _as,
    _grant_operator,
    _trace,
)
from tests.test_trace_project_scoping import (
    world as scoping_world,  # noqa: F401 - a fixture
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
            # What real tracers write: the same payload under other attribute names.
            "mlflow.spanInputs": json.dumps({"question": f"{secret} question"}),
            "mlflow.spanOutputs": json.dumps({"answer": f"{secret} answer"}),
            "mlflow.chat.messages": json.dumps([{"role": "user", "content": secret}]),
            "caliber.tool.input": json.dumps({"q": secret}),
            "caliber.tool.output": json.dumps({"r": secret}),
            "mlflow.spanType": "TOOL",
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


def _seed_completed_review_item(db_session: Session, item_id: str = "RVI-redaction") -> None:
    db_session.add(
        CaliberReviewItem(
            item_id=item_id,
            queue_id="rq-a",
            trace_id="tr-rich",
            experiment_id="exp-A",
            status="completed",
            answers={"ok": True, "rationale": "keep this review answer"},
            assessment_ids=["assessment-1"],
            completed_by="@reviewer-trace",
        )
    )
    db_session.commit()


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


def test_review_queue_alignment_redacts_payloads_but_preserves_review_metadata(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible queue is not a payload grant: the reviewer keeps answers and lineage only."""
    from caliber.routes import review_queues as routes

    _seed_completed_review_item(db_session)
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        if kwargs.get("include_payloads") is False:
            return TraceDetail(trace_id=trace_id, experiment_id="exp-A")
        return TraceDetail(
            trace_id=trace_id,
            experiment_id="exp-A",
            request=SECRET,
            response=SECRET,
        )

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    response = client.get(
        "/ajax-api/2.0/mlflow/caliber/review-queues/rq-a/alignment-examples",
        params={"question_key": "ok"},
        headers=_as(VIEWER, PROJECT_A),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert calls == [{"include_payloads": False}], "a denied reviewer never maps full payloads"
    assert data["examples"][0]["inputs"] == {
        "trace_id": "tr-rich",
        "request": "[redacted:request]",
        "review_item_id": "RVI-redaction",
    }
    assert data["examples"][0]["outputs"] == "[redacted:response]"
    assert data["examples"][0]["expectations"] == {"rationale": "keep this review answer"}
    assert data["examples"][0]["label"] is True
    assert data["examples"][0]["provenance"]["assessment_ids"] == ["assessment-1"]
    assert SECRET not in response.text


def test_review_queue_alignment_keeps_existing_payload_shape_for_editor(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import review_queues as routes

    _seed_completed_review_item(db_session, item_id="RVI-editor")
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        return TraceDetail(
            trace_id=trace_id,
            experiment_id="exp-A",
            request=SECRET,
            response=SECRET,
        )

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    response = client.get(
        "/ajax-api/2.0/mlflow/caliber/review-queues/rq-a/alignment-examples",
        params={"question_key": "ok"},
        headers=_as(ALICE, PROJECT_A),
    )

    assert response.status_code == 200, response.text
    assert calls == [{"include_payloads": False}, {}]
    example = response.json()["data"]["examples"][0]
    assert example["inputs"]["request"] == SECRET
    assert example["outputs"] == SECRET


def test_review_queue_alignment_skips_a_foreign_trace_without_full_fetch(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import review_queues as routes

    _seed_completed_review_item(db_session, item_id="RVI-foreign")
    item = db_session.get(CaliberReviewItem, "RVI-foreign")
    assert item is not None
    item.experiment_id = "exp-B"
    db_session.commit()
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        return TraceDetail(trace_id=trace_id, experiment_id="exp-B", response=SECRET)

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    response = client.get(
        "/ajax-api/2.0/mlflow/caliber/review-queues/rq-a/alignment-examples",
        params={"question_key": "ok"},
        headers=_as(VIEWER, PROJECT_A),
    )

    assert response.status_code == 200, response.text
    assert calls == [{"include_payloads": False}]
    assert response.json()["data"]["examples"] == []
    assert response.json()["data"]["skipped"] == [
        {"item_id": "RVI-foreign", "reason": "trace response unavailable"}
    ]


def test_from_trace_refuses_viewer_before_copying_or_mutating_dataset(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes

    _grant_operator(client, VIEWER)
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        if kwargs.get("include_payloads") is False:
            return TraceDetail(trace_id=trace_id, experiment_id="exp-A")
        return TraceDetail(
            trace_id=trace_id,
            experiment_id="exp-A",
            request=SECRET,
            response=SECRET,
        )

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    before = db_session.get(CaliberEvalDataset, "ds-a")
    assert before is not None
    before_version = before.version
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={
            "trace_id": "tr-rich",
            "input": {"manual": "input"},
            "expected": {"manual": "expected"},
        },
        headers=_as(VIEWER, PROJECT_A),
    )

    assert response.status_code == 403, response.text
    assert calls == [{"strict": True, "include_payloads": False}]
    db_session.expire_all()
    assert db_session.get(CaliberEvalDataset, "ds-a").version == before_version  # type: ignore[union-attr]
    assert db_session.execute(select(CaliberEvalDatasetExample)).scalars().all() == []
    assert SECRET not in response.text


def test_from_trace_returns_503_when_metadata_store_is_unavailable(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes
    from caliber.trace_client import TraceStoreUnavailableError

    _grant_operator(client, ALICE)

    def unavailable(*_args: Any, **_kwargs: Any) -> TraceDetail:
        raise TraceStoreUnavailableError("trace store unavailable")

    monkeypatch.setattr(routes, "fetch_trace_detail", unavailable)
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-down", "input": {"manual": "input"}},
        headers=_as(ALICE, PROJECT_A),
    )

    assert response.status_code == 503
    assert "trace store unavailable" in response.text


def test_from_trace_returns_503_if_store_fails_after_payload_authorization(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes
    from caliber.trace_client import TraceStoreUnavailableError

    _grant_operator(client, ALICE)
    calls: list[dict[str, Any]] = []

    def fails_after_metadata(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        if kwargs.get("include_payloads") is False:
            return TraceDetail(trace_id=trace_id, experiment_id="exp-A")
        raise TraceStoreUnavailableError("trace store lost during payload fetch")

    monkeypatch.setattr(routes, "fetch_trace_detail", fails_after_metadata)
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-race", "input": {"manual": "input"}},
        headers=_as(ALICE, PROJECT_A),
    )

    assert response.status_code == 503
    assert calls == [{"strict": True, "include_payloads": False}, {"strict": True}]


def test_from_trace_returns_404_for_missing_metadata_or_payload_trace(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes
    from caliber.trace_client import TraceNotFoundError

    _grant_operator(client, ALICE)

    def missing_metadata(*_args: Any, **_kwargs: Any) -> TraceDetail:
        raise TraceNotFoundError("not found")

    monkeypatch.setattr(routes, "fetch_trace_detail", missing_metadata)
    metadata_response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-missing-metadata", "input": {"manual": "input"}},
        headers=_as(ALICE, PROJECT_A),
    )
    assert metadata_response.status_code == 404

    calls: list[dict[str, Any]] = []

    def missing_payload(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        if kwargs.get("include_payloads") is False:
            return TraceDetail(trace_id=trace_id, experiment_id="exp-A")
        raise TraceNotFoundError("not found after authorization")

    monkeypatch.setattr(routes, "fetch_trace_detail", missing_payload)
    payload_response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-missing-payload", "input": {"manual": "input"}},
        headers=_as(ALICE, PROJECT_A),
    )
    assert payload_response.status_code == 404
    assert calls == [{"strict": True, "include_payloads": False}, {"strict": True}]


def test_from_trace_allows_editor_after_metadata_authorization(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes

    _grant_operator(client, ALICE)
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        return TraceDetail(
            trace_id=trace_id,
            experiment_id="exp-A",
            request=SECRET,
            response=SECRET,
        )

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-rich"},
        headers=_as(ALICE, PROJECT_A),
    )

    assert response.status_code == 201, response.text
    assert calls == [{"strict": True, "include_payloads": False}, {"strict": True}]
    data = response.json()["data"]
    assert data["input"]["input"] == SECRET
    assert data["expected"]["expected"] == SECRET
    assert data["tags"][:2] == ["from-trace", "trace:tr-rich"]


def test_from_trace_refuses_a_foreign_experiment_before_full_fetch(
    client: TestClient,
    traced: FakeMlflow,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import eval_datasets as routes

    _grant_operator(client, ALICE)
    calls: list[dict[str, Any]] = []

    def fetch(trace_id: str, **kwargs: Any) -> TraceDetail:
        calls.append(kwargs)
        return TraceDetail(trace_id=trace_id, experiment_id="exp-B")

    monkeypatch.setattr(routes, "fetch_trace_detail", fetch)
    before = db_session.get(CaliberEvalDataset, "ds-a")
    assert before is not None
    before_version = before.version
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ds-a/examples/from-trace",
        json={"trace_id": "tr-foreign", "input": {"manual": "input"}},
        headers=_as(ALICE, PROJECT_A),
    )

    assert response.status_code == 404, response.text
    assert calls == [{"strict": True, "include_payloads": False}]
    db_session.expire_all()
    assert db_session.get(CaliberEvalDataset, "ds-a").version == before_version  # type: ignore[union-attr]


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
        "other": "[redacted:attributes.other]",
    }, "an attribute that is not known to be safe is redacted too (fail closed)"
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


def test_every_attribute_outside_the_safe_list_is_redacted_for_real_shaped_spans(
    client: TestClient, traced: FakeMlflow
) -> None:
    """MLflow and the MCP gateway put the payload in attributes the governed list does not name."""
    editor = client.get(f"{OBS}/traces/tr-rich", headers=_as(ALICE, PROJECT_A)).json()["data"]
    viewer = client.get(f"{OBS}/traces/tr-rich", headers=_as(VIEWER, PROJECT_A))
    assert SECRET not in viewer.text
    for span in viewer.json()["data"]["spans"]:
        attributes = span["attributes"]
        for key in (
            "mlflow.spanInputs",
            "mlflow.spanOutputs",
            "mlflow.chat.messages",
            "caliber.tool.input",
            "caliber.tool.output",
        ):
            assert attributes[key] == f"[redacted:attributes.{key}]", key
        reference = _span_by_id(editor)[span["span_id"]]["attributes"]
        for key in (
            "caliber.run_id",
            "caliber.tokens",
            "caliber.cost_usd",
            "caliber.model",
            "mlflow.spanType",
        ):
            assert attributes[key] == reference[key], f"{key} is not a payload"
        assert set(attributes) == set(reference)


def test_metrics_are_built_from_redacted_summaries(
    client: TestClient, traced: FakeMlflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.routes import observability as routes

    seen: list[list[dict[str, Any]]] = []
    real = routes._bucketize

    def capture(summaries: list[dict[str, Any]], since_ms: Any) -> Any:
        seen.append(summaries)
        return real(summaries, since_ms)

    monkeypatch.setattr(routes, "_bucketize", capture)
    assert client.get(f"{OBS}/metrics", headers=_as(VIEWER, PROJECT_A)).status_code == 200
    rich = next(t for t in seen[-1] if t["trace_id"] == "tr-rich")
    assert rich["request_preview"] == "[redacted:request_preview]"
    assert client.get(f"{OBS}/metrics", headers=_as(ALICE, PROJECT_A)).status_code == 200
    assert SECRET in next(t for t in seen[-1] if t["trace_id"] == "tr-rich")["request_preview"]


def test_one_request_can_allow_one_experiment_and_refuse_another(
    client: TestClient, traced: FakeMlflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alice reads her project's payloads but not a public experiment's: the decision is per experiment."""
    import mlflow as mlflow_module

    secret_public = "SECRET-PUBLIC-9921"
    traced.traces["tr-pub"] = _trace("tr-pub", "exp-public", text=secret_public)
    names = {"exp-A": "exp-A", "exp-public": "exp-public"}
    monkeypatch.setattr(
        mlflow_module,
        "get_experiment_by_name",
        lambda name: SimpleNamespace(experiment_id=names[name]) if name in names else None,
    )
    clear_resolution_cache()
    response = client.get(f"{OBS}/traces", headers=_as(ALICE, PROJECT_A))
    by_id = {t["trace_id"]: t for t in response.json()["data"]["traces"]}
    assert SECRET in by_id["tr-rich"]["request_preview"], "her project's experiment"
    assert by_id["tr-pub"]["request_preview"] == "[redacted:request_preview]", "the public one"


def test_a_trace_with_no_experiment_is_redacted_even_for_an_administrator(
    client: TestClient, traced: FakeMlflow
) -> None:
    orphan = _trace("tr-none", "exp-A", text="SECRET-NONE-5531")
    orphan.info.trace_location = SimpleNamespace(
        mlflow_experiment=SimpleNamespace(experiment_id=None)
    )
    traced.traces["tr-none"] = orphan
    traced._honours = (
        False  # the fake store returns every trace, as a store that ignores locations would
    )
    listed = client.get(f"{OBS}/traces")  # the suite's default user is an administrator
    by_id = {t["trace_id"]: t for t in listed.json()["data"]["traces"]}
    assert by_id["tr-none"]["request_preview"] == "[redacted:request_preview]"
    assert "SECRET-NONE-5531" not in listed.text


def test_the_run_trace_route_redacts_for_a_viewer_and_not_for_an_editor(
    client: TestClient, traced: FakeMlflow, db_session: Session
) -> None:
    from caliber.db.models import CaliberWorkflowRun

    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="WR-red",
            workflow_id="wf-a",
            project_id=PROJECT_A,
            status="completed",
            trace_id="tr-rich",
            created_by=ALICE,
        )
    )
    db_session.commit()
    path = "/ajax-api/2.0/mlflow/caliber/workflow-runs/WR-red/trace"
    editor = client.get(path, headers=_as(ALICE, PROJECT_A))
    assert editor.status_code == 200 and SECRET in editor.text
    viewer = client.get(path, headers=_as(VIEWER, PROJECT_A))
    assert viewer.status_code == 200, viewer.text
    assert SECRET not in viewer.text
    assert viewer.json()["data"]["spans"][0]["inputs"] == "[redacted:inputs]"
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "development"})
    assert SECRET in client.get(path, headers=_as(VIEWER, PROJECT_A)).text


def test_a_run_outside_any_project_belongs_to_the_user_who_started_it(
    world: Session,
) -> None:
    from caliber.observability.trace_access import can_read_run_payloads

    assert can_read_run_payloads(world, _identity("@owner"), project_id=None, created_by="@owner")
    assert not can_read_run_payloads(
        world, _identity("@owner"), project_id=None, created_by="@other"
    )
    assert not can_read_run_payloads(world, _identity("@owner"), project_id=None, created_by=None)
    assert not can_read_run_payloads(
        world, _identity("@viewer"), project_id=PROJECT, created_by="@x"
    )
    assert can_read_run_payloads(world, _identity("@editor"), project_id=PROJECT, created_by="@x")
    assert not can_read_run_payloads(
        world, _identity("@editor"), project_id="P-gone", created_by="@x"
    )


def test_a_missing_config_is_governed_and_a_database_failure_is_a_refusal(
    client: TestClient, traced: FakeMlflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert payload_governance_active(None) is True
    from caliber.routes import observability as routes

    def no_session(_request: Any) -> Any:
        def broken() -> Any:
            raise RuntimeError("no database")

        return broken

    monkeypatch.setattr(routes, "get_session_factory", no_session)
    monkeypatch.setattr(routes, "resolve_identity", lambda _request: _identity("@owner"))
    gate = routes._payload_gate(SimpleNamespace(app=client.app))  # type: ignore[arg-type]
    assert gate("exp-A") is False


def test_a_run_whose_owner_cannot_be_decided_is_refused(
    world: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.observability import trace_access
    from caliber.observability.trace_access import can_read_run_payloads

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(trace_access, "_owner_allows_payloads", broken)
    assert (
        can_read_run_payloads(world, _identity("@editor"), project_id=PROJECT, created_by="@x")
        is False
    )


def test_a_span_whose_attributes_are_not_a_dict_is_left_alone() -> None:
    assert redact_trace_detail({"spans": [{"span_id": "s", "attributes": ["x"]}]}, False) == {
        "spans": [{"span_id": "s", "attributes": ["x"]}]
    }
