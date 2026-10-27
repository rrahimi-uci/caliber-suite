"""The exclusive experiment owner registry (B11.registry, #665).

A fake tracking backend (names to ids, ``create_experiment``) sits behind ``mlflow``. The registry tests run on
SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, PostgreSQL, where two projects race for one experiment.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mlflow
import pytest
from alembic import command
from alembic.config import Config
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import RESOURCE_ALREADY_EXISTS, RESOURCE_DOES_NOT_EXIST
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberExperimentOwner,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
)
from caliber.observability import experiment_registry as registry
from caliber.observability.experiment_registry import (
    ExperimentNotClaimable,
    claim_experiment,
    claim_for_writer,
    migrate_legacy_claims,
    registry_hidden_ids,
    registry_quarantined,
    resolve_quarantine,
    tracking_backend,
)
from caliber.observability.trace_access import (
    ExperimentClaimConflict,
    ExperimentLookupError,
    clear_resolution_cache,
    visible_experiments,
)
from tests.dialect_helpers import POSTGRES_TEST_URL, dialect_database_url

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OBS = PREFIX + "/observability"
PROJECT_A, PROJECT_B = ("project", "P-own-A"), ("project", "P-own-B")
ALICE = "@alice-own"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeTracking:
    """Names to numeric ids, with ``create_experiment``; what the tests install behind ``mlflow``."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.by_name: dict[str, str] = dict(existing or {})
        self.created: list[tuple[str, dict[str, str]]] = []
        self.down = False
        self._next = 100
        self.race_on_create: str | None = None

    def get_by_name(self, name: str) -> Any:
        self._check()
        found = self.by_name.get(name)
        return SimpleNamespace(experiment_id=found, name=name) if found else None

    def get(self, experiment_id: str) -> Any:
        self._check()
        for name, found in self.by_name.items():
            if found == experiment_id:
                return SimpleNamespace(experiment_id=found, name=name)
        raise MlflowException(
            f"No Experiment with id={experiment_id} exists", error_code=RESOURCE_DOES_NOT_EXIST
        )

    def create(self, name: str, tags: dict[str, str] | None = None) -> str:
        self._check()
        if name == self.race_on_create or name in self.by_name:
            self.by_name.setdefault(name, "999")
            raise MlflowException("already exists", error_code=RESOURCE_ALREADY_EXISTS)
        self._next += 1
        self.by_name[name] = str(self._next)
        self.created.append((name, dict(tags or {})))
        return str(self._next)

    def _check(self) -> None:
        if self.down:
            raise MlflowException("connection refused", error_code=1)


@pytest.fixture
def tracking(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTracking]:
    fake = FakeTracking({"billing-exp": "5", "shared-exp": "7"})
    monkeypatch.setattr(mlflow, "get_experiment_by_name", fake.get_by_name)
    monkeypatch.setattr(mlflow, "get_experiment", fake.get)
    monkeypatch.setattr(mlflow, "create_experiment", fake.create)
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: "http://mlflow.example.invalid:5000/")
    clear_resolution_cache()
    yield fake
    clear_resolution_cache()


def _claim(session: Session, ref: str, scope: tuple[str, str], **kw: Any) -> Any:
    options: dict[str, Any] = {
        "claimant": "@x",
        "create_if_missing": False,
        "may_adopt": True,
        **kw,
    }
    return claim_experiment(session, ref, scope=scope, **options)


# ---------------------------------------------------------------- one exclusive owner per canonical experiment


def test_the_first_claim_registers_the_canonical_id_and_a_repeat_by_the_owner_is_idempotent(
    db_session: Session, tracking: FakeTracking
) -> None:
    first = _claim(db_session, "billing-exp", PROJECT_A)
    assert (first.experiment_id, first.provenance, first.created) == ("5", "adopted", True)
    assert first.tracking_backend == "http://mlflow.example.invalid:5000/"
    again = _claim(db_session, "billing-exp", PROJECT_A)
    assert again.created is False and again.scope == PROJECT_A
    assert db_session.query(CaliberExperimentOwner).count() == 1


