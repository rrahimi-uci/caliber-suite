"""Tests for the GitHub App installation-token exchange (`P4-E`).

Fully offline: a real RSA key pair is generated in-process (CPU-only, no
network) so the JWT signing path is exercised for real, and a fake
``GitHubTransport`` (the same shape ``test_github_source_control.py`` uses)
stands in for the network call to
``POST /app/installations/{id}/access_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from caliber.github_app_auth import (
    GitHubAppAuthError,
    GitHubAppInstallationTokenProvider,
)
from caliber.github_source_control import GitHubResponse


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem.decode("ascii"), public_pem.decode("ascii")


@pytest.fixture
def rsa_private_key_pem(rsa_keypair: tuple[str, str]) -> str:
    return rsa_keypair[0]


@dataclass
class _Transport:
    responses: list[GitHubResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method, path, *, headers, params=None, json_body=None):
        self.calls.append((method, path, dict(headers)))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _provider(
    transport: _Transport,
    *,
    rsa_private_key_pem: str,
    app_id: str = "app-1",
    installation_id: str = "install-1",
    clock: list[float] | None = None,
) -> GitHubAppInstallationTokenProvider:
    clock_values = clock or [1000.0]

    def _clock() -> float:
        return clock_values[-1]

    return GitHubAppInstallationTokenProvider(
        transport=transport,
        app_id_provider=lambda: app_id,
        installation_id_provider=lambda: installation_id,
        private_key_provider=lambda: rsa_private_key_pem,
        clock=_clock,
    )


def test_token_mints_a_valid_rs256_jwt_and_returns_the_installation_token(
    rsa_keypair: tuple[str, str],
) -> None:
    private_key_pem, public_key_pem = rsa_keypair
    transport = _Transport(
        [GitHubResponse(status_code=201, payload={"token": "ghs_abc", "expires_at": None})]
    )
    provider = _provider(transport, rsa_private_key_pem=private_key_pem)

    token = provider.token()

    assert token == "ghs_abc"
    method, path, headers = transport.calls[0]
    assert method == "POST"
    assert path == "/app/installations/install-1/access_tokens"
    assert headers["Authorization"].startswith("Bearer ")
    app_jwt = headers["Authorization"].removeprefix("Bearer ")
    # Verified against the *public* half of the key pair -- proves the
    # provider actually signed with the private key it was given, not just
    # that it produced JWT-shaped output.
    decoded = jwt.decode(
        app_jwt, public_key_pem, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert decoded["iss"] == "app-1"
    assert decoded["exp"] > decoded["iat"]


def test_token_is_cached_until_close_to_its_reported_expiry(rsa_private_key_pem: str) -> None:
    transport = _Transport(
        [
            GitHubResponse(
                status_code=200,
                payload={"token": "ghs_first", "expires_at": "1970-01-01T01:00:00Z"},
            ),
            GitHubResponse(status_code=200, payload={"token": "ghs_second", "expires_at": None}),
        ]
    )
    clock = [0.0]
    provider = _provider(transport, rsa_private_key_pem=rsa_private_key_pem, clock=clock)

    first = provider.token()
    # Still well inside the cached lifetime -- no second request.
    clock[0] = 100.0
    second = provider.token()
    assert first == second == "ghs_first"
    assert len(transport.calls) == 1

    # Past the refresh margin before the reported expiry -- must refresh.
    clock[0] = 3600.0 - 30.0
    third = provider.token()
    assert third == "ghs_second"
    assert len(transport.calls) == 2


def test_token_raises_on_non_success_status(rsa_private_key_pem: str) -> None:
    transport = _Transport([GitHubResponse(status_code=401, payload={"message": "Bad JWT"})])
    provider = _provider(transport, rsa_private_key_pem=rsa_private_key_pem)

    with pytest.raises(GitHubAppAuthError, match="status 401"):
        provider.token()


def test_token_raises_when_response_is_not_an_object(rsa_private_key_pem: str) -> None:
    transport = _Transport([GitHubResponse(status_code=200, payload=["not", "an", "object"])])
    provider = _provider(transport, rsa_private_key_pem=rsa_private_key_pem)

    with pytest.raises(GitHubAppAuthError, match="not an object"):
        provider.token()


def test_token_raises_when_token_field_is_missing(rsa_private_key_pem: str) -> None:
    transport = _Transport([GitHubResponse(status_code=200, payload={"expires_at": None})])
    provider = _provider(transport, rsa_private_key_pem=rsa_private_key_pem)

    with pytest.raises(GitHubAppAuthError, match="missing 'token'"):
        provider.token()


def test_token_raises_when_app_id_is_empty(rsa_private_key_pem: str) -> None:
    transport = _Transport([GitHubResponse(status_code=200, payload={"token": "x"})])
    provider = GitHubAppInstallationTokenProvider(
        transport=transport,
        app_id_provider=lambda: "",
        installation_id_provider=lambda: "install-1",
        private_key_provider=lambda: rsa_private_key_pem,
    )

    with pytest.raises(GitHubAppAuthError, match="App id"):
        provider.token()


def test_token_raises_when_installation_id_is_empty(rsa_private_key_pem: str) -> None:
    transport = _Transport([GitHubResponse(status_code=200, payload={"token": "x"})])
    provider = GitHubAppInstallationTokenProvider(
        transport=transport,
        app_id_provider=lambda: "app-1",
        installation_id_provider=lambda: "",
        private_key_provider=lambda: rsa_private_key_pem,
    )

    with pytest.raises(GitHubAppAuthError, match="installation id"):
        provider.token()


def test_token_falls_back_to_a_default_ttl_when_expires_at_is_unparseable(
    rsa_private_key_pem: str,
) -> None:
    """A token GitHub actually returned should still be usable even if this
    process cannot parse its ``expires_at`` -- the cache just refreshes
    sooner than strictly necessary rather than treating the token as unusable."""
    transport = _Transport(
        [
            GitHubResponse(
                status_code=200,
                payload={"token": "ghs_weird", "expires_at": "not-a-real-timestamp"},
            ),
            GitHubResponse(status_code=200, payload={"token": "ghs_next", "expires_at": None}),
        ]
    )
    clock = [0.0]
    provider = _provider(transport, rsa_private_key_pem=rsa_private_key_pem, clock=clock)

    first = provider.token()
    assert first == "ghs_weird"
    assert len(transport.calls) == 1

    # Still comfortably inside the fallback default TTL -- cached.
    clock[0] = 60.0
    assert provider.token() == "ghs_weird"
    assert len(transport.calls) == 1


def test_token_raises_when_private_key_is_invalid() -> None:
    transport = _Transport([GitHubResponse(status_code=200, payload={"token": "x"})])
    provider = GitHubAppInstallationTokenProvider(
        transport=transport,
        app_id_provider=lambda: "app-1",
        installation_id_provider=lambda: "install-1",
        private_key_provider=lambda: "not-a-real-pem-key",
    )

    with pytest.raises(GitHubAppAuthError, match="private key is invalid"):
        provider.token()
