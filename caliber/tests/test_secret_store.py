"""Regression tests for the encrypted secret store (durable half of C2).

The review's finding: MCP ``env`` / ``headers`` / ``auth_config`` literals were
contained on *read* — the API returned a write-only sentinel — but the values sat in
ordinary JSON columns "with no durable encrypted/reference-backed resolver,
deployment binding, rotation, or revocation lifecycle". Read containment stops a
known disclosure path; it does nothing about a dump, a backup, or a replica.

Five properties make this a real fix rather than a rename:

1. plaintext never reaches the database, and never comes back out through the API;
2. rotation is a new version, not a destructive edit;
3. revocation makes the reference resolve to *nothing*, so consumers fail closed;
4. ciphertext is bound to its secret name, so swapping rows does not swap values; and
5. a missing key refuses to encrypt rather than silently storing plaintext.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import CaliberSecretVersion
from caliber.secret_store import (
    SECRET_SCHEME,
    SecretCipher,
    SecretNotConfiguredError,
    SecretStore,
    SecretStoreError,
    generate_key,
    is_secret_reference,
    reference_name,
)
from caliber.secrets import bind_secret_store, resolve_secret, unbind_secret_store

SECRET_VALUE = "sk-live-do-not-log-this"
LIST_PATH = "/ajax-api/2.0/mlflow/caliber/secrets"


@pytest.fixture
def store() -> SecretStore:
    return SecretStore(SecretCipher(primary=_raw_key()))


def _raw_key() -> bytes:
    import base64

    return base64.b64decode(generate_key())


@pytest.fixture
def bound_store(store: SecretStore, session_factory) -> Iterator[SecretStore]:  # type: ignore[no-untyped-def]
    """Bind the store for ``secret://`` resolution, and always unbind after.

    Unbinding matters: the binding is process-wide, so one test's store leaking into
    another would make failures order-dependent.
    """
    bind_secret_store(store, session_factory)
    yield store
    unbind_secret_store()


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------


def test_plaintext_never_reaches_the_database(store: SecretStore, db_session: Session) -> None:
    """The entire point: a dump, backup, or replica must not contain the value."""
    store.put(db_session, name="openai", value=SECRET_VALUE, actor="@admin")
    row = db_session.query(CaliberSecretVersion).one()
    assert SECRET_VALUE.encode() not in row.ciphertext
    assert SECRET_VALUE not in repr(row.ciphertext)
    assert len(row.nonce) == 12
    # ...and it still round-trips.
    assert store.resolve(db_session, "openai") == SECRET_VALUE


def test_the_same_value_encrypts_differently_each_time(
    store: SecretStore, db_session: Session
) -> None:
    """A per-version nonce; identical values must not produce identical ciphertext,
    or the table would leak which secrets share a value."""
    store.put(db_session, name="a", value=SECRET_VALUE, actor="@admin")
    store.put(db_session, name="b", value=SECRET_VALUE, actor="@admin")
    rows = db_session.query(CaliberSecretVersion).all()
    assert rows[0].ciphertext != rows[1].ciphertext


def test_ciphertext_is_bound_to_its_secret_name(store: SecretStore, db_session: Session) -> None:
    """Without binding the name as additional authenticated data, an attacker with
    write access could move one secret's ciphertext onto another's row and have it
    decrypt cleanly — substituting a value they never knew."""
    store.put(db_session, name="prod-key", value=SECRET_VALUE, actor="@admin")
    store.put(db_session, name="dev-key", value="dev-value-here", actor="@admin")
    prod = (
        db_session.query(CaliberSecretVersion).filter(CaliberSecretVersion.name == "prod-key").one()
    )
    dev = (
        db_session.query(CaliberSecretVersion).filter(CaliberSecretVersion.name == "dev-key").one()
    )
    # Transplant prod's ciphertext onto dev's row.
    dev.nonce, dev.ciphertext, dev.key_id = prod.nonce, prod.ciphertext, prod.key_id
    db_session.flush()
    with pytest.raises(SecretStoreError, match="could not be decrypted"):
        store.resolve(db_session, "dev-key")


def test_a_missing_key_refuses_rather_than_storing_plaintext() -> None:
    """A silent downgrade would be worse than the original defect, because it would
    look fixed."""
    with pytest.raises(SecretNotConfiguredError):
        SecretStore.from_config(CaliberConfig())


@pytest.mark.parametrize("bad", ["not-base64!!", "aGVsbG8=", "00" * 16 + "zz"])
def test_a_wrong_length_key_is_an_error_not_padded(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stretching or truncating a key would weaken it invisibly."""
    monkeypatch.setenv("CALIBER_TEST_BAD_KEY", bad)
    with pytest.raises(SecretStoreError):
        SecretStore.from_config(CaliberConfig(secret_encryption_key_source="CALIBER_TEST_BAD_KEY"))