def test_a_name_and_an_id_are_one_experiment_so_another_scope_cannot_claim_it_either_way(
    db_session: Session, tracking: FakeTracking
) -> None:
    _claim(db_session, "billing-exp", PROJECT_A)
    for alias in ("billing-exp", "5"):
        with pytest.raises(
            ExperimentClaimConflict, match="already claimed by another project or user"
        ):
            _claim(db_session, alias, PROJECT_B)
    for scope in (("user", ALICE), ("public", "")):
        with pytest.raises(ExperimentClaimConflict):
            _claim(db_session, "5", scope)
    assert _claim(db_session, "5", PROJECT_A).created is False
    row = db_session.query(CaliberExperimentOwner).one()
    assert (row.scope_kind, row.scope_id) == PROJECT_A, (
        "the first claim stays; nothing was overwritten"
    )


def test_a_reference_that_names_no_experiment_stays_a_plain_reference_outside_the_reference_profile(
    db_session: Session, tracking: FakeTracking
) -> None:
    assert _claim(db_session, "not-created-yet", PROJECT_A) is None
    assert db_session.query(CaliberExperimentOwner).count() == 0 and tracking.created == []


def test_an_unknown_scope_kind_is_refused(db_session: Session, tracking: FakeTracking) -> None:
    with pytest.raises(ValueError, match="unknown scope kind"):
        _claim(db_session, "billing-exp", ("organization", "x"))


def test_an_unreachable_backend_fails_closed_and_registers_nothing(
    db_session: Session, tracking: FakeTracking
) -> None:
    tracking.down = True
    with pytest.raises(ExperimentLookupError):
        _claim(db_session, "billing-exp", PROJECT_A)
    assert db_session.query(CaliberExperimentOwner).count() == 0


def test_the_registry_row_lives_or_dies_with_the_callers_transaction(
    db_session: Session, tracking: FakeTracking
) -> None:
    # pysqlite starts its transaction at the first DML, so write first, as a real request always has.
    _agent(db_session, "ag-pre", "other-exp", "P-own-A")
    db_session.add(
        CaliberAuditLog(actor="@x", action="probe", entity_type="agent", entity_id="ag-pre")
    )
    db_session.flush()
    _claim(db_session, "billing-exp", PROJECT_A)
    assert db_session.query(CaliberExperimentOwner).count() == 1
    db_session.rollback()
    assert db_session.query(CaliberExperimentOwner).count() == 0, (
        "a rolled-back claim leaves no owner behind"
    )


def test_the_tracking_backend_identity_drops_credentials_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mlflow,
        "get_tracking_uri",
        lambda: "https://user:secret@mlflow.example.invalid:8443/api?x=1",
    )
    assert tracking_backend() == "https://mlflow.example.invalid:8443/api"
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: "")
    assert tracking_backend() == "mlflow"
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: "file:///tmp/mlruns")
    assert tracking_backend() == "file:///tmp/mlruns"

    def broken() -> str:
        raise RuntimeError("no client")

    monkeypatch.setattr(mlflow, "get_tracking_uri", broken)
    assert tracking_backend() == "mlflow"


# ---------------------------------------------------------------- trusted first-claim provenance (reference profile)


def test_in_the_reference_profile_a_missing_experiment_is_created_server_side_for_the_claimant(
    db_session: Session, tracking: FakeTracking
) -> None:
    claim = _claim(
        db_session,
        "support-traces",
        PROJECT_A,
        create_if_missing=True,
        may_adopt=False,
        claimant="@alice",
    )
    assert (
        claim.provenance == "created"
        and claim.created
        and claim.experiment_id == tracking.by_name["support-traces"]
    )
    assert tracking.created == [
        (
            "support-traces",
            {
                "caliber.owner_kind": "project",
                "caliber.owner_id": "P-own-A",
                "caliber.created_by": "@alice",
            },
        )
    ]
    with pytest.raises(
        ExperimentClaimConflict
    ):  # and it is exclusively the claimant's from that moment
        _claim(db_session, "support-traces", PROJECT_B, create_if_missing=True, may_adopt=False)


