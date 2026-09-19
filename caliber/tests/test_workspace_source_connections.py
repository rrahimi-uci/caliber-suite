"""Tests for encrypted GitHub App connection storage (`P4-E`).

Proves the properties the task cares about most: secret material never
lands in a plaintext column, rotation supersedes rather than duplicates,
revocation actually revokes both secrets and clears the source's
``has_connection`` signal, and credential resolution fails closed rather
than returning a partial value.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject, CaliberWorkspaceSource
from caliber.ids import new_project_id, new_workspace_source_id
from caliber.secret_store import SecretCipher, SecretStore, SecretStoreError, generate_key
from caliber.workspace_source_connections import (
    WorkspaceSourceConnectionError,
    WorkspaceSourceConnectionInstallationConflictError,
    WorkspaceSourceConnectionNotFoundError,
    WorkspaceSourceConnectionProviderMismatchError,
    WorkspaceSourceConnectionUnavailableError,
    configure_connection,
    get_connection,
    get_connection_by_installation,
    private_key_secret_name,
    resolve_credentials,
    resolve_webhook_secret,
    revoke_connection,
    webhook_secret_secret_name,
)

PRIVATE_KEY_VALUE = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n"
WEBHOOK_SECRET_VALUE = "wh-secret-do-not-log"


@pytest.fixture
def store() -> SecretStore:
    return SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))


def _project(session: Session) -> CaliberProject:
    project = CaliberProject(project_id=new_project_id(), name="Connections", status="active")
    session.add(project)
    session.flush()
    return project


def _source(
    session: Session, project: CaliberProject, *, provider: str = "github"
) -> CaliberWorkspaceSource:
    source = CaliberWorkspaceSource(
        source_id=new_workspace_source_id(),
        project_id=project.project_id,
        provider=provider,
        provider_host="github.com",
        canonical_repository_id="github:owner/repo",
        display_path="owner/repo",
        default_branch="main",
        status="disabled",
        created_by="@test",
    )
    session.add(source)
    session.flush()
    return source


def _second_project_and_source(session: Session) -> CaliberWorkspaceSource:
    """A distinct project+source pair (a source has at most one per project)."""
    project = CaliberProject(project_id=new_project_id(), name="Connections 2", status="active")
    session.add(project)
    session.flush()
    source = CaliberWorkspaceSource(
        source_id=new_workspace_source_id(),
        project_id=project.project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id="github:owner/other-repo",
        display_path="owner/other-repo",
        default_branch="main",
        status="disabled",
        created_by="@test",
    )
    session.add(source)
    session.flush()
    return source


def test_configure_connection_stores_only_a_secret_reference(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="12345",
        installation_id="98765",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    assert (
        connection.private_key_ref
        == f"secret://{private_key_secret_name(connection.connection_id)}"
    )
    assert connection.webhook_secret_ref == (
        f"secret://{webhook_secret_secret_name(connection.connection_id)}"
    )
    assert PRIVATE_KEY_VALUE not in connection.private_key_ref
    assert WEBHOOK_SECRET_VALUE not in connection.webhook_secret_ref
    assert connection.status == "active"
    assert source.connection_ref == connection.connection_id


def test_configure_connection_rejects_provider_mismatch(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project, provider="gitlab")

    with pytest.raises(WorkspaceSourceConnectionProviderMismatchError):
        configure_connection(
            db_session,
            store,
            source=source,
            app_id="1",
            installation_id="1",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )


def test_configure_connection_twice_rotates_the_same_connection_id(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    first = configure_connection(
        db_session,
        store,
        source=source,
        app_id="111",
        installation_id="222",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    second = configure_connection(
        db_session,
        store,
        source=source,
        app_id="333",
        installation_id="444",
        private_key="rotated-key",
        webhook_secret="rotated-secret",
        actor="@admin",
    )

    assert second.connection_id == first.connection_id
    assert second.app_id == "333"
    assert second.installation_id == "444"
    private_key, webhook_secret = resolve_credentials(db_session, store, second)
    assert private_key == "rotated-key"
    assert webhook_secret == b"rotated-secret"


def test_revoke_connection_revokes_both_secrets_and_clears_the_source_pointer(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    revoked = revoke_connection(db_session, store, source=source, actor="@admin")

    assert revoked.status == "revoked"
    assert source.connection_ref is None
    assert get_connection(db_session, source.source_id) is None
    assert store.resolve(db_session, private_key_secret_name(connection.connection_id)) is None
    assert store.resolve(db_session, webhook_secret_secret_name(connection.connection_id)) is None


def test_revoke_connection_without_one_configured_raises_not_found(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    with pytest.raises(WorkspaceSourceConnectionNotFoundError):
        revoke_connection(db_session, store, source=source, actor="@admin")


def test_resolve_credentials_fails_closed_when_secret_store_has_no_key_bound(
    db_session: Session, store: SecretStore
) -> None:
    """A connection row can exist while its secret store binding is gone.

    ``resolve_credentials`` must raise rather than return an empty/partial
    credential in that case -- the caller (the real GitHub adapter wrapper)
    treats this exactly like "no connection configured", never as "empty
    token is fine".
    """
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    # Revoking directly through the secret store (bypassing
    # revoke_connection) simulates an out-of-band secret revocation while
    # the connection row itself still claims to be active.
    store.revoke(db_session, name=private_key_secret_name(connection.connection_id), actor="@admin")

    with pytest.raises(WorkspaceSourceConnectionUnavailableError):
        resolve_credentials(db_session, store, connection)


def test_get_connection_ignores_revoked_rows(db_session: Session, store: SecretStore) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    revoke_connection(db_session, store, source=source, actor="@admin")

    assert get_connection(db_session, source.source_id) is None


def test_resolve_credentials_fails_closed_when_only_the_webhook_secret_is_unavailable(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    store.revoke(
        db_session, name=webhook_secret_secret_name(connection.connection_id), actor="@admin"
    )

    with pytest.raises(WorkspaceSourceConnectionUnavailableError):
        resolve_credentials(db_session, store, connection)


def test_configure_connection_accepts_an_explicit_timezone_aware_timestamp(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert connection.created_at.isoformat() == "2026-01-01T00:00:00"


def test_configure_connection_accepts_an_explicit_naive_timestamp_unchanged(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
        now=datetime(2026, 1, 1),
    )

    assert connection.created_at.isoformat() == "2026-01-01T00:00:00"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", ""),
        ("app_id", "x" * 65),
        ("installation_id", ""),
        ("installation_id", "x" * 65),
    ],
)
def test_configure_connection_rejects_out_of_bounds_identifiers(
    db_session: Session, store: SecretStore, field: str, value: str
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    kwargs = {
        "app_id": "1",
        "installation_id": "1",
        "private_key": PRIVATE_KEY_VALUE,
        "webhook_secret": WEBHOOK_SECRET_VALUE,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        configure_connection(db_session, store, source=source, actor="@admin", **kwargs)


def test_configure_connection_rejects_an_empty_private_key(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    with pytest.raises(ValueError, match="private_key"):
        configure_connection(
            db_session,
            store,
            source=source,
            app_id="1",
            installation_id="1",
            private_key="   ",
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )


def test_configure_connection_rejects_an_empty_webhook_secret(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    with pytest.raises(ValueError, match="webhook_secret"):
        configure_connection(
            db_session,
            store,
            source=source,
            app_id="1",
            installation_id="1",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret="",
            actor="@admin",
        )


@dataclass
class _FailingPutStore:
    """Stands in for a real ``SecretStore`` whose ``put`` always fails.

    ``configure_connection`` never calls any other ``SecretStore`` method
    before ``put``, so a minimal stub is enough to exercise its
    ``except SecretStoreError`` branch deterministically, without needing to
    contrive a real encryption failure.
    """

    def put(self, *args: object, **kwargs: object) -> int:
        raise SecretStoreError("secret store is unavailable")


def test_configure_connection_wraps_a_secret_store_failure(db_session: Session) -> None:
    project = _project(db_session)
    source = _source(db_session, project)

    with pytest.raises(WorkspaceSourceConnectionError):
        configure_connection(
            db_session,
            _FailingPutStore(),  # type: ignore[arg-type]
            source=source,
            app_id="1",
            installation_id="1",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )


def test_revoke_connection_leaves_an_unrelated_source_connection_ref_untouched(
    db_session: Session, store: SecretStore
) -> None:
    """Guards the edge case where ``source.connection_ref`` was reassigned
    (e.g. to a newer connection) out from under an older connection row
    before it gets revoked -- revocation must not clobber a pointer it no
    longer owns."""
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="1",
        installation_id="1",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    source.connection_ref = "some-other-connection-id"

    revoked = revoke_connection(db_session, store, source=source, actor="@admin")

    assert revoked.connection_id == connection.connection_id
    assert revoked.status == "revoked"
    assert source.connection_ref == "some-other-connection-id"


# ---------------------------------------------------------------------------
# Installation-id uniqueness (P4-E slice 2: webhook-ingress candidate lookup)
# ---------------------------------------------------------------------------


def test_configure_connection_rejects_a_reused_installation_id(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source_a = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source_a,
        app_id="app-a",
        installation_id="shared-install",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    source_b = _second_project_and_source(db_session)

    with pytest.raises(WorkspaceSourceConnectionInstallationConflictError):
        configure_connection(
            db_session,
            store,
            source=source_b,
            app_id="app-b",
            installation_id="shared-install",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )
    # The rejected attempt must not have left a partial connection row for
    # source_b, and source_a's own connection must be unaffected.
    assert get_connection(db_session, source_b.source_id) is None
    assert get_connection(db_session, source_a.source_id) is not None


def test_configure_connection_rotating_into_a_reused_installation_id_is_rejected(
    db_session: Session, store: SecretStore
) -> None:
    """The conflict check also applies when *rotating* an existing connection."""
    project = _project(db_session)
    source_a = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source_a,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    source_b = _second_project_and_source(db_session)
    configure_connection(
        db_session,
        store,
        source=source_b,
        app_id="app-b",
        installation_id="install-b",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    with pytest.raises(WorkspaceSourceConnectionInstallationConflictError):
        configure_connection(
            db_session,
            store,
            source=source_b,
            app_id="app-b",
            installation_id="install-a",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )
    # source_b's own connection must still be intact with its original
    # installation id -- the rejected rotation must not have partially applied.
    connection_b = get_connection(db_session, source_b.source_id)
    assert connection_b is not None
    assert connection_b.installation_id == "install-b"


def test_configure_connection_re_rotating_the_same_installation_id_onto_itself_succeeds(
    db_session: Session, store: SecretStore
) -> None:
    """Rotating a connection's other fields while keeping the same
    installation_id must not trip the conflict check against itself."""
    project = _project(db_session)
    source = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    rotated = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a-renamed",
        installation_id="install-a",
        private_key="rotated-key",
        webhook_secret="rotated-secret",
        actor="@admin",
    )

    assert rotated.app_id == "app-a-renamed"
    assert rotated.installation_id == "install-a"


def test_configure_connection_catches_a_racing_installation_conflict_at_flush_time(
    db_session: Session, store: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates two concurrent ``configure_connection`` calls both passing
    the pre-check before either flushes: monkeypatch the pre-check to
    report "no conflict" (as it would for a call that ran before the
    racing write landed) while a real conflicting row already exists, so
    the DB-level partial unique index is what actually catches it, exactly
    as it would under a genuine race.
    """
    import caliber.workspace_source_connections as connections_module

    project = _project(db_session)
    source_a = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source_a,
        app_id="app-a",
        installation_id="shared-install",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    source_b = _second_project_and_source(db_session)
    monkeypatch.setattr(connections_module, "get_connection_by_installation", lambda *a, **kw: None)

    with pytest.raises(WorkspaceSourceConnectionInstallationConflictError):
        configure_connection(
            db_session,
            store,
            source=source_b,
            app_id="app-b",
            installation_id="shared-install",
            private_key=PRIVATE_KEY_VALUE,
            webhook_secret=WEBHOOK_SECRET_VALUE,
            actor="@admin",
        )