def test_a_key_from_hex_or_base64_both_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    raw = _raw_key()
    monkeypatch.setenv("CALIBER_TEST_B64", base64.b64encode(raw).decode())
    monkeypatch.setenv("CALIBER_TEST_HEX", raw.hex())
    b64 = SecretStore.from_config(CaliberConfig(secret_encryption_key_source="CALIBER_TEST_B64"))
    hexed = SecretStore.from_config(CaliberConfig(secret_encryption_key_source="CALIBER_TEST_HEX"))
    assert b64._cipher.primary_key_id == hexed._cipher.primary_key_id


# ---------------------------------------------------------------------------
# Rotation and revocation
# ---------------------------------------------------------------------------


def test_writing_again_rotates_rather_than_overwrites(
    store: SecretStore, db_session: Session
) -> None:
    assert store.put(db_session, name="k", value="first-value-here", actor="@a") == 1
    assert store.put(db_session, name="k", value="second-value-here", actor="@b") == 2
    # Both versions are retained, so an operator can see when each took effect.
    assert db_session.query(CaliberSecretVersion).count() == 2
    assert store.resolve(db_session, "k") == "second-value-here"
    described = store.describe(db_session, "k")
    assert described is not None
    assert described["current_version"] == 2
    assert described["updated_by"] == "@b"


def test_revocation_makes_the_reference_resolve_to_nothing(
    store: SecretStore, db_session: Session
) -> None:
    """Consumers treat ``None`` as unresolved and fail closed — that is what
    revocation has to mean to be useful."""
    store.put(db_session, name="k", value=SECRET_VALUE, actor="@a")
    assert store.revoke(db_session, name="k", actor="@admin") is True
    assert store.resolve(db_session, "k") is None
    described = store.describe(db_session, "k")
    assert described is not None
    assert described["revoked"] is True
    assert described["revoked_by"] == "@admin"
    # Revoking twice is not an error, and reports that nothing changed.
    assert store.revoke(db_session, name="k", actor="@admin") is False


def test_writing_to_a_revoked_name_un_revokes_it(store: SecretStore, db_session: Session) -> None:
    """Otherwise the only recovery would be deleting audit history."""
    store.put(db_session, name="k", value=SECRET_VALUE, actor="@a")
    store.revoke(db_session, name="k", actor="@admin")
    store.put(db_session, name="k", value="rotated-value-here", actor="@a")
    assert store.resolve(db_session, "k") == "rotated-value-here"


def test_purge_actually_deletes_ciphertext(store: SecretStore, db_session: Session) -> None:
    """Distinct from revoke: "stop using this" and "destroy this" are different
    intents, and conflating them makes one impossible."""
    store.put(db_session, name="k", value=SECRET_VALUE, actor="@a")
    store.put(db_session, name="k", value="v2-value-here", actor="@a")
    assert store.purge(db_session, name="k") == 2
    assert db_session.query(CaliberSecretVersion).count() == 0
    assert store.describe(db_session, "k") is None
    assert store.resolve(db_session, "k") is None