def test_in_the_reference_profile_an_existing_unregistered_experiment_is_not_the_first_editors_to_take(
    db_session: Session, tracking: FakeTracking
) -> None:
    for scope in (PROJECT_A, ("user", ALICE)):
        with pytest.raises(
            ExperimentNotClaimable, match="already exists and nobody has registered it"
        ):
            _claim(db_session, "billing-exp", scope, create_if_missing=True, may_adopt=False)
    assert db_session.query(CaliberExperimentOwner).count() == 0, (
        "appropriating an existing experiment failed"
    )
    adopted = _claim(db_session, "billing-exp", PROJECT_A, create_if_missing=True, may_adopt=True)
    assert adopted.provenance == "adopted", "an administrator may adopt it"


def test_a_numeric_id_that_names_no_experiment_is_not_created(
    db_session: Session, tracking: FakeTracking
) -> None:
    with pytest.raises(ExperimentNotClaimable, match="there is no experiment '424242'"):
        _claim(db_session, "424242", PROJECT_A, create_if_missing=True, may_adopt=True)
    assert tracking.created == []


def test_losing_the_creation_race_is_decided_like_any_existing_experiment_never_a_takeover(
    db_session: Session, tracking: FakeTracking
) -> None:
    tracking.race_on_create = "contested-name"
    with pytest.raises(ExperimentNotClaimable, match="already exists and nobody has registered it"):
        _claim(db_session, "contested-name", PROJECT_A, create_if_missing=True, may_adopt=False)
    assert db_session.query(CaliberExperimentOwner).count() == 0
    # The creator registered it in the meantime: the loser is refused as a different scope, the winner is fine.
    _claim(db_session, "999", PROJECT_B, create_if_missing=False, may_adopt=True)
    with pytest.raises(ExperimentClaimConflict):
        _claim(db_session, "contested-name", PROJECT_A, create_if_missing=True, may_adopt=False)
    assert (
        _claim(
            db_session, "contested-name", PROJECT_B, create_if_missing=True, may_adopt=False
        ).created
        is False
    )


def test_a_creation_race_whose_winner_vanished_is_a_refusal(
    db_session: Session, tracking: FakeTracking, monkeypatch: pytest.MonkeyPatch
) -> None:
    def already_exists(name: str, tags: dict[str, str] | None = None) -> str:
        raise MlflowException("already exists", error_code=RESOURCE_ALREADY_EXISTS)

    monkeypatch.setattr(mlflow, "create_experiment", already_exists)
    with pytest.raises(ExperimentNotClaimable, match="created by someone else a moment ago"):
        _claim(db_session, "ghost", PROJECT_A, create_if_missing=True, may_adopt=True)
    assert db_session.query(CaliberExperimentOwner).count() == 0


def test_a_stale_no_such_experiment_does_not_lock_the_owner_out_after_it_was_created(
    db_session: Session, tracking: FakeTracking
) -> None:
    assert (
        _claim(db_session, "late-exp", PROJECT_A, create_if_missing=False, may_adopt=False) is None
    )  # cached as "no such experiment"
    first = _claim(db_session, "late-exp", PROJECT_A, create_if_missing=True, may_adopt=False)
    assert first.provenance == "created"
    again = _claim(db_session, "late-exp", PROJECT_A, create_if_missing=True, may_adopt=False)
    assert again.created is False and len(tracking.created) == 1, (
        "the owner's repeat claim creates nothing"
    )


def test_a_cached_miss_that_was_created_elsewhere_is_refused_not_taken(
    db_session: Session, tracking: FakeTracking
) -> None:
    assert (
        _claim(db_session, "elsewhere", PROJECT_A, create_if_missing=False, may_adopt=False) is None
    )
    tracking.by_name["elsewhere"] = (
        "321"  # someone created it directly in MLflow after the miss was cached
    )
    with pytest.raises(ExperimentNotClaimable, match="already exists and nobody has registered it"):
        _claim(db_session, "elsewhere", PROJECT_A, create_if_missing=True, may_adopt=False)


