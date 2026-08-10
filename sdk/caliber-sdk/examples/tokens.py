"""Issue a scoped automation token, use it, and revoke it."""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def issue_scoped_token(caliber: CaliberClient, *, name: str = "ci") -> dict[str, Any]:
    """Create a token limited to operator scope.

    The scope list is a *ceiling*: the effective authority is the intersection
    with what the owner holds when the request is made. Requesting more than
    you hold is refused outright rather than silently narrowed, so a token
    never claims authority it cannot exercise.
    """
    issued = caliber.auth.tokens.create(name, scopes=["caliber.operator"])
    # The plaintext exists exactly once. There is no endpoint that returns it
    # again — store it now or rotate to get a new one.
    secret = issued.token

    live = [token.token_id for token in caliber.auth.tokens.list() if token.active]
    caliber.auth.tokens.revoke(issued.token_id)
    return {"token_id": issued.token_id, "secret_len": len(secret), "live_before_revoke": live}
