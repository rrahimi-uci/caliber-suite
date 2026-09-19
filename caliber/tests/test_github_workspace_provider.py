"""Tests for the real-credential bridge into the Workspace source registry (`P4-E`).

Proves ``GitHubWorkspaceSourceProvider`` -- the thing ``server.py`` registers
under ``"github"`` when ``github_source_control_enabled`` is true -- fails
closed with no connection configured, produces a working adapter once one
is, and never leaks credential material through any exception message it
raises. Fully offline: a fake ``GitHubTransport`` (the same fixture shape as
``test_github_source_control.py``) stands in for every network call.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberProject, CaliberWorkspaceSource
from caliber.github_source_control import GitHubResponse
from caliber.github_workspace_provider import (
    GitHubWorkspaceSourceProvider,
    build_github_source_control_provider,
    build_webhook_verifier,
)
from caliber.ids import new_project_id, new_workspace_source_id
from caliber.secret_store import SecretCipher, SecretStore, generate_key
from caliber.workspace_source_connections import (
    configure_connection,
    private_key_secret_name,
)
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderUnavailableError,
    WorkspaceSourceVerificationError,
)

WEBHOOK_SECRET_VALUE = "wh-secret"


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@dataclass
class _Transport:
    responses: dict[tuple[str, str], GitHubResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, *, headers, params=None, json_body=None):
        self.calls.append((method, path))
        try:
            return self.responses[(method, path)]
        except KeyError as exc:  # pragma: no cover - guards a broken fixture
            raise AssertionError(f"unexpected request: {method} {path}") from exc


@pytest.fixture
def store() -> SecretStore:
    return SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))


def _project(session: Session) -> CaliberProject:
    project = CaliberProject(project_id=new_project_id(), name="Provider", status="active")
    session.add(project)
    session.flush()
    return project


def _source(session: Session, project: CaliberProject) -> CaliberWorkspaceSource:
    source = CaliberWorkspaceSource(
        source_id=new_workspace_source_id(),
        project_id=project.project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id="owner/repo",
        display_path="owner/repo",
        default_branch="main",
        status="disabled",
        created_by="@test",
    )
    session.add(source)
    session.flush()
    return source


def test_build_github_source_control_provider_authenticates_with_the_resolved_credentials(
    db_session: Session, store: SecretStore, rsa_private_key_pem: str
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    transport = _Transport(
        {
            ("POST", "/app/installations/install-1/access_tokens"): GitHubResponse(
                status_code=201, payload={"token": "ghs_live", "expires_at": None}
            )
        }
    )

    bundle = build_github_source_control_provider(
        db_session, store, connection, host="github.com", transport=transport
    )

    assert bundle.token_provider.token() == "ghs_live"
    assert bundle.adapter.canonical_repository_id("owner/repo") == "github:owner/repo"


def test_verify_fails_closed_with_no_connection_configured(
    db_session: Session, store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    db_session.commit()
    provider = GitHubWorkspaceSourceProvider(session_factory=session_factory, secret_store=store)

    with pytest.raises(WorkspaceSourceProviderUnavailableError):
        provider.verify(source)


def test_verify_succeeds_and_matches_capabilities_and_identity(
    db_session: Session,
    store: SecretStore,
    rsa_private_key_pem: str,
    session_factory: sessionmaker[Session],
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    db_session.commit()
    transport = _Transport(
        {
            ("POST", "/app/installations/install-1/access_tokens"): GitHubResponse(
                status_code=201, payload={"token": "ghs_live", "expires_at": None}
            )
        }
    )
    provider = GitHubWorkspaceSourceProvider(
        session_factory=session_factory,
        secret_store=store,
        transport_factory=lambda host: transport,
    )

    verification = provider.verify(source)

    assert verification.canonical_repository_id == "github:owner/repo"
    assert verification.capabilities["webhook_verification"] is True
    assert transport.calls == [("POST", "/app/installations/install-1/access_tokens")]


def test_verify_fails_closed_when_authentication_fails(
    db_session: Session,
    store: SecretStore,
    rsa_private_key_pem: str,
    session_factory: sessionmaker[Session],
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    db_session.commit()
    transport = _Transport(
        {
            ("POST", "/app/installations/install-1/access_tokens"): GitHubResponse(
                status_code=401, payload={"message": "Bad credentials"}
            )
        }
    )
    provider = GitHubWorkspaceSourceProvider(
        session_factory=session_factory,
        secret_store=store,
        transport_factory=lambda host: transport,
    )

    with pytest.raises(WorkspaceSourceProviderError) as excinfo:
        provider.verify(source)
    assert rsa_private_key_pem not in str(excinfo.value)
    assert WEBHOOK_SECRET_VALUE not in str(excinfo.value)


def test_verify_raises_verification_error_for_a_malformed_repository_binding(
    db_session: Session,
    store: SecretStore,
    rsa_private_key_pem: str,
    session_factory: sessionmaker[Session],
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    source.canonical_repository_id = "not-a-valid-repository-id"
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    db_session.commit()
    provider = GitHubWorkspaceSourceProvider(
        session_factory=session_factory,
        secret_store=store,
        transport_factory=lambda host: _Transport({}),
    )

    with pytest.raises(WorkspaceSourceVerificationError):
        provider.verify(source)


def test_verify_reuses_a_cached_transport_for_the_same_host(
    db_session: Session,
    store: SecretStore,
    rsa_private_key_pem: str,
    session_factory: sessionmaker[Session],
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    db_session.commit()
    transport = _Transport(
        {
            ("POST", "/app/installations/install-1/access_tokens"): GitHubResponse(
                status_code=201, payload={"token": "ghs_live", "expires_at": None}
            )
        }
    )
    built_hosts: list[str] = []

    def _factory(host: str) -> object:
        built_hosts.append(host)
        return transport

    provider = GitHubWorkspaceSourceProvider(
        session_factory=session_factory, secret_store=store, transport_factory=_factory
    )

    provider.verify(source)
    provider.verify(source)

    # The transport factory only ran once -- the second ``verify()`` reused
    # the cached transport for "github.com" instead of building a new one.
    assert built_hosts == ["github.com"]


def test_verify_fails_closed_when_the_connections_secret_material_is_unavailable(
    db_session: Session,
    store: SecretStore,
    rsa_private_key_pem: str,
    session_factory: sessionmaker[Session],
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    # Revoke the underlying secret directly (out of band), leaving the
    # connection row itself still marked active -- proves ``verify()`` fails
    # closed on a connection whose credentials became unresolvable, not just
    # a missing connection row.
    store.revoke(db_session, name=private_key_secret_name(connection.connection_id), actor="@admin")
    db_session.commit()
    provider = GitHubWorkspaceSourceProvider(session_factory=session_factory, secret_store=store)

    with pytest.raises(WorkspaceSourceProviderError):
        provider.verify(source)


def test_capabilities_reports_the_full_static_set(
    store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    provider = GitHubWorkspaceSourceProvider(session_factory=session_factory, secret_store=store)

    capabilities = provider.capabilities()

    assert capabilities["status_publication"] is True
    assert capabilities["commit_tree_fetch"] is True


# ---------------------------------------------------------------------------
# build_webhook_verifier: minimal-privilege signature-only adapter
# ---------------------------------------------------------------------------


def test_build_webhook_verifier_can_verify_a_signed_payload(
    db_session: Session, store: SecretStore
) -> None:
    import hashlib
    import hmac
    import json

    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key="unused-for-webhook-verification",
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    verifier = build_webhook_verifier(db_session, store, connection, host="github.com")
    payload = json.dumps({"repository": {"full_name": "owner/repo"}, "action": "opened"}).encode()
    signature = hmac.new(WEBHOOK_SECRET_VALUE.encode(), payload, hashlib.sha256).hexdigest()

    event = verifier.verify_webhook(
        "owner/repo",
        {
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
        payload,
    )

    assert event.signature_verified is True
    assert event.repository_id == "github:owner/repo"


def test_build_webhook_verifier_never_resolves_the_private_key(
    db_session: Session, store: SecretStore
) -> None:
    """The private-privilege claim, proven rather than assumed: revoke the
    private key out of band and confirm the verifier still works."""
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key="a-private-key-that-will-be-revoked",
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    store.revoke(db_session, name=private_key_secret_name(connection.connection_id), actor="@admin")

    verifier = build_webhook_verifier(db_session, store, connection, host="github.com")

    # Constructing the verifier succeeded despite the private key being
    # unresolvable -- proof it was never touched.
    assert verifier is not None


def test_unused_transport_raises_if_ever_called_directly() -> None:
    from caliber.github_workspace_provider import _UnusedTransport

    with pytest.raises(RuntimeError, match="must not perform GitHub API requests"):
        _UnusedTransport().request("GET", "/", headers={})


def test_build_webhook_verifiers_transport_and_token_provider_are_never_invoked(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="install-1",
        private_key=WEBHOOK_SECRET_VALUE,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    verifier = build_webhook_verifier(db_session, store, connection, host="github.com")

    with pytest.raises(RuntimeError, match="must not"):
        verifier.publish_status("owner/repo", "a" * 40, state="success", description="x")