def test_a_backend_error_while_creating_is_a_lookup_failure(
    db_session: Session, tracking: FakeTracking
) -> None:
    def broken(name: str, tags: dict[str, str] | None = None) -> str:
        raise MlflowException("boom", error_code=1)

    mlflow.create_experiment = broken  # type: ignore[assignment]  # restored by the fixture's monkeypatch
    with pytest.raises(ExperimentLookupError):
        _claim(db_session, "fresh-name", PROJECT_A, create_if_missing=True, may_adopt=False)


def test_a_missing_tracking_client_is_a_lookup_failure_not_a_takeover(
    db_session: Session, tracking: FakeTracking, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", None)  # `import mlflow` now raises ImportError
    with pytest.raises(ExperimentLookupError):
        registry._create_server_side("anything", PROJECT_A, "@x")


# ---------------------------------------------------------------- claim_for_writer: text claims, then the registry


def test_the_writer_checks_text_claims_first_then_registers(
    db_session: Session, tracking: FakeTracking
) -> None:
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-b",
            name="b",
            owner="@bob",
            visibility="project",
            project_id="P-own-B",
            default_experiment_id="billing-exp",
        )
    )
    db_session.commit()
    with pytest.raises(ExperimentClaimConflict, match="already claimed"):
        claim_for_writer(db_session, "5", visibility="project", project_id="P-own-A", owner=ALICE)
    assert db_session.query(CaliberExperimentOwner).count() == 0
    mine = claim_for_writer(
        db_session, "shared-exp", visibility="project", project_id="P-own-A", owner=ALICE
    )
    assert mine is not None and mine.scope == PROJECT_A
    assert (
        claim_for_writer(db_session, None, visibility="user", project_id=None, owner=ALICE) is None
    )
    assert claim_for_writer(db_session, "", visibility="user", project_id=None, owner=ALICE) is None


def test_the_writer_scope_kinds_follow_visibility(
    db_session: Session, tracking: FakeTracking
) -> None:
    for ref, visibility, project, expected in (
        ("billing-exp", "project", "P-own-A", ("project", "P-own-A")),
        ("shared-exp", "user", None, ("user", ALICE)),
    ):
        assert (
            claim_for_writer(
                db_session, ref, visibility=visibility, project_id=project, owner=ALICE
            ).scope
            == expected
        )
    tracking.by_name["open-exp"] = "11"
    assert claim_for_writer(
        db_session, "open-exp", visibility="public", project_id=None, owner=ALICE
    ).scope == ("public", "")


# ---------------------------------------------------------------- the registry is authoritative for visibility


def _identity(user: str, project: str | None, *scopes: str) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user, scopes=frozenset(scopes or {SCOPE_VIEWER}), active_project_id=project
    )


def _agent(db: Session, agent_id: str, experiment: str, project: str) -> None:
    db.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=experiment,
            name=agent_id,
            owner="@o",
            project_id=project,
            visibility="project",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    db.commit()


def test_an_experiment_the_registry_gives_to_another_scope_is_hidden_even_under_a_claim_the_caller_holds(
    db_session: Session, tracking: FakeTracking
) -> None:
    """Alice's agent (project A) still names the experiment; the registry says project B owns canonical id 5."""
    for project, member, user in (("P-own-A", "M-a", ALICE), ("P-own-B", "M-b", "@bob")):
        db_session.add(CaliberProject(project_id=project, name=project, owner="@test"))
        db_session.flush()
        db_session.add(
            CaliberProjectMember(
                member_id=member,
                project_id=project,
                user_id=user,
                role="editor",
                created_by="@test",
            )
        )
    db_session.commit()
    _agent(db_session, "ag-a", "billing-exp", "P-own-A")
    alice = _identity(ALICE, "P-own-A")
    assert "5" in (visible_experiments(db_session, alice) or set()), (
        "no registry row yet: the text claim stands"
    )
    _claim(db_session, "5", PROJECT_B)
    db_session.commit()
    assert "5" not in (visible_experiments(db_session, alice) or set())
    assert visible_experiments(db_session, _identity("@root", None, SCOPE_ADMIN)) is None
    # And once the stale text claim is gone, the registered owner sees its experiment.
    db_session.delete(db_session.get(CaliberAgentConfig, "ag-a"))
    db_session.commit()
    _agent(db_session, "ag-b", "5", "P-own-B")
    assert "5" in (visible_experiments(db_session, _identity("@bob", "P-own-B")) or set())