def test_key_rotation_keeps_old_versions_readable(db_session: Session) -> None:
    """An operator rotating the data-encryption key must not lose existing secrets."""
    old_key, new_key = _raw_key(), _raw_key()
    old_store = SecretStore(SecretCipher(primary=old_key))
    old_store.put(db_session, name="k", value=SECRET_VALUE, actor="@a")

    # New primary, old key retained: the existing version still opens.
    rotated = SecretStore(SecretCipher(primary=new_key, additional=(old_key,)))
    assert rotated.resolve(db_session, "k") == SECRET_VALUE
    # A new write uses the new key...
    rotated.put(db_session, name="k", value="written-under-new-key", actor="@a")
    assert rotated.resolve(db_session, "k") == "written-under-new-key"
    # ...and dropping the old key breaks only the versions it wrote.
    orphaned = SecretStore(SecretCipher(primary=new_key))
    assert orphaned.resolve(db_session, "k") == "written-under-new-key"


def test_dropping_the_key_that_wrote_a_version_is_a_clear_error(db_session: Session) -> None:
    """Treating an undecryptable secret as *absent* would let a key mistake look
    like a missing configuration."""
    old_store = SecretStore(SecretCipher(primary=_raw_key()))
    old_store.put(db_session, name="k", value=SECRET_VALUE, actor="@a")
    wrong = SecretStore(SecretCipher(primary=_raw_key()))
    with pytest.raises(SecretStoreError, match="ADDITIONAL_KEYS"):
        wrong.resolve(db_session, "k")


@pytest.mark.parametrize("bad", ["", "  ", "has space", "-leading", "x" * 200, "semi;colon"])
def test_invalid_secret_names_are_rejected(
    store: SecretStore, db_session: Session, bad: str
) -> None:
    with pytest.raises(SecretStoreError):
        store.put(db_session, name=bad, value=SECRET_VALUE, actor="@a")


def test_an_empty_value_is_rejected(store: SecretStore, db_session: Session) -> None:
    """Storing empty would be indistinguishable from revoked at resolution time."""
    with pytest.raises(SecretStoreError):
        store.put(db_session, name="k", value="", actor="@a")


# ---------------------------------------------------------------------------
# The secret:// reference form
# ---------------------------------------------------------------------------


def test_reference_helpers() -> None:
    assert is_secret_reference(f"{SECRET_SCHEME}openai") is True
    assert is_secret_reference("env://OPENAI_API_KEY") is False
    assert is_secret_reference(None) is False
    assert reference_name(f"{SECRET_SCHEME}openai") == "openai"


def test_resolve_secret_reads_through_the_bound_store(
    bound_store: SecretStore, db_session: Session
) -> None:
    """This is what lets an MCP/provider/tool field hold a reference instead of a
    literal."""
    bound_store.put(db_session, name="openai", value=SECRET_VALUE, actor="@a")
    db_session.commit()
    assert resolve_secret(f"{SECRET_SCHEME}openai") == SECRET_VALUE


def test_a_revoked_reference_resolves_to_none_not_a_fallback(
    bound_store: SecretStore, db_session: Session
) -> None:
    bound_store.put(db_session, name="openai", value=SECRET_VALUE, actor="@a")
    bound_store.revoke(db_session, name="openai", actor="@admin")
    db_session.commit()
    assert resolve_secret(f"{SECRET_SCHEME}openai") is None


def test_an_unbound_store_resolves_to_none_rather_than_plaintext() -> None:
    """No store bound must not degrade to reading an env var of the same name — a
    consumer that gets ``None`` fails closed, which is correct."""
    unbind_secret_store()
    assert resolve_secret(f"{SECRET_SCHEME}anything") is None


