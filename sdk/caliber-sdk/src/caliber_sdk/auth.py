"""Authentication strategies for the CALIBER management API.

CALIBER resolves identity three ways (``caliber.auth``), and the SDK supports
the two that make sense for a program:

* a **personal access token** or session token in ``Authorization: Bearer`` --
  the normal choice for automation;
* a **trusted header** (``X-CALIBER-User``), valid only where the deployment
  runs in ``trusted_header`` mode behind a proxy.

The browser's HttpOnly cookie is deliberately not modelled: a script that has
the cookie has the token, and the Bearer form is the supported path.
"""

from __future__ import annotations

from typing import Protocol

from .errors import CaliberConfigError


class AuthProvider(Protocol):
    """Supplies per-request auth headers.

    A protocol rather than a base class so a caller can plug in their own --
    fetching a short-lived token from a secret manager, for instance -- without
    subclassing anything in this package.
    """

    def headers(self) -> dict[str, str]:
        """Headers to attach to every request."""

    @property
    def uses_cookie_auth(self) -> bool:
        """Whether this credential is cookie-based.

        Drives CSRF: CALIBER's protection exists for browser credentials, and a
        Bearer client should not be forced to bootstrap a token it does not
        need. See :mod:`caliber_sdk.csrf`.
        """


class TokenAuth:
    """``Authorization: Bearer <token>`` — personal access or session token."""

    def __init__(self, token: str) -> None:
        cleaned = (token or "").strip()
        if not cleaned:
            raise CaliberConfigError("token must not be empty")
        self._token = cleaned

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def uses_cookie_auth(self) -> bool:
        return False

    def __repr__(self) -> str:
        # Never render the token. A repr lands in logs and tracebacks, which is
        # exactly where a credential must not appear.
        return "TokenAuth(token='***')"


class TrustedHeaderAuth:
    """``X-CALIBER-User`` — only for deployments in ``trusted_header`` mode.

    Carries no proof of identity by itself, which is why CALIBER ignores the
    header entirely in the default ``session`` mode. Offered because local
    development and proxy-terminated deployments genuinely use it.
    """

    def __init__(self, user: str, *, proxy_secret: str | None = None) -> None:
        cleaned = (user or "").strip()
        if not cleaned:
            raise CaliberConfigError("user must not be empty")
        if "," in cleaned:
            # The server parses its scope user-lists as comma separated and
            # rejects values containing a comma. Failing here gives a clear
            # error instead of a confusing 401 later.
            raise CaliberConfigError("user must not contain a comma")
        self._user = cleaned
        self._proxy_secret = (proxy_secret or "").strip() or None

    def headers(self) -> dict[str, str]:
        headers = {"X-CALIBER-User": self._user}
        if self._proxy_secret:
            headers["X-CALIBER-Proxy-Secret"] = self._proxy_secret
        return headers

    @property
    def uses_cookie_auth(self) -> bool:
        return False

    def __repr__(self) -> str:
        secret = "***" if self._proxy_secret else None
        return f"TrustedHeaderAuth(user={self._user!r}, proxy_secret={secret})"


class NoAuth:
    """Send no credential. Useful for probing an unauthenticated endpoint."""

    def headers(self) -> dict[str, str]:
        return {}

    @property
    def uses_cookie_auth(self) -> bool:
        return False


__all__ = ["AuthProvider", "NoAuth", "TokenAuth", "TrustedHeaderAuth"]