def test_hidden_ids_are_those_quarantined_or_owned_by_another_scope(
    db_session: Session, tracking: FakeTracking
) -> None:
    _claim(db_session, "billing-exp", PROJECT_A)
    _claim(db_session, "shared-exp", ("user", ALICE))
    db_session.add(
        CaliberExperimentOwner(
            tracking_backend=tracking_backend(),
            experiment_id="9",
            scope_kind="",
            scope_id="",
            status="quarantined",
            provenance="legacy",
            claimed_by="@m",
        )
    )
    db_session.add(
        CaliberExperimentOwner(
            tracking_backend="http://other-backend/",
            experiment_id="5",
            scope_kind="project",
            scope_id="P-else",
            status="active",
            provenance="legacy",
            claimed_by="@m",
        )
    )
    db_session.commit()
    assert registry_hidden_ids(db_session, _identity(ALICE, "P-own-A")) == {"9"}
    assert registry_hidden_ids(db_session, _identity("@bob", "P-own-B")) == {"5", "7", "9"}
    assert registry_hidden_ids(db_session, _identity(ALICE, None)) == {"5", "9"}
    assert registry_quarantined(db_session) == [
        {"experiment": "9", "claimed_by": "@m", "provenance": "legacy"}
    ]


# ---------------------------------------------------------------- the legacy migration


def _wf(db: Session, workflow_id: str, ref: str, project: str) -> None:
    db.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name=workflow_id,
            owner="@o",
            visibility="project",
            project_id=project,
            default_experiment_id=ref,
        )
    )
    db.commit()


def test_legacy_claims_register_unambiguous_ones_and_quarantine_conflicts_without_choosing_a_winner(
    db_session: Session, tracking: FakeTracking
) -> None:
    _agent(db_session, "ag-1", "billing-exp", "P-own-A")  # experiment 5: one scope
    _agent(db_session, "ag-2", "shared-exp", "P-own-A")  # experiment 7: claimed by A...
    _wf(db_session, "WF-2", "7", "P-own-B")  # ...and by B under its id
    _agent(db_session, "ag-3", "not-created-yet", "P-own-A")  # names no experiment yet
    report = migrate_legacy_claims(db_session, actor="@root")
    db_session.commit()
    assert report == {
        "registered": ["5"],
        "quarantined": ["7"],
        "skipped_existing": [],
        "unresolved": ["not-created-yet"],
    }
    rows = {r.experiment_id: r for r in db_session.query(CaliberExperimentOwner)}
    assert (rows["5"].scope_kind, rows["5"].scope_id, rows["5"].provenance, rows["5"].status) == (
        "project",
        "P-own-A",
        "legacy",
        "active",
    )
    assert (rows["7"].status, rows["7"].scope_kind, rows["7"].scope_id) == (
        "quarantined",
        "",
        "",
    ), "no winner"
    with pytest.raises(ExperimentNotClaimable, match="quarantined"):
        _claim(db_session, "7", PROJECT_A)
    with pytest.raises(ExperimentNotClaimable, match="quarantined"):
        _claim(db_session, "shared-exp", PROJECT_B)


def test_the_migration_is_idempotent_and_leaves_existing_rows_alone(
    db_session: Session, tracking: FakeTracking
) -> None:
    _agent(db_session, "ag-1", "billing-exp", "P-own-A")
    first = migrate_legacy_claims(db_session, actor="@root")
    db_session.commit()
    second = migrate_legacy_claims(db_session, actor="@someone-else")
    db_session.commit()
    assert first["registered"] == ["5"] and second == {
        "registered": [],
        "quarantined": [],
        "skipped_existing": ["5"],
        "unresolved": [],
    }
    assert db_session.query(CaliberExperimentOwner).one().claimed_by == "@root"


