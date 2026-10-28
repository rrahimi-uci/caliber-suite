"""Run-bound grants: exact ids, explicit sets, rechecked authority, revocation that cannot be raced (B03.2, #664).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity (PostgreSQL)" job),
on PostgreSQL, against a database migrated by Alembic so the migration's triggers are under test, not just the ORM.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator
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
    ROLE_NOT_ALLOWED,
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
ACTIONS = ["resource.execute", "release.apply"]
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


def _seed(
    engine: Engine, *, member: str | None = "alice", status: str = "active", role: str = "editor"
) -> str:
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
                    role=role,
                    status=status,
                    created_by="@owner",
                )
            )
        session.add(
            CaliberWorkflowRun(workflow_run_id=run_id, workflow_id="wf-1", manifest_legacy=False)
        )
    return run_id


def _new_grant_id() -> str:
    """A fresh test grant id. The letter keeps a run of ten hex digits from reading as a phone number: the audit log's
    default redaction would turn ``grant-4543565950`` into ``grant[REDACTED]`` and fail a comparison on that row."""
    return f"grant-g{uuid.uuid4().hex[:9]}"


def _issue(
    engine: Engine,
    run_id: str,
    grant_id: str | None = None,
    *,
    expires: datetime | None = None,
    **overrides: Any,
) -> str:
    grant_id = grant_id or _new_grant_id()
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
    action: str = "resource.execute",
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
        grant_id = _issue(
            engine,
            run_id,
            allowed_actions=["resource.execute", "release.apply", "resource.execute"],
        )
        with Session(engine) as session:
            grant = load_grant(session, run_id=run_id, grant_id=grant_id)
        assert grant.grant_id == grant_id and grant.run_id == run_id
        assert grant.subject == _subject() and grant.workload == _workload()
        assert grant.project_id == "proj-1" and grant.policy_version == "policy-v1"
        assert grant.allowed_actions == frozenset({"resource.execute", "release.apply"})
        assert grant.allowed_resources == frozenset(RESOURCES)
        assert grant.expires_at == (NOW + timedelta(hours=1)).replace(tzinfo=None)
        assert grant.revoked_at is None
        assert grant.allows("release.apply", "workflow:wf-1") and not grant.allows(
            "resource.read", "workflow:wf-1"
        )


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
            engine,
            run_id,
            allowed_actions=["resource.execute"],
            allowed_resources=["workflow:wf-1"],
        )
        broad = _issue(
            engine,
            run_id,
            allowed_actions=["resource.execute", "release.apply"],
            allowed_resources=RESOURCES,
        )
        assert _check(engine, run_id, broad, "release.apply", "prompt:support")
        with _denied(ACTION_NOT_ALLOWED):
            _check(engine, run_id, narrow, "release.apply", "workflow:wf-1")
        with _denied(RESOURCE_NOT_ALLOWED):
            _check(engine, run_id, narrow, "resource.execute", "prompt:support")


# ---------------------------------------------------------------- explicit sets: exact, no ordering


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("action", "resource", "code"),
    [
        ("resource.execute", "workflow:wf-1", None),
        ("resource.EXECUTE", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("resource", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("workflow.run.extra", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("*", "workflow:wf-1", ACTION_NOT_ALLOWED),
        ("resource.execute", "workflow:*", RESOURCE_NOT_ALLOWED),
        ("resource.execute", "workflow:wf-10", RESOURCE_NOT_ALLOWED),
        ("resource.execute", "workflow:", RESOURCE_NOT_ALLOWED),
        ("resource.execute", "", RESOURCE_NOT_ALLOWED),
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
        # An aware clock in another zone is converted; a naive one is refused (its zone is unknowable).
        zone = timezone(timedelta(hours=5))
        assert _check(
            engine, run_id, grant_id, now=(expires - timedelta(seconds=1)).astimezone(zone)
        )
        with pytest.raises(GrantError, match="timezone-aware"):
            _check(
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
def test_a_grant_cannot_be_issued_to_someone_who_is_not_a_current_member(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, member="someone-else")
        with pytest.raises(GrantError, match="not a current member"):
            _issue(engine, run_id)
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 0


def _remove_member(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.delete(session.query(CaliberProjectMember).filter_by(user_id="alice").one())


def test_the_denial_order_is_revoked_then_expired_then_membership_then_action_then_role_then_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        _remove_member(engine)
        late = NOW + timedelta(days=2)
        with _denied(GRANT_EXPIRED):
            _check(engine, run_id, grant_id, "nope", "nope", now=late)
        with _denied(NOT_A_MEMBER):
            _check(engine, run_id, grant_id, "nope", "nope")
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        with _denied(GRANT_REVOKED):
            _check(engine, run_id, grant_id, "nope", "nope", now=late)


def test_action_then_role_then_resource_are_checked_in_that_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, allowed_actions=["resource.execute", "release.apply"])
        with _denied(ACTION_NOT_ALLOWED):
            _check(engine, run_id, grant_id, "release.rollback", "nope")
        with Session(engine) as session, session.begin():  # demoted: editor -> viewer
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = "viewer"
        with _denied(ROLE_NOT_ALLOWED):
            _check(engine, run_id, grant_id, "release.apply", "nope")


def test_every_denial_code_is_documented_and_distinct() -> None:
    assert {
        "grant_not_found",
        "grant_revoked",
        "grant_expired",
        "not_a_member",
        "action_not_allowed",
        "role_not_allowed",
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
                    action="resource.execute",
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

        monkeypatch.setattr(grants, "_current_role", broken)
        with _denied(AUTHORITY_UNAVAILABLE), Session(engine) as session:
            authorize(
                session,
                run_id=run_id,
                grant_id=grant_id,
                action="resource.execute",
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


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TRUNCATE triggers are PostgreSQL-only")
def test_postgresql_refuses_truncate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        _issue(engine, _seed(engine))
        with pytest.raises(IntegrityError, match="TRUNCATE refused"):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE caliber_run_grants"))
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 1


# ---------------------------------------------------------------- concurrency (PostgreSQL)


@contextmanager
def _held(engine: Engine) -> Iterator[Session]:
    """A transaction held open by the test, always rolled back so a failed assertion cannot leave a lock behind."""
    session = Session(engine)
    session.begin()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _wait_blocked(engine: Engine, timeout: float = 15.0) -> None:
    """Return once some backend is waiting on a lock (polled, not slept): proof the second transaction blocked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("no transaction ever blocked on a lock")


