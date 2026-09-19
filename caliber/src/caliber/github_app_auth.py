"""GitHub App installation-token exchange (`P4-E`).

`github_source_control.GitHubSourceControlProvider` takes a bare
``token_provider: Callable[[], str]`` closure -- it deliberately has no
opinion on *how* that token is produced, only that producing it performs no
network I/O at adapter-construction time. A real GitHub App deployment
authenticates in two steps: mint a short-lived JWT signed with the App's
private key (``iss`` = app id), then exchange that JWT for an even
shorter-lived *installation* access token via
``POST /app/installations/{id}/access_tokens``. This module is that
exchange, wrapped as a zero-argument callable so it plugs directly into the
adapter's ``token_provider`` slot.

Nothing here persists or logs the private key or the minted token. The
private key is accepted as a callable (resolved by the caller through
:mod:`caliber.secret_store`, never read from this module) and the minted
installation token is cached only in memory, only for the remaining
lifetime GitHub itself reports, and never written to a log line.
"""

from __future__ import annotations

import calendar
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import jwt

from caliber.github_source_control import GitHubTransport

#: GitHub rejects an app JWT with more than 10 minutes of validity. Staying
#: comfortably under that ceiling leaves room for clock drift between this
#: process and GitHub's.
JWT_TTL_SECONDS = 540
#: Backdate ``iat`` slightly so a few seconds of clock skew toward the past
#: on GitHub's side never makes a freshly minted JWT look "not yet valid".
JWT_CLOCK_SKEW_SECONDS = 60
JWT_ALGORITHM = "RS256"
#: Refresh an installation token this many seconds before GitHub's own
#: reported expiry, so a request in flight never races a token that expires
#: mid-call.
TOKEN_REFRESH_MARGIN_SECONDS = 60
#: GitHub does not always return ``expires_at``; fall back to its documented
#: default installation-token lifetime (60 minutes) minus the same margin.
DEFAULT_TOKEN_TTL_SECONDS = 3300


class GitHubAppAuthError(RuntimeError):
    """The installation-token exchange could not produce a usable token."""

    code = "github_app_auth_error"


class PrivateKeyProvider(Protocol):
    """Zero-argument callable returning the App's PEM-encoded private key."""

    def __call__(self) -> str: ...  # pragma: no cover - structural typing stub, never invoked


def _require(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubAppAuthError(f"{GitHubAppAuthError.code}: {field_name} is unavailable")
    return value.strip()


def _parse_expires_at(value: object, *, now: float) -> float:
    """Best-effort parse of GitHub's ``expires_at`` (RFC 3339, ``Z`` suffix).

    Falls back to a conservative default rather than raising: a token that
    is real but whose expiry we could not parse should still be usable, just
    refreshed sooner than strictly necessary.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
        else:
            return float(calendar.timegm(parsed.timetuple()))
    return now + DEFAULT_TOKEN_TTL_SECONDS


@dataclass
class GitHubAppInstallationTokenProvider:
    """Mints and caches a GitHub App installation access token on demand.

    ``.token`` (bound method) is what satisfies the adapter's
    ``TokenProvider = Callable[[], str]`` shape -- pass it directly as
    ``GitHubSourceControlProvider(..., token_provider=this.token)``.

    All three credential-shaped constructor arguments are callables, not
    values, so this object can be constructed once and still pick up a
    rotated private key or installation id on the next call without being
    rebuilt -- matching the wrapped adapter's own "short-lived credential
    provider" injection pattern.
    """

    transport: GitHubTransport
    app_id_provider: Callable[[], str]
    installation_id_provider: Callable[[], str]
    private_key_provider: PrivateKeyProvider
    clock: Callable[[], float] = field(default=time.time)
    _cached_token: str | None = field(default=None, init=False, repr=False)
    _cached_expires_at: float = field(default=0.0, init=False, repr=False)

    def _mint_jwt(self) -> str:
        app_id = _require(self.app_id_provider(), "GitHub App id")
        private_key = _require(self.private_key_provider(), "GitHub App private key")
        now = int(self.clock())
        payload = {
            "iat": now - JWT_CLOCK_SKEW_SECONDS,
            "exp": now + JWT_TTL_SECONDS,
            "iss": app_id,
        }
        try:
            encoded = jwt.encode(payload, private_key, algorithm=JWT_ALGORITHM)
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise GitHubAppAuthError(
                f"{GitHubAppAuthError.code}: GitHub App private key is invalid"
            ) from exc
        # PyJWT >=2 returns ``str``; guard anyway since this crosses a
        # third-party API boundary and a version regression should fail
        # closed rather than pass a non-string into an Authorization header.
        if not isinstance(encoded, str):
            encoded = encoded.decode("ascii")  # pragma: no cover - PyJWT<2 compatibility
        return encoded

    def token(self) -> str:
        """Return a valid installation access token, minting a fresh one if needed."""
        now = self.clock()
        if (
            self._cached_token is not None
            and now < self._cached_expires_at - TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._cached_token
        installation_id = _require(self.installation_id_provider(), "GitHub App installation id")
        app_jwt = self._mint_jwt()
        response = self.transport.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if not (200 <= response.status_code < 300):  # noqa: PLR2004 - HTTP success range
            raise GitHubAppAuthError(
                f"{GitHubAppAuthError.code}: installation token request failed with status "
                f"{response.status_code}"
            )
        payload = response.payload
        if not isinstance(payload, dict):
            raise GitHubAppAuthError(
                f"{GitHubAppAuthError.code}: installation token response is not an object"
            )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppAuthError(
                f"{GitHubAppAuthError.code}: installation token response is missing 'token'"
            )
        self._cached_token = token
        self._cached_expires_at = _parse_expires_at(payload.get("expires_at"), now=now)
        return token


__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "JWT_ALGORITHM",
    "JWT_TTL_SECONDS",
    "TOKEN_REFRESH_MARGIN_SECONDS",
    "GitHubAppAuthError",
    "GitHubAppInstallationTokenProvider",
    "PrivateKeyProvider",
]