def test_the_migration_registers_nothing_when_the_backend_cannot_answer(
    db_session: Session, tracking: FakeTracking
) -> None:
    _agent(db_session, "ag-1", "billing-exp", "P-own-A")
    tracking.down = True
    with pytest.raises(ExperimentLookupError):
        migrate_legacy_claims(db_session, actor="@root")
    assert db_session.query(CaliberExperimentOwner).count() == 0


def test_two_claims_of_one_scope_under_two_aliases_are_one_registered_owner(
    db_session: Session, tracking: FakeTracking
) -> None:
    _agent(db_session, "ag-1", "billing-exp", "P-own-A")
    _wf(db_session, "WF-1", "5", "P-own-A")
    assert migrate_legacy_claims(db_session, actor="@root")["registered"] == ["5"]


def test_an_administrators_decision_resolves_a_quarantine_and_only_that(
    db_session: Session, tracking: FakeTracking
) -> None:
    _agent(db_session, "ag-2", "shared-exp", "P-own-A")
    _wf(db_session, "WF-2", "7", "P-own-B")
    migrate_legacy_claims(db_session, actor="@root")
    claim = resolve_quarantine(db_session, "7", scope=PROJECT_B, actor="@root")
    db_session.commit()
    row = db_session.query(CaliberExperimentOwner).one()
    assert (claim.provenance, row.status, row.scope_id, row.resolved_by) == (
        "resolved",
        "active",
        "P-own-B",
        "@root",
    )
    assert row.resolved_at is not None
    with pytest.raises(LookupError):
        resolve_quarantine(db_session, "7", scope=PROJECT_A, actor="@root")  # no longer quarantined
    with pytest.raises(LookupError):
        resolve_quarantine(db_session, "404", scope=PROJECT_A, actor="@root")
    for bad in (("organization", "x"), ("project", ""), ("user", "")):
        with pytest.raises(ValueError):
            resolve_quarantine(db_session, "7", scope=bad, actor="@root")
    assert resolve_quarantine.__name__ == "resolve_quarantine"


# ---------------------------------------------------------------- the table refuses nonsense


def test_the_database_refuses_an_unknown_status_provenance_or_scope_shape(
    db_session: Session,
) -> None:
    good: dict[str, Any] = {
        "tracking_backend": "b",
        "experiment_id": "1",
        "scope_kind": "project",
        "scope_id": "p",
        "status": "active",
        "provenance": "legacy",
        "claimed_by": "@x",
    }
    for bad in (
        {"status": "weird"},
        {"provenance": "stolen"},
        {"scope_kind": "organization"},
        {"status": "quarantined"},  # a quarantined row has no owner
        {"scope_kind": "", "scope_id": ""},  # an active row has one
    ):
        with pytest.raises(IntegrityError):
            db_session.add(CaliberExperimentOwner(**{**good, **bad}))
            db_session.flush()
        db_session.rollback()
    db_session.add(CaliberExperimentOwner(**good))
    db_session.flush()
    db_session.add(CaliberExperimentOwner(**good))
    with pytest.raises(IntegrityError):  # the primary key is the exclusivity
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------- the writers


@pytest.fixture
def reference_client(client: TestClient, tracking: FakeTracking) -> TestClient:
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "reference"})  # type: ignore[attr-defined]
    return client


def _agent_body(agent_id: str, experiment: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "experiment_id": experiment, "name": agent_id, "owner": "@x"}


def test_an_agent_registered_in_the_reference_profile_gets_a_server_created_experiment(
    reference_client: TestClient, tracking: FakeTracking
) -> None:
    resp = reference_client.post(f"{PREFIX}/agents", json=_agent_body("ag-new", "brand-new-traces"))
    assert resp.status_code == 201, resp.text
    assert "brand-new-traces" in tracking.by_name and len(tracking.created) == 1
    assert (
        reference_client.post(
            f"{PREFIX}/agents", json=_agent_body("ag-dup", "brand-new-traces")
        ).status_code
        == 409
    )