def test_get_connection_by_installation_finds_only_the_active_claim(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    found = get_connection_by_installation(
        db_session, provider="github", installation_id="install-a"
    )
    assert found is not None
    assert found.connection_id == connection.connection_id

    assert (
        get_connection_by_installation(db_session, provider="github", installation_id="no-such-id")
        is None
    )

    revoke_connection(db_session, store, source=source, actor="@admin")
    assert (
        get_connection_by_installation(db_session, provider="github", installation_id="install-a")
        is None
    )


# ---------------------------------------------------------------------------
# Minimal-privilege webhook-secret resolution
# ---------------------------------------------------------------------------


def test_resolve_webhook_secret_returns_only_the_webhook_secret(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )

    resolved = resolve_webhook_secret(db_session, store, connection)

    assert resolved == WEBHOOK_SECRET_VALUE.encode("utf-8")


def test_resolve_webhook_secret_fails_closed_when_revoked(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    store.revoke(
        db_session, name=webhook_secret_secret_name(connection.connection_id), actor="@admin"
    )

    with pytest.raises(WorkspaceSourceConnectionUnavailableError):
        resolve_webhook_secret(db_session, store, connection)


def test_resolve_webhook_secret_never_touches_the_private_key(
    db_session: Session, store: SecretStore
) -> None:
    """Proves the "minimal privilege" claim, not just the happy path.

    Revoke the *private key* out of band (simulating it being unresolvable)
    and confirm ``resolve_webhook_secret`` still succeeds -- it must never
    read ``private_key_ref`` at all.
    """
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-a",
        installation_id="install-a",
        private_key=PRIVATE_KEY_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    store.revoke(db_session, name=private_key_secret_name(connection.connection_id), actor="@admin")

    resolved = resolve_webhook_secret(db_session, store, connection)

    assert resolved == WEBHOOK_SECRET_VALUE.encode("utf-8")
