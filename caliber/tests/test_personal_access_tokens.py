"""Personal access tokens: issuance, authentication, scoping, and revocation.

The security properties, in the order they matter:

1. The plaintext is returned once and never stored.
2. A token authenticates as its owner.
3. A token's scopes are a *ceiling*, intersected with what its owner holds now
   -- so it cannot grant authority the owner lacks, and a demotion narrows every
   token that user owns immediately.
4. Revoked and expired tokens stop working.
5. One user cannot see, revoke, or rotate another user's tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, SCOPE_VIEWER
from caliber.db.models import CaliberAuditLog, CaliberPersonalAccessToken
from caliber.routes.auth import TOKENS_PATH
from caliber.sessions import PAT_PREFIX, create_personal_access_token, token_fingerprint

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CAPABILITIES = PREFIX + "/capabilities"


def _issue(client: TestClient, **payload: object) -> dict:
    body = {"name": "ci-automation", **payload}
    response = client.post(TOKENS_PATH, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _as_token(token: str) -> dict[str, str]:
    """Headers for a PAT-authenticated request.

    The empty ``X-CALIBER-User`` matters: the fixture client sends an admin
    header by default, and without clearing it the request would authenticate
    by header and prove nothing about the token.
    """
    return {"Authorization": f"Bearer {token}", "X-CALIBER-User": ""}


def test_issued_token_is_returned_once_and_stored_only_as_a_digest(
    client: TestClient, db_session: Session
) -> None:
    issued = _issue(client)
    token = issued["token"]
    assert token.startswith(PAT_PREFIX)

    row = db_session.get(CaliberPersonalAccessToken, issued["token_id"])
    assert row is not None
    assert row.token_hash == token_fingerprint(token)
    # The credential itself must appear nowhere in the row.
    assert token not in str(row.__dict__)

    # Listing never re-exposes it.
    listed = client.get(TOKENS_PATH).json()["data"]["tokens"]
    assert [item["token_id"] for item in listed] == [issued["token_id"]]
    assert "token" not in listed[0]


def test_a_token_authenticates_as_its_owner(client: TestClient) -> None:
    token = _issue(client)["token"]
    response = client.get(CAPABILITIES, headers=_as_token(token))
    assert response.status_code == 200, response.text


def test_an_unknown_token_is_rejected(client: TestClient) -> None:
    response = client.get(CAPABILITIES, headers=_as_token(PAT_PREFIX + "not-a-real-token"))
    assert response.status_code in (401, 403)


def test_scopes_are_a_ceiling_not_a_grant(client: TestClient, db_session: Session) -> None:
    """The property the whole design rests on.

    Written directly with a scope its owner does not hold -- which the API would
    refuse to issue -- so this tests resolution rather than validation.
    Authentication succeeds as that user, but the intersection with what the
    user actually holds is empty of admin, so the authority is not conferred.
    """
    issued = create_personal_access_token(
        db_session,
        user_id="@nobody-special",
        name="over-reaching",
        scopes=[SCOPE_ADMIN],
    )
    db_session.commit()

    # It authenticates: the token is valid and names a real identity.
    assert client.get(CAPABILITIES, headers=_as_token(issued.token)).status_code == 200

    # But the admin scope it claims was never held by @nobody-special, so the
    # intersection drops it and an operator-gated write is refused.
    write = client.post(
        f"{PREFIX}/cookbooks/02/install",
        json={"name": "Must not be permitted"},
        headers=_as_token(issued.token),
    )
    assert write.status_code == 403, write.text


def test_a_disabled_account_revokes_its_tokens(client: TestClient, db_session: Session) -> None:
    """Where the deployment has accounts, disabling one must kill its tokens."""
    from caliber.db.models import CaliberUserAccount

    issued = _issue(client)
    owner = client.get(TOKENS_PATH).json()["data"]["tokens"][0]["user_id"]
    db_session.add(CaliberUserAccount(user_id=owner, password_hash="x", disabled=True))
    db_session.commit()

    assert client.get(CAPABILITIES, headers=_as_token(issued["token"])).status_code in (401, 403)


def test_requesting_a_scope_the_caller_lacks_is_refused(client: TestClient) -> None:
    """Refused at issuance rather than silently intersected away later.

    A token that claims authority it can never exercise is worse than a clear
    error: its failures surface far from their cause.
    """
    response = client.post(
        TOKENS_PATH,
        json={"name": "too-much", "scopes": [SCOPE_ADMIN]},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert response.status_code == 403, response.text
    assert "cannot grant scopes you do not hold" in response.json()["detail"]


def test_unknown_scope_names_are_refused(client: TestClient) -> None:
    response = client.post(TOKENS_PATH, json={"name": "typo", "scopes": ["caliber.opperator"]})
    assert response.status_code == 400
    assert "unknown scopes" in response.json()["detail"]


def test_a_narrowed_token_cannot_perform_what_it_did_not_request(client: TestClient) -> None:
    """A viewer-scoped token must not inherit its admin owner's authority."""
    token = _issue(client, scopes=[SCOPE_VIEWER])["token"]

    # Reads still work.
    assert client.get(CAPABILITIES, headers=_as_token(token)).status_code == 200

    # A write requiring operator does not: the ceiling excludes it even though
    # the owner holds admin.
    write = client.post(
        f"{PREFIX}/cookbooks/02/install",
        json={"name": "Blocked by token scope"},
        headers=_as_token(token),
    )
    assert write.status_code == 403, write.text