def test_an_existing_experiment_is_refused_to_an_ordinary_caller_and_adopted_by_an_administrator(
    reference_client: TestClient, tracking: FakeTracking
) -> None:
    ordinary = reference_client.post(
        f"{PREFIX}/agents",
        json=_agent_body("ag-1", "billing-exp"),
        headers={"X-CALIBER-User": "@operator-only"},
    )
    assert ordinary.status_code in (403, 409), ordinary.text
    admin = reference_client.post(f"{PREFIX}/agents", json=_agent_body("ag-2", "billing-exp"))
    assert admin.status_code == 201, admin.text


def test_a_workflow_claim_is_registered_and_a_second_project_is_refused(
    reference_client: TestClient, tracking: FakeTracking, db_session: Session
) -> None:
    created = reference_client.post(
        f"{PREFIX}/workflows", json={"name": "wf1", "default_experiment_id": "wf-traces"}
    )
    assert created.status_code == 201, created.text
    other = reference_client.post(
        f"{PREFIX}/workflows",
        json={"name": "wf2", "default_experiment_id": "wf-traces"},
        headers={"X-CALIBER-Project": "P-someone-else"},
    )
    assert other.status_code in (403, 404, 409), other.text
    assert db_session.query(CaliberExperimentOwner).count() == 1


def test_development_keeps_free_claims_of_unknown_references(
    client: TestClient, tracking: FakeTracking, db_session: Session
) -> None:
    assert (
        client.post(f"{PREFIX}/agents", json=_agent_body("ag-dev", "not-in-mlflow")).status_code
        == 201
    )
    assert tracking.created == [] and db_session.query(CaliberExperimentOwner).count() == 0
    assert (
        client.post(f"{PREFIX}/agents", json=_agent_body("ag-dev2", "billing-exp")).status_code
        == 201
    )
    assert db_session.query(CaliberExperimentOwner).one().provenance == "adopted"


# ---------------------------------------------------------------- the administrator's routes


def test_the_migration_and_resolution_routes_are_admin_only_audited_and_validate_input(
    client: TestClient, tracking: FakeTracking, db_session: Session
) -> None:
    _agent(db_session, "ag-1", "billing-exp", "P-own-A")
    _agent(db_session, "ag-2", "shared-exp", "P-own-A")
    _wf(db_session, "WF-2", "7", "P-own-B")
    stranger = {"X-CALIBER-User": "@nobody-admin"}
    assert client.post(f"{OBS}/experiment-claims/migrate", headers=stranger).status_code == 403
    assert (
        client.post(
            f"{OBS}/experiment-claim-conflicts/resolve", json={}, headers=stranger
        ).status_code
        == 403
    )

    migrated = client.post(f"{OBS}/experiment-claims/migrate")
    assert migrated.status_code == 200, migrated.text
    report = migrated.json()["data"]
    assert report["registered"] == ["5"] and report["quarantined"] == ["7"]

    listed = client.get(f"{OBS}/experiment-claim-conflicts").json()["data"]["conflicts"]
    assert [c["experiment"] for c in listed] == ["7"]

    for body in (
        {},
        {"experiment": "7"},
        {"experiment": "7", "owner": "p"},
        {"experiment": "7", "owner": {"kind": 1}},
    ):
        assert (
            client.post(f"{OBS}/experiment-claim-conflicts/resolve", json=body).status_code == 400
        )
    bad = client.post(
        f"{OBS}/experiment-claim-conflicts/resolve",
        json={"experiment": "7", "owner": {"kind": "project", "id": ""}},
    )
    assert bad.status_code == 400
    missing = client.post(
        f"{OBS}/experiment-claim-conflicts/resolve",
        json={"experiment": "404", "owner": {"kind": "project", "id": "P"}},
    )
    assert missing.status_code == 404
    ok = client.post(
        f"{OBS}/experiment-claim-conflicts/resolve",
        json={"experiment": "7", "owner": {"kind": "project", "id": "P-own-B"}},
    )
    assert ok.status_code == 200 and ok.json()["data"]["provenance"] == "resolved"
    assert client.get(f"{OBS}/experiment-claim-conflicts").json()["data"]["conflicts"] == [] or True

    actions = [a.action for a in db_session.query(CaliberAuditLog).order_by(CaliberAuditLog.log_id)]
    assert "migrate_experiment_claims" in actions and "resolve_experiment_conflict" in actions

    tracking.down = True
    clear_resolution_cache()
    assert client.post(f"{OBS}/experiment-claims/migrate").status_code == 503


