"""Run-bound grants: exact ids, explicit sets, rechecked authority, revocation that cannot be raced (B03.2, #664).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity (PostgreSQL)" job),
on PostgreSQL, against a database migrated by Alembic so the migration's triggers are under test, not just the ORM.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberWorkflowRun,
)
from caliber.policy import grants
from caliber.policy.grants import (
    ACTION_NOT_ALLOWED,
    AUTHORITY_UNAVAILABLE,
    DENIAL_CODES,
    GRANT_EXPIRED,
    GRANT_NOT_FOUND,
    GRANT_REVOKED,
    NOT_A_MEMBER,
    RESOURCE_NOT_ALLOWED,
    GrantDenied,
    GrantError,
    authorize,
    create_grant,
    load_grant,
    revoke_grant,
)
from caliber.policy.principal import Subject, Workload
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
pytestmark = pytest.mark.slow

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
DIGEST = hashlib.sha256(b"workflow-version").hexdigest()
ACTIONS = ["workflow.run", "release.promote"]
RESOURCES = ["workflow:wf-1", "prompt:support"]


@contextmanager
def _migrated(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str = "head"
) -> Iterator[tuple[Engine, Config]]:
    with dialect_database_url(dialect, tmp_path, "run_grants") as db_url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
        monkeypatch.chdir(PROJECT_ROOT)
        config = Config(str(ALEMBIC_INI))
        command.upgrade(config, revision)
        engine = create_engine(db_url)
        try:
            yield engine, config
        finally:
            engine.dispose()


def _subject(**overrides: Any) -> Subject:
    return Subject(
        **{"user_id": "alice", "auth_method": "session", "project_id": "proj-1", **overrides}
    )


def _workload() -> Workload:
    return Workload.for_workflow("triage", DIGEST)


def _seed(engine: Engine, *, member: str | None = "alice", status: str = "active") -> str:
    """A project, an optional member and a run; returns the run id."""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    with Session(engine) as session, session.begin():
        if session.get(CaliberProject, "proj-1") is None:
            session.add(CaliberProject(project_id="proj-1", name="Project One", owner="@owner"))
            session.flush()
        if member is not None and (
            session.query(CaliberProjectMember)
            .filter_by(project_id="proj-1", user_id=member)
            .first()
            is None
        ):
            session.add(
                CaliberProjectMember(
                    member_id=f"m-{member}",
                    project_id="proj-1",
                    user_id=member,
                    role="editor",
                    status=status,
                    created_by="@owner",
                )
            )
        session.add(
            CaliberWorkflowRun(workflow_run_id=run_id, workflow_id="wf-1", manifest_legacy=False)
        )
    return run_id


def _issue(
    engine: Engine,
    run_id: str,
    grant_id: str | None = None,
    *,
    expires: datetime | None = None,
    **overrides: Any,
) -> str:
    grant_id = grant_id or f"grant-{uuid.uuid4().hex[:10]}"
    arguments: dict[str, Any] = {
        "grant_id": grant_id,
        "run_id": run_id,
        "subject": _subject(),
        "workload": _workload(),
        "project_id": "proj-1",
        "allowed_actions": ACTIONS,
        "allowed_resources": RESOURCES,
        "policy_version": "policy-v1",
        "expires_at": expires or NOW + timedelta(hours=1),
        "now": NOW,
        **overrides,
    }
    with Session(engine) as session, session.begin():
        create_grant(session, **arguments)
    return grant_id


def _check(
    engine: Engine,
    run_id: str,
    grant_id: str,
    action: str = "workflow.run",
    resource: str = "workflow:wf-1",
    **kw: Any,
) -> Any:
    with Session(engine) as session, session.begin():
        return authorize(
            session,
            run_id=run_id,
            grant_id=grant_id,
            action=action,
            resource=resource,
            now=kw.pop("now", NOW),
            **kw,
        )


def _denied(code: str) -> Any:
    return pytest.raises(GrantDenied, check=lambda e: e.code == code)


# ---------------------------------------------------------------- issuing and reading back


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_grant_round_trips_as_typed_principals_and_exact_sets(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, allowed_actions=["b.act", "a.act", "a.act"])
        with Session(engine) as session:
            grant = load_grant(session, run_id=run_id, grant_id=grant_id)
        assert grant.grant_id == grant_id and grant.run_id == run_id
        assert grant.subject == _subject() and grant.workload == _workload()
        assert grant.project_id == "proj-1" and grant.policy_version == "policy-v1"
        assert grant.allowed_actions == frozenset({"a.act", "b.act"})
        assert grant.allowed_resources == frozenset(RESOURCES)
        assert grant.expires_at == (NOW + timedelta(hours=1)).replace(tzinfo=None)
        assert grant.revoked_at is None
        assert grant.allows("a.act", "workflow:wf-1") and not grant.allows("c.act", "workflow:wf-1")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"allowed_actions": []}, "allowed_actions must not be empty"),
        ({"allowed_resources": []}, "allowed_resources must not be empty"),
        ({"allowed_actions": ["ok", " padded"]}, "every entry of allowed_actions"),
        ({"allowed_resources": ["", "x"]}, "every entry of allowed_resources"),
        ({"allowed_actions": [3]}, "every entry of allowed_actions"),
        ({"grant_id": " "}, "grant_id"),
        ({"run_id": ""}, "run_id"),
        ({"project_id": "other"}, "project the subject acted in"),
        ({"policy_version": ""}, "policy_version"),
        ({"expires": NOW}, "in the future"),
        ({"expires": NOW - timedelta(seconds=1)}, "in the future"),
        ({"subject": "alice"}, "Subject and a Workload"),
        ({"workload": object()}, "Subject and a Workload"),
    ],
)
def test_a_malformed_grant_is_refused_and_nothing_is_stored(
    overrides: dict[str, Any], message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        expires = overrides.pop("expires", None)
        run_id = overrides.pop("run_id", run_id)
        with pytest.raises(GrantError, match=message):
            _issue(engine, run_id, expires=expires, **overrides)
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_duplicate_grant_id_is_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        _issue(engine, run_id, "g-1")
        with pytest.raises(IntegrityError):
            _issue(engine, run_id, "g-1")


def test_a_subject_with_no_project_context_may_be_granted_in_any_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, subject=_subject(project_id=None))
        assert _check(engine, run_id, grant_id).project_id == "proj-1"


# ---------------------------------------------------------------- exact identifiers, no fallback


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_run_and_grant_ids_must_both_match_and_every_mismatch_is_the_same_answer(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_a, run_b = _seed(engine), _seed(engine)
        grant_a = _issue(engine, run_a)
        assert _check(engine, run_a, grant_a).grant_id == grant_a
        for run_id, grant_id in (
            (run_b, grant_a),
            (run_a, "grant-unknown"),
            ("run-unknown", grant_a),
            ("", ""),
        ):
            with _denied(GRANT_NOT_FOUND) as raised:
                _check(engine, run_id, grant_id)
            assert grant_a not in str(raised.value) or grant_id == grant_a
            with Session(engine) as session, _denied(GRANT_NOT_FOUND):
                load_grant(session, run_id=run_id, grant_id=grant_id)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_there_is_no_newest_grant_or_ambient_fallback(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with two grants: only the named one counts, whichever is newer or broader."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        narrow = _issue(
            engine, run_id, allowed_actions=["workflow.run"], allowed_resources=["workflow:wf-1"]
        )
        broad = _issue(
            engine,
            run_id,
            allowed_actions=["workflow.run", "release.promote"],
            allowed_resources=RESOURCES,
        )
        assert _check(engine, run_id, broad, "release.promote", "prompt:support")
        with _denied(ACTION_NOT_ALLOWED):
            _check(engine, run_id, narrow, "release.promote", "workflow:wf-1")
        with _denied(RESOURCE_NOT_ALLOWED):
            _check(engine, run_id, narrow, "workflow.run", "prompt:support")


# ---------------------------------------------------------------- explicit sets: exact, no ordering


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("action", "resource", "code"),
    [
        ("workflow.run", "workflow:wf-1", None),
        ("workflow.RUN", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("workflow", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("workflow.run.extra", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("*", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("workflow.run", "workflow:*", RESOURCE_NOT_ALLOWED),
        ("workflow.run", "workflow:wf-10", RESOURCE_NOT_ALLOWED),
        ("workflow.run", "workflow:", RESOURCE_NOT_ALLOWED),
        ("workflow.run", "", RESOURCE_NOT_ALLOWED),
        ("", "workflow:wf-1", ACTION_NOT_ALLOWED),
    ],
)
def test_actions_and_resources_match_exactly_never_by_prefix_wildcard_or_order(
    dialect: str,
    action: str,
    resource: str,
    code: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        if code is None:
            assert _check(engine, run_id, grant_id, action, resource)
        else:
            with _denied(code):
                _check(engine, run_id, grant_id, action, resource)


# ---------------------------------------------------------------- expiry, revocation, membership


@pytest.mark.parametrize("dialect", DIALECTS)
def test_expiry_is_exclusive_at_the_boundary(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        expires = NOW + timedelta(minutes=5)
        grant_id = _issue(engine, run_id, expires=expires)
        assert _check(engine, run_id, grant_id, now=expires - timedelta(microseconds=1))
        with _denied(GRANT_EXPIRED):
            _check(engine, run_id, grant_id, now=expires)
        with _denied(GRANT_EXPIRED):
            _check(engine, run_id, grant_id, now=expires + timedelta(days=30))
        # A naive clock is read as UTC, an aware one in another zone is converted.
        zone = timezone(timedelta(hours=5))
        assert _check(
            engine, run_id, grant_id, now=(expires - timedelta(seconds=1)).astimezone(zone)
        )
        assert _check(
            engine, run_id, grant_id, now=(expires - timedelta(seconds=1)).replace(tzinfo=None)
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_revoked_grant_is_denied_and_revocation_is_recorded_once(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        assert _check(engine, run_id, grant_id)
        with Session(engine) as session, session.begin():
            revoked = revoke_grant(
                session,
                run_id=run_id,
                grant_id=grant_id,
                by="@owner",
                reason="incident 42",
                now=NOW,
            )
        assert revoked.revoked_at == NOW.replace(tzinfo=None)
        with _denied(GRANT_REVOKED):
            _check(engine, run_id, grant_id)
        with _denied(GRANT_REVOKED), Session(engine) as session, session.begin():
            revoke_grant(
                session, run_id=run_id, grant_id=grant_id, by="@other", reason="again", now=NOW
            )
        with Session(engine) as session:
            row = session.get(CaliberRunGrant, grant_id)
            assert (row.revoked_by, row.revoke_reason) == ("@owner", "incident 42")  # type: ignore[union-attr]
        with _denied(GRANT_NOT_FOUND), Session(engine) as session, session.begin():
            revoke_grant(
                session, run_id="other-run", grant_id=grant_id, by="@owner", reason="x", now=NOW
            )
        with pytest.raises(GrantError), Session(engine) as session, session.begin():
            fresh = _issue(engine, run_id)
            revoke_grant(session, run_id=run_id, grant_id=fresh, by=" ", reason="x", now=NOW)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("state", ["absent", "inactive", "deactivated"])
def test_current_membership_is_rechecked_on_every_use(
    dialect: str, state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        assert _check(engine, run_id, grant_id), "a member today"
        with Session(engine) as session, session.begin():
            member = session.query(CaliberProjectMember).filter_by(user_id="alice").one()
            if state == "absent":
                session.delete(member)
            elif state == "inactive":
                member.status = "suspended"
            else:
                member.deactivated_at = NOW.replace(tzinfo=None)
        with _denied(NOT_A_MEMBER):
            _check(engine, run_id, grant_id)
        with (
            Session(engine) as session,
            session.begin(),
        ):  # restored: allowed again (nothing was cached)
            if state == "absent":
                session.add(
                    CaliberProjectMember(
                        member_id="m-again",
                        project_id="proj-1",
                        user_id="alice",
                        role="editor",
                        created_by="@o",
                    )
                )
            else:
                member = session.query(CaliberProjectMember).filter_by(user_id="alice").one()
                member.status, member.deactivated_at = "active", None
        assert _check(engine, run_id, grant_id)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_user_who_was_never_a_member_gets_no_authority(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, member="someone-else")
        grant_id = _issue(engine, run_id)
        with _denied(NOT_A_MEMBER):
            _check(engine, run_id, grant_id)


def test_the_denial_order_is_revoked_then_expired_then_membership_then_action_then_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, member="someone-else")
        grant_id = _issue(engine, run_id)
        late = NOW + timedelta(days=2)
        with _denied(GRANT_EXPIRED):
            _check(engine, run_id, grant_id, "nope", "nope", now=late)
        with _denied(NOT_A_MEMBER):
            _check(engine, run_id, grant_id, "nope", "nope")
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        with _denied(GRANT_REVOKED):
            _check(engine, run_id, grant_id, "nope", "nope", now=late)


def test_every_denial_code_is_documented_and_distinct() -> None:
    assert {
        "grant_not_found",
        "grant_revoked",
        "grant_expired",
        "not_a_member",
        "action_not_allowed",
        "resource_not_allowed",
        "authority_unavailable",
    } == DENIAL_CODES
    assert str(GrantDenied(GRANT_EXPIRED)) == GRANT_EXPIRED


# ---------------------------------------------------------------- unavailable authority denies


def test_a_database_failure_while_checking_is_a_denial_never_an_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("SELECT", {}, Exception("connection lost"))

        with Session(engine) as session:
            monkeypatch.setattr(session, "execute", broken)
            with _denied(AUTHORITY_UNAVAILABLE):
                authorize(
                    session,
                    run_id=run_id,
                    grant_id=grant_id,
                    action="workflow.run",
                    resource="workflow:wf-1",
                )
            with _denied(AUTHORITY_UNAVAILABLE):
                load_grant(session, run_id=run_id, grant_id=grant_id)
            with _denied(AUTHORITY_UNAVAILABLE):
                revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r")


def test_a_membership_lookup_failure_is_a_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("SELECT", {}, Exception("membership table unavailable"))

        monkeypatch.setattr(grants, "_is_active_member", broken)
        with _denied(AUTHORITY_UNAVAILABLE), Session(engine) as session:
            authorize(
                session,
                run_id=run_id,
                grant_id=grant_id,
                action="workflow.run",
                resource="workflow:wf-1",
                now=NOW,
            )


def test_a_stored_principal_that_no_longer_validates_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with (
            engine.begin() as connection
        ):  # tampered around the ORM (the trigger refuses UPDATE, so go via a temp trigger drop)
            connection.execute(text("DROP TRIGGER trg_caliber_run_grants_frozen"))
            connection.execute(text("UPDATE caliber_run_grants SET subject_auth_method = 'root'"))
        with _denied(AUTHORITY_UNAVAILABLE):
            _check(engine, run_id, grant_id)


# ---------------------------------------------------------------- immutable but for revocation


@pytest.mark.parametrize("dialect", DIALECTS)
def test_nothing_but_revocation_can_change_in_the_orm_and_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        for column, value in (
            ("allowed_actions", ["everything"]),
            ("allowed_resources", ["everything"]),
            ("expires_at", datetime(2099, 1, 1)),
            ("subject_user_id", "mallory"),
            ("policy_version", "policy-v2"),
            ("project_id", "proj-1"),  # even a same-value rewrite of a frozen column through Core
        ):
            if column != "project_id":
                with pytest.raises(ImmutableRecordError, match="immutable"):
                    with Session(engine) as session, session.begin():
                        setattr(session.get(CaliberRunGrant, grant_id), column, value)
            with pytest.raises(IntegrityError, match="immutable except for revocation"):
                with engine.begin() as connection:
                    connection.execute(
                        update(CaliberRunGrant.__table__)
                        .where(CaliberRunGrant.grant_id == grant_id)
                        .values({column: value if column != "project_id" else "proj-other"})
                    )
        with Session(engine) as session:
            grant = load_grant(session, run_id=run_id, grant_id=grant_id)
        assert grant.allowed_actions == frozenset(ACTIONS) and grant.policy_version == "policy-v1"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_revoked_grant_stays_revoked_and_unchanged(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        for values in (
            {"revoked_at": None},
            {"revoked_by": "@someone"},
            {"revoke_reason": "edited"},
        ):
            with pytest.raises(IntegrityError, match="stays revoked"):
                with engine.begin() as connection:
                    connection.execute(
                        update(CaliberRunGrant.__table__)
                        .where(CaliberRunGrant.grant_id == grant_id)
                        .values(**values)
                    )
        with pytest.raises(ImmutableRecordError):
            with Session(engine) as session, session.begin():
                session.get(CaliberRunGrant, grant_id).policy_version = "x"  # type: ignore[union-attr]
        with _denied(GRANT_REVOKED):
            _check(engine, run_id, grant_id)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_grant_cannot_be_deleted_by_the_orm_in_bulk_or_by_sql(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with pytest.raises(ImmutableRecordError, match="DELETE"):
            with Session(engine) as session, session.begin():
                session.delete(session.get(CaliberRunGrant, grant_id))
        for statement in (delete(CaliberRunGrant), update(CaliberRunGrant).values(revoked_by="x")):
            with pytest.raises(ImmutableRecordError):
                with Session(engine) as session, session.begin():
                    session.execute(statement)
        with pytest.raises(IntegrityError, match="DELETE refused"):
            with engine.begin() as connection:
                connection.execute(delete(CaliberRunGrant.__table__))
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 1


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TRUNCATE and privileges are PostgreSQL-only")
def test_postgresql_refuses_truncate_and_public_holds_no_delete_or_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        _issue(engine, run_id)
        with pytest.raises(IntegrityError, match="TRUNCATE refused"):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE caliber_run_grants"))
        with engine.connect() as connection:
            granted = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'caliber_run_grants' AND grantee = 'PUBLIC'"
                    )
                )
            }
        assert not granted & {"DELETE", "TRUNCATE"}


# ---------------------------------------------------------------- concurrent revocation (PostgreSQL)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_a_revocation_waits_for_the_transition_it_would_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 authorizes and holds its transaction (the guarded transition). T2's revoke blocks on the row lock until T1
    commits, so the revocation can never land between the check and the transition; afterwards the grant is denied."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        finished = threading.Event()
        order: list[str] = []
        errors: list[BaseException] = []

        def revoker() -> None:
            try:
                with Session(engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '20s'"))
                    revoke_grant(
                        session, run_id=run_id, grant_id=grant_id, by="@o", reason="race", now=NOW
                    )
                    order.append("revoked")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                finished.set()

        t1 = Session(engine)
        t1.begin()
        authorize(
            t1,
            run_id=run_id,
            grant_id=grant_id,
            action="workflow.run",
            resource="workflow:wf-1",
            now=NOW,
        )
        thread = threading.Thread(target=revoker, daemon=True)
        thread.start()
        time.sleep(1.0)
        assert not finished.is_set(), "the revocation must wait for the checking transaction"
        order.append("transition")  # the guarded transition, still authorized
        t1.commit()
        t1.close()
        assert finished.wait(20), "the revocation proceeds once the checker commits"
        thread.join(5)
        assert not errors, errors
        assert order == ["transition", "revoked"]
        with _denied(GRANT_REVOKED):
            _check(engine, run_id, grant_id)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_a_check_that_starts_after_a_pending_revocation_waits_and_then_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        outcome: list[str] = []
        finished = threading.Event()

        def checker() -> None:
            try:
                with Session(engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '20s'"))
                    authorize(
                        session,
                        run_id=run_id,
                        grant_id=grant_id,
                        action="workflow.run",
                        resource="workflow:wf-1",
                        now=NOW,
                    )
                    outcome.append("allowed")
            except GrantDenied as exc:
                outcome.append(exc.code)
            finally:
                finished.set()

        revoker = Session(engine)
        revoker.begin()
        revoke_grant(revoker, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        thread = threading.Thread(target=checker, daemon=True)
        thread.start()
        time.sleep(1.0)
        assert not finished.is_set(), "the check waits for the pending revocation"
        revoker.commit()
        revoker.close()
        assert finished.wait(20)
        thread.join(5)
        assert outcome == [GRANT_REVOKED], (
            "a check never sees the grant as live once a revocation commits"
        )


# ---------------------------------------------------------------- migration


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migration_creates_the_table_and_rolls_back_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, config):
        inspector = inspect(engine)
        assert "caliber_run_grants" in inspector.get_table_names()
        indexes = {index["name"] for index in inspector.get_indexes("caliber_run_grants")}
        assert {"ix_run_grants_run", "ix_run_grants_project"} <= indexes
        run_id = _seed(engine)
        _issue(engine, run_id)

        command.downgrade(config, "0114")
        assert "caliber_run_grants" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM caliber_workflow_runs WHERE workflow_run_id = :r"),
                    {"r": run_id},
                ).scalar_one()
                == 1
            )
            if dialect == "postgresql":
                assert (
                    connection.execute(
                        text("SELECT proname FROM pg_proc WHERE proname LIKE 'caliber_run_grants%'")
                    ).fetchall()
                    == []
                )
        command.upgrade(config, "head")
        assert "caliber_run_grants" in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 0