def test_a_token_without_explicit_scopes_inherits_its_owner(client: TestClient) -> None:
    """Empty means inherit, not "no authority".

    Treating an empty ceiling as an empty scope set would make the simplest
    possible token -- issued with just a name -- useless.
    """
    token = _issue(client)["token"]
    response = client.post(
        f"{PREFIX}/cookbooks/02/install",
        json={"name": "Allowed by inherited scopes"},
        headers=_as_token(token),
    )
    assert response.status_code == 201, response.text


def test_operator_scope_is_enough_for_an_operator_route(client: TestClient) -> None:
    token = _issue(client, scopes=[SCOPE_OPERATOR])["token"]
    response = client.post(
        f"{PREFIX}/cookbooks/02/install",
        json={"name": "Allowed by operator ceiling"},
        headers=_as_token(token),
    )
    assert response.status_code == 201, response.text


def test_revoked_tokens_stop_authenticating(client: TestClient) -> None:
    issued = _issue(client)
    token, token_id = issued["token"], issued["token_id"]
    assert client.get(CAPABILITIES, headers=_as_token(token)).status_code == 200

    revoked = client.delete(f"{TOKENS_PATH}/{token_id}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked"] is True

    assert client.get(CAPABILITIES, headers=_as_token(token)).status_code in (401, 403)


def test_expired_tokens_stop_authenticating(client: TestClient, db_session: Session) -> None:
    issued = _issue(client)
    row = db_session.get(CaliberPersonalAccessToken, issued["token_id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    assert client.get(CAPABILITIES, headers=_as_token(issued["token"])).status_code in (401, 403)


def test_expiry_must_be_in_the_future(client: TestClient) -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    response = client.post(TOKENS_PATH, json={"name": "stale", "expires_at": past})
    assert response.status_code == 400
    assert "future" in response.json()["detail"]


def test_rotation_issues_a_new_secret_and_retires_the_old_one(client: TestClient) -> None:
    original = _issue(client, scopes=[SCOPE_OPERATOR])
    rotated = client.post(f"{TOKENS_PATH}/{original['token_id']}/rotate")
    assert rotated.status_code == 201, rotated.text
    replacement = rotated.json()["data"]

    assert replacement["token"] != original["token"]
    assert replacement["rotated_from"] == original["token_id"]
    # Name and ceiling carry over, or rotation would silently change authority.
    assert replacement["name"] == original["name"]
    assert replacement["scopes"] == original["scopes"]

    assert client.get(CAPABILITIES, headers=_as_token(replacement["token"])).status_code == 200
    assert client.get(CAPABILITIES, headers=_as_token(original["token"])).status_code in (401, 403)


def test_a_revoked_token_cannot_be_rotated(client: TestClient) -> None:
    issued = _issue(client)
    client.delete(f"{TOKENS_PATH}/{issued['token_id']}")
    response = client.post(f"{TOKENS_PATH}/{issued['token_id']}/rotate")
    assert response.status_code == 409


def test_another_users_token_is_invisible_rather_than_forbidden(
    client: TestClient, db_session: Session
) -> None:
    """404, not 403.

    Distinguishing "exists but not yours" from "does not exist" would let any
    user enumerate every other user's token ids.
    """
    issued = create_personal_access_token(
        db_session, user_id="@someone-else", name="theirs", scopes=[]
    )
    db_session.commit()

    assert client.get(TOKENS_PATH).json()["data"]["tokens"] == []
    assert client.delete(f"{TOKENS_PATH}/{issued.token_id}").status_code == 404
    assert client.post(f"{TOKENS_PATH}/{issued.token_id}/rotate").status_code == 404


def test_issuance_and_revocation_are_audited(client: TestClient, db_session: Session) -> None:
    issued = _issue(client, scopes=[SCOPE_OPERATOR])
    client.delete(f"{TOKENS_PATH}/{issued['token_id']}")

    rows = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_type == "personal_access_token")
        .all()
    )
    actions = {row.action for row in rows}
    assert {"create_personal_access_token", "revoke_personal_access_token"} <= actions
    # The secret must never reach the audit log, which outlives the response and
    # is read by more people than the issuing caller.
    assert all(issued["token"] not in str(row.details) for row in rows)


def test_listing_requires_authentication(client: TestClient) -> None:
    assert client.get(TOKENS_PATH, headers={"X-CALIBER-User": ""}).status_code in (401, 403)


def test_a_session_token_is_not_treated_as_a_pat(client: TestClient) -> None:
    """The prefix branch must not misroute an ordinary session token."""
    response = client.get(CAPABILITIES, headers=_as_token("not-prefixed-at-all"))
    assert response.status_code in (401, 403)