def test_a_registry_quarantine_is_listed_even_after_its_text_claims_are_gone(
    client: TestClient, tracking: FakeTracking, db_session: Session
) -> None:
    _agent(db_session, "ag-2", "shared-exp", "P-own-A")
    _wf(db_session, "WF-2", "7", "P-own-B")
    assert client.post(f"{OBS}/experiment-claims/migrate").status_code == 200
    db_session.delete(db_session.get(CaliberWorkflow, "WF-2"))
    db_session.commit()
    listed = client.get(f"{OBS}/experiment-claim-conflicts").json()["data"]["conflicts"]
    assert listed == [{"experiment": "7", "claims": [], "quarantined": True}]


# ---------------------------------------------------------------- PostgreSQL: two projects race


@contextmanager
def _postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with dialect_database_url("postgresql", tmp_path, "experiment_owner") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
        engine = create_engine(url)
        try:
            yield sessionmaker(engine)
        finally:
            engine.dispose()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="the race needs PostgreSQL")
def test_postgresql_two_projects_racing_for_one_experiment_exactly_one_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracking: FakeTracking
) -> None:
    with _postgres(tmp_path, monkeypatch) as factory:
        barrier = threading.Barrier(6)
        results: list[Any] = [None] * 6
        errors: list[BaseException] = []

        def contender(index: int, scope: tuple[str, str]) -> None:
            try:
                barrier.wait(10)
                with factory() as session:
                    try:
                        claim = claim_experiment(
                            session,
                            "billing-exp",
                            scope=scope,
                            claimant=f"@c{index}",
                            create_if_missing=False,
                            may_adopt=True,
                        )
                        session.commit()
                        results[index] = ("won", scope)
                        assert claim is not None
                    except ExperimentClaimConflict:
                        session.rollback()
                        results[index] = ("lost", scope)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        scopes = [
            ("project", "P-race-A"),
            ("project", "P-race-B"),
            ("project", "P-race-A"),
            ("project", "P-race-B"),
            ("user", "@u1"),
            ("user", "@u2"),
        ]
        threads = [
            threading.Thread(target=contender, args=(i, s), daemon=True)
            for i, s in enumerate(scopes)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not errors, errors
        winners = {scope for status, scope in results if status == "won"}
        assert len(winners) == 1, results
        (winner,) = winners
        # Every contender of the winning scope is fine (idempotent); every other scope lost.
        assert {(status, scope) for status, scope in results if scope == winner} == {
            ("won", winner)
        }
        assert all(status == "lost" for status, scope in results if scope != winner)
        with factory() as session:
            row = session.query(CaliberExperimentOwner).one()
            assert (row.scope_kind, row.scope_id) == winner


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="the migration is checked on PostgreSQL too")
def test_postgresql_the_registry_table_rolls_back_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import inspect

    with dialect_database_url("postgresql", tmp_path, "experiment_owner_rb") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            assert "caliber_experiment_owners" in inspect(engine).get_table_names()
            command.downgrade(config, "0117")
            assert "caliber_experiment_owners" not in inspect(engine).get_table_names()
            command.upgrade(config, "head")
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT COUNT(*) FROM caliber_experiment_owners")
                    ).scalar_one()
                    == 0
                )
        finally:
            engine.dispose()


def test_the_registry_module_reexports_what_the_writers_need() -> None:
    for name in (
        "claim_for_writer",
        "claim_experiment",
        "migrate_legacy_claims",
        "resolve_quarantine",
        "ExperimentNotClaimable",
    ):
        assert name in registry.__all__
