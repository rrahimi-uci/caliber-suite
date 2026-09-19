"""Bridges the injectable GitHub adapter into the Workspace source registry (`P4-E`).

Two protocols exist for a reason, not by accident:

* :class:`caliber.workspace_sources.WorkspaceSourceProvider` is the small,
  provider-neutral surface (``name``, ``capabilities()``, ``verify(source)``)
  the Workspace source lifecycle routes depend on.
* :class:`caliber.github_source_control.GitHubSourceControlProvider` is the
  full GitHub-specific REST adapter (commit/tree comparisons, change-request
  verification, webhook normalization, status publication). It does not
  implement ``WorkspaceSourceProvider`` itself -- it has no ``verify(source)``
  and its ``capabilities()`` returns a typed dataclass, not a ``Mapping``.

This module is the real-credential bridge between the two:
:func:`build_github_source_control_provider` resolves one project's stored
:class:`caliber.db.models.CaliberWorkspaceSourceConnection` and returns a
fully wired ``GitHubSourceControlProvider`` -- reused by both
:class:`GitHubWorkspaceSourceProvider` (registered into
``WorkspaceSourceProviderRegistry`` for the source lifecycle routes) and, in
a later slice, by the webhook ingress route, so the credential-resolution
logic is written exactly once.

No credential is cached at construction: :func:`build_github_source_control_provider`
resolves the connection's private key and webhook secret from the caller's
own already-open session, and everything downstream (a GitHub App JWT, an
installation access token) is minted lazily, per call, by
:class:`caliber.github_app_auth.GitHubAppInstallationTokenProvider`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberWorkspaceSource, CaliberWorkspaceSourceConnection
from caliber.github_app_auth import GitHubAppAuthError, GitHubAppInstallationTokenProvider
from caliber.github_http_transport import HTTPXGitHubTransport
from caliber.github_source_control import (
    GitHubSourceControlError,
    GitHubSourceControlProvider,
    GitHubTransport,
)
from caliber.secret_store import SecretStore
from caliber.workspace_source_connections import (
    WorkspaceSourceConnectionError,
    get_connection,
    resolve_credentials,
)
from caliber.workspace_source_control import SourceControlCapabilities
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderUnavailableError,
    WorkspaceSourceVerification,
    WorkspaceSourceVerificationError,
)

_Factory = sessionmaker[Session]

#: Every capability the adapter can perform when a real connection backs it.
#: Per-source availability (no connection configured, credentials revoked)
#: is enforced by :meth:`GitHubWorkspaceSourceProvider.verify` failing
#: closed, not by narrowing this snapshot.
_FULL_CAPABILITIES = SourceControlCapabilities(
    commit_tree_fetch=True,
    commit_reachability=True,
    change_request_lookup=True,
    exact_head_reviews=True,
    required_checks=True,
    ruleset_observation=True,
    webhook_verification=True,
    status_publication=True,
)


@dataclass(frozen=True)
class GitHubProviderBundle:
    """A live adapter plus the token provider that authenticates it.

    Exposed separately (rather than only the adapter) so a caller that needs
    to *prove* the connection currently authenticates -- as
    :meth:`GitHubWorkspaceSourceProvider.verify` does -- can mint a token
    directly instead of reaching into the adapter's private request
    machinery.
    """

    adapter: GitHubSourceControlProvider
    token_provider: GitHubAppInstallationTokenProvider


def build_github_source_control_provider(
    session: Session,
    secret_store: SecretStore,
    connection: CaliberWorkspaceSourceConnection,
    *,
    host: str,
    transport: GitHubTransport | None = None,
) -> GitHubProviderBundle:
    """Build a real, credential-backed adapter for one stored connection.

    ``private_key``/``webhook_secret`` are resolved once, from the caller's
    already-open ``session`` -- deferred past module import and past the
    registry's own construction (matching the adapter's "construction
    performs no network I/O" contract), but not re-queried on every single
    adapter call within the same resolution, since nothing about a
    connection row's secret material changes mid-request.

    Raises :class:`caliber.workspace_source_connections.WorkspaceSourceConnectionError`
    if the connection's secret material cannot be resolved (store unbound,
    revoked, undecryptable) -- callers already handle that type when calling
    :func:`caliber.workspace_source_connections.resolve_credentials` directly.
    """
    private_key, webhook_secret = resolve_credentials(session, secret_store, connection)
    real_transport = transport or HTTPXGitHubTransport(host=host)
    token_provider = GitHubAppInstallationTokenProvider(
        transport=real_transport,
        app_id_provider=lambda: connection.app_id,
        installation_id_provider=lambda: connection.installation_id,
        private_key_provider=lambda: private_key,
    )
    adapter = GitHubSourceControlProvider(
        real_transport,
        token_provider=token_provider.token,
        webhook_secret_provider=lambda: webhook_secret,
        host=host,
        capabilities=_FULL_CAPABILITIES,
    )
    return GitHubProviderBundle(adapter=adapter, token_provider=token_provider)


class GitHubWorkspaceSourceProvider:
    """``WorkspaceSourceProvider`` implementation backed by a real GitHub App connection.

    Registered under ``"github"`` in ``app.state.workspace_source_registry``
    when ``config.github_source_control_enabled`` is true (see
    ``server.py``). Holds no per-source state itself -- every call opens its
    own session and resolves that source's connection fresh, so enabling,
    disabling, rotating, or revoking a connection takes effect on the very
    next lifecycle call with no restart required.
    """

    name = "github"

    def __init__(
        self,
        *,
        session_factory: _Factory,
        secret_store: SecretStore,
        transport_factory: Callable[[str], GitHubTransport] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._transport_factory = transport_factory
        # One transport (and its underlying connection pool) per provider
        # host, reused across calls rather than rebuilt on every ``verify()``.
        self._transport_cache: dict[str, GitHubTransport] = {}

    def _transport(self, host: str) -> GitHubTransport:
        cached = self._transport_cache.get(host)
        if cached is not None:
            return cached
        built = (
            self._transport_factory(host)
            if self._transport_factory
            else HTTPXGitHubTransport(host=host)
        )
        self._transport_cache[host] = built
        return built

    def capabilities(self) -> Mapping[str, object]:
        return _FULL_CAPABILITIES.as_dict()

    def verify(self, source: CaliberWorkspaceSource) -> WorkspaceSourceVerification:
        with self._session_factory() as session:
            connection = get_connection(session, source.source_id)
            if connection is None:
                raise WorkspaceSourceProviderUnavailableError(
                    "source_provider_unavailable: no active GitHub App connection is "
                    "configured for this source"
                )
            try:
                bundle = build_github_source_control_provider(
                    session,
                    self._secret_store,
                    connection,
                    host=source.provider_host,
                    transport=self._transport(source.provider_host),
                )
            except WorkspaceSourceConnectionError as exc:
                raise WorkspaceSourceProviderError(str(exc)) from exc
            try:
                canonical = bundle.adapter.canonical_repository_id(source.canonical_repository_id)
            except ValueError as exc:
                raise WorkspaceSourceVerificationError(str(exc)) from exc
            # Mint (or reuse a still-fresh cached) installation token as the
            # liveness/authentication proof that this connection actually
            # works -- a stale or revoked GitHub App installation fails here,
            # closed, rather than reporting "verified" on identifiers alone.
            try:
                bundle.token_provider.token()
            except (GitHubAppAuthError, GitHubSourceControlError) as exc:
                raise WorkspaceSourceProviderError(str(exc)) from exc
        return WorkspaceSourceVerification(
            canonical_repository_id=canonical,
            capabilities=self.capabilities(),
        )


__all__ = [
    "GitHubProviderBundle",
    "GitHubWorkspaceSourceProvider",
    "build_github_source_control_provider",
]