def test_other_secret_schemes_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a scheme must not disturb the existing ones."""
    monkeypatch.setenv("CALIBER_TEST_PLAIN", "from-env")
    assert resolve_secret("CALIBER_TEST_PLAIN") == "from-env"
    assert resolve_secret("env://CALIBER_TEST_PLAIN") == "from-env"


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def secrets_client(client: TestClient, store: SecretStore) -> Iterator[TestClient]:
    client.app.state.secret_store = store
    bind_secret_store(store, client.app.state.session_factory)
    yield client
    unbind_secret_store()


def test_the_api_never_returns_a_stored_value(secrets_client: TestClient) -> None:
    """The invariant: writes go in, only metadata comes out. An endpoint that
    returned plaintext would recreate exactly the readback path C2 is about."""
    put = secrets_client.put(
        "/ajax-api/2.0/mlflow/caliber/secrets/openai", json={"value": SECRET_VALUE}
    )
    assert put.status_code == 201, put.text
    assert SECRET_VALUE not in put.text
    assert put.json()["data"]["reference"] == f"{SECRET_SCHEME}openai"

    listed = secrets_client.get(LIST_PATH)
    assert listed.status_code == 200
    assert SECRET_VALUE not in listed.text
    assert [s["name"] for s in listed.json()["data"]["secrets"]] == ["openai"]


def test_rotation_over_http_bumps_the_version(secrets_client: TestClient) -> None:
    secrets_client.put("/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": "first-value-here"})
    second = secrets_client.put(
        "/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": "second-value-here"}
    )
    assert second.status_code == 200
    assert second.json()["data"]["current_version"] == 2


def test_revoke_and_delete_over_http(secrets_client: TestClient) -> None:
    secrets_client.put("/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": SECRET_VALUE})
    revoked = secrets_client.post("/ajax-api/2.0/mlflow/caliber/secrets/k/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["data"]["revoked"] is True

    deleted = secrets_client.delete("/ajax-api/2.0/mlflow/caliber/secrets/k")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["versions_removed"] == 1
    assert secrets_client.get(LIST_PATH).json()["data"]["total"] == 0


def test_unknown_secret_operations_are_404(secrets_client: TestClient) -> None:
    assert (
        secrets_client.post("/ajax-api/2.0/mlflow/caliber/secrets/ghost/revoke").status_code == 404
    )
    assert secrets_client.delete("/ajax-api/2.0/mlflow/caliber/secrets/ghost").status_code == 404


def test_an_empty_value_is_a_400(secrets_client: TestClient) -> None:
    resp = secrets_client.put("/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": ""})
    assert resp.status_code == 400


def test_secret_administration_requires_admin(client: TestClient, store: SecretStore) -> None:
    client.app.state.secret_store = store
    headers = {"X-CALIBER-User": "@viewer"}
    assert client.get(LIST_PATH, headers=headers).status_code == 403
    assert (
        client.put(
            "/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": SECRET_VALUE}, headers=headers
        ).status_code
        == 403
    )


def test_an_unconfigured_store_is_a_503_that_names_the_setting(client: TestClient) -> None:
    """A deployment state, not a fault — and the message says how to fix it."""
    client.app.state.secret_store = None
    resp = client.get(LIST_PATH)
    assert resp.status_code == 503
    assert "CALIBER_SECRET_ENCRYPTION_KEY_SOURCE" in resp.json()["detail"]


def test_the_audit_row_records_no_value_derived_data(
    secrets_client: TestClient, db_session: Session
) -> None:
    from caliber.db.models import CaliberAuditLog

    secrets_client.put("/ajax-api/2.0/mlflow/caliber/secrets/k", json={"value": SECRET_VALUE})
    rows = db_session.query(CaliberAuditLog).filter(CaliberAuditLog.action == "put_secret").all()
    assert len(rows) == 1
    # Not the value, not a length, not a prefix — the audit trail is itself a
    # surface C2 had to be contained on.
    assert rows[0].details == {"version": 1}
