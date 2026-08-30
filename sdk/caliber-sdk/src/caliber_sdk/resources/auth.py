"""Authentication, tokens, and accounts.

The token surface is the one most SDK users touch: a script needs a credential
before it can do anything else, and personal access tokens are the supported
way to get one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models._decode import decode, decode_list
from ..models.core import Account, IssuedToken, PersonalAccessToken, SessionInfo
from ._base import Resource


class TokensAPI(Resource):
    """Personal access tokens for automation."""

    def list(self) -> list[PersonalAccessToken]:
        """Every token belonging to the caller. Never includes a secret."""
        payload = self._get("/auth/tokens")
        items = payload.get("tokens") if isinstance(payload, dict) else None
        return decode_list(PersonalAccessToken, items)

    def create(
        self,
        name: str,
        *,
        # ``Sequence`` rather than ``list``: inside a class that defines a
        # ``list()`` method, the annotation ``list[str]`` resolves to that
        # method rather than the builtin.
        scopes: Sequence[str] | None = None,
        expires_at: str | None = None,
    ) -> IssuedToken:
        """Issue a token. The plaintext is returned **once** — store it now.

        ``scopes`` is a ceiling, not a grant: the effective authority is the
        intersection with what the owner holds at request time. Omit it to
        inherit the owner's scopes. Requesting a scope the caller does not hold
        is refused rather than silently narrowed.
        """
        body: dict[str, Any] = {"name": name}
        if scopes is not None:
            body["scopes"] = list(scopes)
        if expires_at is not None:
            body["expires_at"] = expires_at
        return decode(IssuedToken, self._post("/auth/tokens", json=body))

    def revoke(self, token_id: str) -> bool:
        """Revoke a token. Returns whether a live token was actually revoked."""
        payload = self._delete(f"/auth/tokens/{token_id}")
        return bool(payload.get("revoked")) if isinstance(payload, dict) else False

    def rotate(self, token_id: str) -> IssuedToken:
        """Replace a token's secret, preserving its name and scope ceiling.

        One transaction on the server: the old token is revoked and the
        replacement issued together, so a failure cannot leave an account with
        two live tokens or none.
        """
        return decode(IssuedToken, self._post(f"/auth/tokens/{token_id}/rotate"))


class AccountsAPI(Resource):
    """User accounts. Admin-only on the server."""

    def list(self) -> list[Account]:
        payload = self._get("/auth/accounts")
        items = payload.get("accounts") if isinstance(payload, dict) else None
        return decode_list(Account, items)

    def create(self, user_id: str, password: str) -> Any:
        return self._post("/auth/accounts", json={"user_id": user_id, "password": password})

    def update(
        self, user_id: str, *, password: str | None = None, disabled: bool | None = None
    ) -> Any:
        """Reset a password or enable/disable an account.

        Both revoke the account's sessions server-side, so they take effect
        immediately rather than at the next expiry.
        """
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if disabled is not None:
            body["disabled"] = disabled
        return self._patch(f"/auth/accounts/{user_id}", json=body)

    def revoke_sessions(self, user_id: str) -> int:
        """Sign an account out everywhere. Returns how many sessions were cut."""
        payload = self._delete(f"/auth/accounts/{user_id}/sessions")
        return int(payload.get("revoked", 0)) if isinstance(payload, dict) else 0


class AuthAPI(Resource):
    """Session inspection, plus the token and account sub-resources."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.tokens = TokensAPI(transport)
        self.accounts = AccountsAPI(transport)

    def session(self) -> SessionInfo:
        """How this client's identity was established."""
        return decode(SessionInfo, self._get("/auth/session"))


__all__ = ["AccountsAPI", "AuthAPI", "TokensAPI"]
