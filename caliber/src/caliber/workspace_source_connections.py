"""Encrypted least-privilege GitHub App connection storage (`P4-E`).

`docs/workspace-plan.md`'s `P4-E` row names "encrypted least-privilege GitHub
App connection storage" as still open even though `github_source_control.py`
(the injectable GitHub REST adapter) and `secret_store.py` (the encrypted-at-
rest secret store) were both already delivered. This module is the missing
link: it persists a GitHub App connection's *identifiers* (app id,
installation id, canonical source binding) in an ordinary row, while the
connection's actual secret material -- the App's private key and the
webhook-signing secret -- is written through :mod:`caliber.secret_store` and
referenced only as a ``secret://name`` string on that row. No plaintext
credential column exists anywhere in this module.

A source has at most one *active* connection at a time (enforced by a
partial unique index on ``caliber_workspace_source_connections``). Replacing
a connection creates a new secret-store version for the same logical
reference names; revoking a connection revokes both underlying secrets and
clears ``CaliberWorkspaceSource.connection_ref`` so ``has_connection``
(the API's secret-free projection) goes false immediately.

Credentials are resolved through :func:`resolve_credentials` -- always from
an explicit, already-open session, never cached beyond the caller's own
scope -- so a revoked or rotated secret takes effect on the very next
resolution, matching :mod:`caliber.github_source_control`'s "short-lived
credential provider" design.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkspaceSource, CaliberWorkspaceSourceConnection
from caliber.ids import new_workspace_source_connection_id
from caliber.secret_store import SecretStore, SecretStoreError, reference_name

CONNECTION_ACTIVE = "active"
CONNECTION_REVOKED = "revoked"
MAX_APP_ID_CHARS = 64
MAX_INSTALLATION_ID_CHARS = 64


class WorkspaceSourceConnectionError(RuntimeError):
    """Base error for connection-storage operations."""

    code = "workspace_source_connection_error"


class WorkspaceSourceConnectionNotFoundError(WorkspaceSourceConnectionError):
    """No active connection exists for the requested source."""

    code = "workspace_source_connection_not_found"


class WorkspaceSourceConnectionProviderMismatchError(WorkspaceSourceConnectionError):
    """The connection's provider does not match its source's configured provider."""

    code = "workspace_source_connection_provider_mismatch"


class WorkspaceSourceConnectionInstallationConflictError(WorkspaceSourceConnectionError):
    """Another active connection already claims this (provider, installation_id).

    A real GitHub App installation is only ever legitimately bound to one
    target -- enforced by a DB-level partial unique index, not just this
    check. This is the friendly pre-check; a race against a concurrent
    write is still caught by the same underlying constraint (see
    :func:`configure_connection`'s ``IntegrityError`` handling).
    """

    code = "workspace_source_connection_installation_conflict"


class WorkspaceSourceConnectionUnavailableError(WorkspaceSourceConnectionError):
    """A connection row exists, but its secret material could not be resolved.

    Distinct from "not found": the connection is configured, but the
    encrypted secret store is unbound, the referenced secret was revoked
    out of band, or ciphertext could not be decrypted. Callers should treat
    this exactly like "provider unavailable" -- fail closed, never fall
    back to a cached or partial credential.
    """

    code = "workspace_source_connection_unavailable"


def _utc_naive(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def _bounded(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return result


def private_key_secret_name(connection_id: str) -> str:
    """The `secret_store` name holding one connection's GitHub App private key."""
    return f"workspace-source-connection-{connection_id}-private-key"


def webhook_secret_secret_name(connection_id: str) -> str:
    """The `secret_store` name holding one connection's webhook-signing secret."""
    return f"workspace-source-connection-{connection_id}-webhook-secret"


def get_connection(session: Session, source_id: str) -> CaliberWorkspaceSourceConnection | None:
    """Return the source's active connection, or ``None`` if none is configured."""
    return session.execute(
        select(CaliberWorkspaceSourceConnection).where(
            CaliberWorkspaceSourceConnection.source_id == source_id,
            CaliberWorkspaceSourceConnection.status == CONNECTION_ACTIVE,
        )
    ).scalar_one_or_none()


def get_connection_by_installation(
    session: Session, *, provider: str, installation_id: str
) -> CaliberWorkspaceSourceConnection | None:
    """Return the one active connection claiming ``(provider, installation_id)``.

    Used by webhook ingress to find a *candidate* source from an inbound
    delivery's untrusted ``installation.id`` claim. The DB-level partial
    unique index on ``(provider, installation_id)`` where ``status =
    'active'`` guarantees at most one row -- this is a lookup, not a trust
    decision; the caller must still verify the delivery's HMAC signature
    against this candidate's own webhook secret before treating it as
    authentic.
    """
    return session.execute(
        select(CaliberWorkspaceSourceConnection).where(
            CaliberWorkspaceSourceConnection.provider == provider,
            CaliberWorkspaceSourceConnection.installation_id == installation_id,
            CaliberWorkspaceSourceConnection.status == CONNECTION_ACTIVE,
        )
    ).scalar_one_or_none()


def configure_connection(
    session: Session,
    secret_store: SecretStore,
    *,
    source: CaliberWorkspaceSource,
    app_id: str,
    installation_id: str,
    private_key: str,
    webhook_secret: str,
    actor: str,
    now: datetime | None = None,
) -> CaliberWorkspaceSourceConnection:
    """Create or rotate the one active connection for ``source``.

    Secret material is written exclusively through ``secret_store.put`` --
    which is itself what makes this "rotation" rather than a destructive
    edit, since the previous version is superseded, not overwritten. The
    returned row never carries ``private_key``/``webhook_secret`` themselves;
    callers (routes) must build their own secret-free response from it.
    """
    if source.provider != "github":
        raise WorkspaceSourceConnectionProviderMismatchError(
            f"{WorkspaceSourceConnectionProviderMismatchError.code}: source provider "
            f"{source.provider!r} is not 'github'"
        )
    normalized_app_id = _bounded(app_id, "app_id", MAX_APP_ID_CHARS)
    normalized_installation_id = _bounded(
        installation_id, "installation_id", MAX_INSTALLATION_ID_CHARS
    )
    if not isinstance(private_key, str) or not private_key.strip():
        raise ValueError("private_key must be a non-empty string")
    if not isinstance(webhook_secret, str) or not webhook_secret.strip():
        raise ValueError("webhook_secret must be a non-empty string")

    stamp = _utc_naive(now)
    existing_connection = get_connection(session, source.source_id)

    # Friendly pre-check: a real GitHub App installation is only ever
    # legitimately bound to one target. This is the actionable error path;
    # the DB-level partial unique index (checked below via IntegrityError)
    # is what makes it correct under a race, not merely convention.
    existing_claim = get_connection_by_installation(
        session, provider=source.provider, installation_id=normalized_installation_id
    )
    if existing_claim is not None and (
        existing_connection is None
        or existing_claim.connection_id != existing_connection.connection_id
    ):
        raise WorkspaceSourceConnectionInstallationConflictError(
            f"{WorkspaceSourceConnectionInstallationConflictError.code}: "
            f"installation_id {normalized_installation_id!r} is already bound to "
            "another active connection"
        )

    connection: CaliberWorkspaceSourceConnection
    if existing_connection is None:
        connection = CaliberWorkspaceSourceConnection(
            connection_id=new_workspace_source_connection_id(),
            source_id=source.source_id,
            project_id=source.project_id,
            provider=source.provider,
            app_id=normalized_app_id,
            installation_id=normalized_installation_id,
            # Placeholder until the secrets below are written -- a connection_id
            # is required to name the secret-store entries, and secret names must
            # be stable across rotations of the *same* connection.
            private_key_ref="",
            webhook_secret_ref="",
            status=CONNECTION_ACTIVE,
            created_by=actor,
            created_at=stamp,
            updated_at=stamp,
        )
    else:
        connection = existing_connection
        connection.app_id = normalized_app_id
        connection.installation_id = normalized_installation_id

    try:
        with session.begin_nested():
            if existing_connection is None:
                session.add(connection)
            session.flush()
    except IntegrityError as exc:
        # Race safety net: two concurrent configure_connection calls could
        # both pass the pre-check above before either flushes. The partial
        # unique index is the actual guarantee; this translates its
        # violation into the same typed error the pre-check raises.
        raise WorkspaceSourceConnectionInstallationConflictError(
            f"{WorkspaceSourceConnectionInstallationConflictError.code}: "
            f"installation_id {normalized_installation_id!r} is already bound to "
            "another active connection"
        ) from exc

    private_key_name = private_key_secret_name(connection.connection_id)
    webhook_secret_name = webhook_secret_secret_name(connection.connection_id)
    try:
        secret_store.put(
            session,
            name=private_key_name,
            value=private_key,
            actor=actor,
            description=f"GitHub App private key for workspace source {source.source_id}",
        )
        secret_store.put(
            session,
            name=webhook_secret_name,
            value=webhook_secret,
            actor=actor,
            description=f"GitHub App webhook secret for workspace source {source.source_id}",
        )
    except SecretStoreError as exc:
        raise WorkspaceSourceConnectionError(str(exc)) from exc

    connection.private_key_ref = f"secret://{private_key_name}"
    connection.webhook_secret_ref = f"secret://{webhook_secret_name}"
    connection.status = CONNECTION_ACTIVE
    connection.updated_by = actor
    connection.updated_at = stamp
    source.connection_ref = connection.connection_id
    source.updated_by = actor
    session.flush()
    return connection


def revoke_connection(
    session: Session,
    secret_store: SecretStore,
    *,
    source: CaliberWorkspaceSource,
    actor: str,
    now: datetime | None = None,
) -> CaliberWorkspaceSourceConnection:
    """Revoke the source's active connection and both of its secrets.

    Ciphertext is retained (via ``secret_store.revoke``, not ``purge``) so an
    operator can still audit when and by whom the connection was disabled.
    Idempotent: revoking an already-revoked source raises
    :class:`WorkspaceSourceConnectionNotFoundError` only when there was
    never an active connection to begin with.
    """
    connection = get_connection(session, source.source_id)
    if connection is None:
        raise WorkspaceSourceConnectionNotFoundError(
            f"{WorkspaceSourceConnectionNotFoundError.code}: source "
            f"{source.source_id!r} has no active connection"
        )
    secret_store.revoke(session, name=reference_name(connection.private_key_ref), actor=actor)
    secret_store.revoke(session, name=reference_name(connection.webhook_secret_ref), actor=actor)
    stamp = _utc_naive(now)
    connection.status = CONNECTION_REVOKED
    connection.updated_by = actor
    connection.updated_at = stamp
    if source.connection_ref == connection.connection_id:
        source.connection_ref = None
        source.updated_by = actor
    session.flush()
    return connection


def resolve_credentials(
    session: Session,
    secret_store: SecretStore,
    connection: CaliberWorkspaceSourceConnection,
) -> tuple[str, bytes]:
    """Resolve one connection's plaintext private key (PEM) and webhook secret.

    Always reads through ``secret_store`` from the given session -- never
    cached across calls -- so a revoked or rotated secret is honored
    immediately. Raises :class:`WorkspaceSourceConnectionUnavailableError`
    rather than returning a partial credential when either value cannot be
    resolved (store unbound, secret revoked, ciphertext unreadable).
    """
    private_key = secret_store.resolve(session, reference_name(connection.private_key_ref))
    if not private_key:
        raise WorkspaceSourceConnectionUnavailableError(
            f"{WorkspaceSourceConnectionUnavailableError.code}: private key for "
            f"connection {connection.connection_id!r} is unavailable"
        )
    webhook_secret = secret_store.resolve(session, reference_name(connection.webhook_secret_ref))
    if not webhook_secret:
        raise WorkspaceSourceConnectionUnavailableError(
            f"{WorkspaceSourceConnectionUnavailableError.code}: webhook secret for "
            f"connection {connection.connection_id!r} is unavailable"
        )
    return private_key, webhook_secret.encode("utf-8")


def resolve_webhook_secret(
    session: Session,
    secret_store: SecretStore,
    connection: CaliberWorkspaceSourceConnection,
) -> bytes:
    """Resolve only a connection's webhook-signing secret -- never its private key.

    Webhook signature verification is pure HMAC math; it needs no GitHub API
    call and therefore no installation token, no JWT, and no private key.
    Webhook ingress may need to check several *candidate* connections for
    one inbound delivery (see ``get_connection_by_installation``); paying
    the cost -- and blast radius -- of decrypting every candidate's private
    key just to reject most of them would violate least privilege for no
    benefit. Raises :class:`WorkspaceSourceConnectionUnavailableError`,
    matching :func:`resolve_credentials`'s fail-closed behavior.
    """
    webhook_secret = secret_store.resolve(session, reference_name(connection.webhook_secret_ref))
    if not webhook_secret:
        raise WorkspaceSourceConnectionUnavailableError(
            f"{WorkspaceSourceConnectionUnavailableError.code}: webhook secret for "
            f"connection {connection.connection_id!r} is unavailable"
        )
    return webhook_secret.encode("utf-8")


def resolve_private_key(
    session: Session,
    secret_store: SecretStore,
    connection: CaliberWorkspaceSourceConnection,
) -> str:
    """Resolve only a connection's private key -- never its webhook secret.

    Symmetric to :func:`resolve_webhook_secret`: minting a GitHub App JWT
    (for the App-scoped admin endpoints webhook reconciliation uses, e.g.
    listing/redelivering webhook deliveries) needs the private key but not
    the webhook secret. Raises
    :class:`WorkspaceSourceConnectionUnavailableError`, matching
    :func:`resolve_credentials`'s fail-closed behavior.
    """
    private_key = secret_store.resolve(session, reference_name(connection.private_key_ref))
    if not private_key:
        raise WorkspaceSourceConnectionUnavailableError(
            f"{WorkspaceSourceConnectionUnavailableError.code}: private key for "
            f"connection {connection.connection_id!r} is unavailable"
        )
    return private_key


__all__ = [
    "CONNECTION_ACTIVE",
    "CONNECTION_REVOKED",
    "WorkspaceSourceConnectionError",
    "WorkspaceSourceConnectionInstallationConflictError",
    "WorkspaceSourceConnectionNotFoundError",
    "WorkspaceSourceConnectionProviderMismatchError",
    "WorkspaceSourceConnectionUnavailableError",
    "configure_connection",
    "get_connection",
    "get_connection_by_installation",
    "private_key_secret_name",
    "resolve_credentials",
    "resolve_private_key",
    "resolve_webhook_secret",
    "revoke_connection",
    "webhook_secret_secret_name",
]