def _in_thread(
    engine: Engine, work: Callable[[Session], Any]
) -> tuple[threading.Thread, dict[str, Any]]:
    """Run ``work`` in its own transaction on a thread; its outcome lands in the returned dict."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '30s'"))
                outcome["result"] = work(session)
            outcome["committed_at"] = time.monotonic()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _authorize_now(session: Session, run_id: str, grant_id: str, **kw: Any) -> Any:
    return authorize(
        session,
        run_id=run_id,
        grant_id=grant_id,
        action="resource.execute",
        resource="workflow:wf-1",
        now=NOW,
        **kw,
    )


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_a_revocation_waits_for_the_transition_it_would_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 authorizes and holds its transaction (the guarded transition). T2's revoke blocks on the row lock until T1
    commits, so the revocation can never land between the check and the transition; afterwards the grant is denied."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        order: list[str] = []
        with _held(engine) as t1:
            _authorize_now(t1, run_id, grant_id)
            thread, outcome = _in_thread(
                engine,
                lambda s: (
                    revoke_grant(
                        s, run_id=run_id, grant_id=grant_id, by="@o", reason="race", now=NOW
                    ),
                    order.append("revoked"),
                ),
            )
            _wait_blocked(engine)
            assert thread.is_alive(), "the revocation must wait for the checking transaction"
            order.append("transition")  # the guarded transition, still authorized
            t1.commit()
        thread.join(30)
        assert "error" not in outcome, outcome.get("error")
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
        with _held(engine) as revoker:
            revoke_grant(revoker, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
            thread, outcome = _in_thread(engine, lambda s: _authorize_now(s, run_id, grant_id))
            _wait_blocked(engine)
            assert thread.is_alive(), "the check waits for the pending revocation"
            revoker.commit()
        thread.join(30)
        error = outcome.get("error")
        assert isinstance(error, GrantDenied) and error.code == GRANT_REVOKED, outcome


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
@pytest.mark.parametrize("change", ["remove", "suspend", "demote"])
def test_postgresql_removing_or_demoting_a_member_waits_for_a_check_in_flight(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check share-locks the membership it relied on, so the change cannot land between the check and the
    transition it guards; once it commits, the next check denies."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)

        def change_member(session: Session) -> None:
            member = session.query(CaliberProjectMember).filter_by(user_id="alice").one()
            if change == "remove":
                session.delete(member)
            elif change == "suspend":
                member.status = "suspended"
            else:
                member.role = "viewer"

        with _held(engine) as t1:
            _authorize_now(t1, run_id, grant_id)
            thread, outcome = _in_thread(engine, change_member)
            _wait_blocked(engine)
            assert thread.is_alive(), "the membership change must wait for the check's transaction"
            transitioned_at = time.monotonic()  # the guarded transition, still authorized
            t1.commit()
        thread.join(30)
        assert "error" not in outcome, outcome.get("error")
        assert outcome["committed_at"] > transitioned_at
        code = ROLE_NOT_ALLOWED if change == "demote" else NOT_A_MEMBER
        with _denied(code):
            _check(engine, run_id, grant_id)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="isolation levels are PostgreSQL-only")
@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_postgresql_a_change_committed_after_the_snapshot_is_a_denial_not_an_allow(
    level: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine.execution_options(isolation_level=level)) as checker:
            checker.begin()
            checker.execute(text("SELECT 1"))  # the snapshot starts here
            with Session(engine) as other, other.begin():
                other.query(CaliberProjectMember).filter_by(user_id="alice").delete()
            with pytest.raises(GrantDenied) as raised:
                _authorize_now(checker, run_id, grant_id)
            assert raised.value.code in {AUTHORITY_UNAVAILABLE, NOT_A_MEMBER}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_authorize_sees_a_revocation_committed_after_the_grant_was_loaded_into_the_session(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: without ``populate_existing`` the identity map answered with the stale, unrevoked grant."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as stale:
            stale.begin()
            assert stale.get(CaliberRunGrant, grant_id).revoked_at is None  # type: ignore[union-attr]
            with Session(engine) as other, other.begin():
                revoke_grant(other, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
            with _denied(GRANT_REVOKED):
                _authorize_now(stale, run_id, grant_id)


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


# ---------------------------------------------------------------- closed vocabulary, roles, hygiene


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_action_outside_the_closed_vocabulary_cannot_be_granted(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        for action in (
            "workflow.run",
            "release.break_glass_apply",
            "*",
            "resource.execute ",
            "RESOURCE.EXECUTE",
        ):
            with pytest.raises(GrantError, match="closed vocabulary|surrounding whitespace"):
                _issue(engine, run_id, allowed_actions=["resource.execute", action])
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_run_must_exist_and_belong_to_the_grants_project(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        with pytest.raises(GrantError, match="run does not exist"):
            _issue(engine, "run-nowhere")
        other = _seed(engine)
        with Session(engine) as session, session.begin():
            session.get(CaliberWorkflowRun, other).project_id = "proj-other"  # type: ignore[union-attr]
        with pytest.raises(GrantError, match="different project"):
            _issue(engine, other)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_id", "g" * 65),
        ("policy_version", "p" * 65),
        ("grant_id", "bad\nid"),
        ("policy_version", "zero\u200bwidth"),
        ("allowed_resources", ["x" * 257]),
        ("allowed_resources", ["fine", "bad\x00nul"]),
        ("allowed_resources", [f"r{n}" for n in range(257)]),
    ],
)
def test_sizes_and_characters_are_bounded_before_anything_is_stored(
    field: str, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        with pytest.raises(GrantError):
            _issue(engine, run_id, **{field: value})


def test_naive_datetimes_are_refused_because_their_zone_is_unknowable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        with pytest.raises(GrantError, match="timezone-aware"):
            _issue(engine, run_id, expires=datetime(2030, 1, 1))
        with pytest.raises(GrantError, match="timezone-aware"):
            _issue(engine, run_id, now=datetime(2026, 1, 1))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_revoking_with_bad_input_changes_nothing_even_if_the_caller_commits(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the row used to be marked revoked before the reason was validated."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        for by, reason in (("@o", " "), (" ", "r"), ("", "r"), ("@o", "x" * 1001)):
            with Session(engine) as session, session.begin():
                with pytest.raises(GrantError):
                    revoke_grant(
                        session, run_id=run_id, grant_id=grant_id, by=by, reason=reason, now=NOW
                    )
            with Session(engine) as session:
                row = session.get(CaliberRunGrant, grant_id)
                assert (row.revoked_at, row.revoked_by, row.revoke_reason) == (None, None, None)  # type: ignore[union-attr]
        assert _check(engine, run_id, grant_id)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "values",
    [
        {"revoked_by": "@o"},
        {"revoke_reason": "why"},
        {"revoked_at": datetime(2026, 1, 1)},
        {"revoked_at": datetime(2026, 1, 1), "revoked_by": "@o"},
    ],
)
def test_a_revocation_is_written_whole_or_not_at_all(
    dialect: str, values: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(CaliberRunGrant.__table__)
                    .where(CaliberRunGrant.grant_id == grant_id)
                    .values(**values)
                )
        with pytest.raises(
            IntegrityError
        ):  # and through the ORM, where the whole-revocation CHECK is the backstop
            with Session(engine) as session, session.begin():
                row = session.get(CaliberRunGrant, grant_id)
                for key, value in values.items():
                    setattr(row, key, value)
        assert _check(engine, run_id, grant_id)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_frozen_column_is_refused_by_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changes: dict[str, Any] = {
        "grant_id": "g-other",
        "workflow_run_id": "run-other",
        "subject_user_id": "mallory",
        "subject_auth_method": "pat",
        "workload_id": "wl_" + "0" * 64,
        "workload_name": "other",
        "workflow_version_digest": "1" * 64,
        "project_id": "proj-other",
        "allowed_actions": ["release.apply"],
        "allowed_resources": ["x"],
        "policy_version": "policy-v2",
        "expires_at": datetime(2099, 1, 1),
        "created_at": datetime(2001, 1, 1),
    }
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        assert set(changes) == set(CaliberRunGrant.__table__.columns.keys()) - {
            "revoked_at",
            "revoked_by",
            "revoke_reason",
        }, "a new column must be classified as frozen or revocable"
        for column, value in changes.items():
            with pytest.raises(
                IntegrityError, match="immutable except for revocation|foreign key|check"
            ):
                with engine.begin() as connection:
                    connection.execute(
                        update(CaliberRunGrant.__table__)
                        .where(CaliberRunGrant.grant_id == grant_id)
                        .values({column: value})
                    )
        with Session(engine) as session:
            assert session.query(CaliberRunGrant).count() == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_orm_refuses_to_edit_a_revocation_after_it_was_committed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        with Session(engine) as session, session.begin():
            row = session.get(CaliberRunGrant, grant_id)
            with pytest.raises(ImmutableRecordError):
                row.revoke_reason = "rewritten"  # type: ignore[union-attr]
                session.flush()
            session.rollback()


# ---------------------------------------------------------------- the owner role needs live owner scopes


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_project_owner_counts_as_owner_only_with_live_owner_scopes(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.resource_access import OWNER_ROLE_REQUIRED_SCOPES

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, member=None)
        with Session(engine) as session, session.begin():
            session.get(CaliberProject, "proj-1").owner = "alice"  # type: ignore[union-attr]
        owner_only = ["release.rollback"]  # {owner} alone in resource_access.PROJECT_ACTIONS
        with pytest.raises(GrantError, match="never exceeds the role"):
            _issue(
                engine, run_id, allowed_actions=owner_only
            )  # no scopes: the owner narrows to editor
        with pytest.raises(GrantError, match="never exceeds the role"):
            _issue(
                engine, run_id, allowed_actions=owner_only, scopes=frozenset({"caliber.operator"})
            )
        grant_id = _issue(
            engine, run_id, allowed_actions=owner_only, scopes=OWNER_ROLE_REQUIRED_SCOPES
        )
        assert _check(
            engine, run_id, grant_id, "release.rollback", scopes=OWNER_ROLE_REQUIRED_SCOPES
        )
        with _denied(ROLE_NOT_ALLOWED):
            _check(engine, run_id, grant_id, "release.rollback")  # the owner's scopes lapsed
        with _denied(ROLE_NOT_ALLOWED):
            _check(
                engine, run_id, grant_id, "release.rollback", scopes=frozenset({"caliber.operator"})
            )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_assigning_a_frozen_column_its_own_value_is_not_an_update(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session, session.begin():
            row = session.get(CaliberRunGrant, grant_id)
            row.policy_version = row.policy_version  # type: ignore[union-attr]
        assert _check(engine, run_id, grant_id)


def test_a_generated_grant_id_is_never_redacted_as_a_phone_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.redaction import DEFAULT_PATTERNS, Redactor

    class AllDigits:
        hex = "4543565950abcdef"

    monkeypatch.setattr(uuid, "uuid4", lambda: AllDigits())
    grant_id = _new_grant_id()
    assert Redactor.from_patterns(DEFAULT_PATTERNS).redact_value({"grant_id": grant_id}) == {
        "grant_id": grant_id
    }
