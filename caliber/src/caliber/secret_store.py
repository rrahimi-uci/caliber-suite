"""Encrypted-at-rest secret storage with versions, rotation, and revocation.

Closes the durable half of C2. The prior state: MCP ``env`` / ``headers`` /
``auth_config`` literals were contained on *read* — the API returned a write-only
sentinel — but the values themselves sat in ordinary JSON columns, with "no durable
encrypted/reference-backed resolver, deployment binding, rotation, or revocation
lifecycle". Read containment stops a known disclosure path; it does nothing about a
database dump, a backup, or a replica.

## Shape

A secret is a **named series of versions**. Writing a new value creates a new
version and supersedes the previous one, which is what makes rotation an ordinary
operation rather than a destructive edit:

* ``put`` → new version, becomes current.
* ``resolve`` → plaintext of the current version, or ``None`` if revoked/absent.
* ``revoke`` → the whole name stops resolving. Ciphertext is retained so an
  operator can still audit *when* it was revoked and by whom; the value cannot be
  read back through any API.
* ``purge`` → actually deletes ciphertext, for a real "destroy this" request.

Consumers reference a secret as ``secret://name``, resolved through
:func:`caliber.secrets.resolve_secret`. A workflow, MCP server, or provider config
therefore stores a *reference*, and the value exists in exactly one place.

## Crypto

AES-256-GCM via ``cryptography``, with a random 96-bit nonce per version and the
secret name bound in as additional authenticated data. Binding the name matters:
without it, ciphertext moved from one row to another would decrypt cleanly, so an
attacker with write access to the table could swap one secret's value for another's.

The data-encryption key comes from ``CALIBER_SECRET_ENCRYPTION_KEY`` (or any
``caliber.secrets`` source), 32 bytes base64/hex. **There is deliberately no
fallback**: no key means the store refuses to encrypt rather than storing plaintext
under an "encrypted" name. That is the whole failure mode this module exists to
prevent, and a silent downgrade would be worse than the original defect because it
would *look* fixed.

Key rotation is supported through ``key_id``: each version records which key
encrypted it, and additional keys can be supplied so old versions still decrypt
after the primary key changes.

## What this is not

Not an HSM, not a KMS integration, and not protection against an attacker who
already has the key material and the database together. It removes plaintext at
rest, gives rotation and revocation a real implementation, and puts every consumer
behind one reference — which is what "no secret lifecycle" meant.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

logger = logging.getLogger("caliber.secret_store")

#: ``secret://name`` is the reference form consumers store.
SECRET_SCHEME = "secret://"  # noqa: S105 - a URI scheme, not a secret

#: AES-256 keys only. A shorter key is a configuration error, not something to pad.
_KEY_BYTES = 32
_NONCE_BYTES = 12

#: Secret names are used in references, audit rows, and UI labels, so keep them to
#: an unambiguous character set rather than sanitizing at every use site.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

#: The env var / secret source holding the data-encryption key.
KEY_SOURCE_FIELD = "secret_encryption_key_source"
ADDITIONAL_KEYS_FIELD = "secret_encryption_additional_keys"


class SecretStoreError(RuntimeError):
    """The store cannot operate — no key, bad key, or unusable ciphertext."""


class SecretNotConfiguredError(SecretStoreError):
    """No encryption key is configured.

    A distinct type so callers can tell "the operator has not enabled the secret
    store" from "the secret store is broken", and surface the right message.
    """


def validate_name(name: str) -> str:
    value = (name or "").strip()
    if not NAME_RE.match(value):
        raise SecretStoreError(
            "secret name must be 1-128 characters of letters, digits, '.', '_', or "
            "'-' and start with a letter or digit"
        )
    return value


def is_secret_reference(value: object) -> bool:
    """Whether ``value`` is a ``secret://name`` reference rather than a literal."""
    return isinstance(value, str) and value.startswith(SECRET_SCHEME)


def reference_name(value: str) -> str:
    """Extract the name from a ``secret://name`` reference."""
    return value[len(SECRET_SCHEME) :].strip()


def _decode_key(raw: str) -> bytes:
    """Decode a 32-byte key from base64 or hex.

    Both encodings are accepted because operators paste whichever their key
    management produced; a wrong *length* is always an error, since silently
    stretching or truncating a key would weaken it invisibly.
    """
    text = (raw or "").strip()
    if not text:
        raise SecretNotConfiguredError("no secret encryption key configured")
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            key = decoder(text)
        except (binascii.Error, ValueError):
            continue
        if len(key) == _KEY_BYTES:
            return bytes(key)
    raise SecretStoreError(
        f"secret encryption key must decode to {_KEY_BYTES} bytes from base64 or hex; "
        "generate one with: python -c "
        '"import base64,os;print(base64.b64encode(os.urandom(32)).decode())"'
    )


def generate_key() -> str:
    """A fresh base64 AES-256 key, for operator setup and tests."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")


def key_id_for(key: bytes) -> str:
    """Short, non-reversible identifier for a key.

    Lets each version record which key encrypted it — required for rotation —
    without storing anything that helps recover the key.
    """
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(b"caliber-secret-key-id" + key).hexdigest()[:16]


@dataclass(frozen=True)
class SecretCipher:
    """AES-256-GCM with the secret name bound as additional authenticated data.

    ``additional_keys`` hold superseded keys so versions written before a key
    rotation still decrypt. Encryption always uses ``primary``.
    """

    primary: bytes
    additional: tuple[bytes, ...] = ()

    @classmethod
    def from_config(cls, config: Any) -> SecretCipher:
        from caliber.secrets import resolve_secret  # noqa: PLC0415

        source = str(getattr(config, KEY_SOURCE_FIELD, "") or "").strip()
        if not source:
            raise SecretNotConfiguredError(
                "set CALIBER_SECRET_ENCRYPTION_KEY_SOURCE to an env var or "
                "caliber.secrets URI holding a 32-byte key before using the secret store"
            )
        primary = _decode_key(resolve_secret(source) or "")
        extra_raw = str(getattr(config, ADDITIONAL_KEYS_FIELD, "") or "")
        additional: list[bytes] = []
        for item in extra_raw.split(","):
            candidate = item.strip()
            if not candidate:
                continue
            resolved = resolve_secret(candidate) or candidate
            try:
                additional.append(_decode_key(resolved))
            except SecretStoreError:
                logger.warning("ignoring unusable additional secret key source %r", candidate)
        return cls(primary=primary, additional=tuple(additional))

    @property
    def primary_key_id(self) -> str:
        return key_id_for(self.primary)

    def encrypt(self, name: str, plaintext: str) -> tuple[bytes, bytes, str]:
        """Return ``(nonce, ciphertext, key_id)``."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self.primary).encrypt(
            nonce, plaintext.encode("utf-8"), name.encode("utf-8")
        )
        return nonce, ciphertext, self.primary_key_id

    def decrypt(self, name: str, nonce: bytes, ciphertext: bytes, key_id: str | None) -> str:
        """Decrypt, trying the key that wrote the version first.

        Falls back to the other configured keys so a version written before a
        rotation still opens. A value that no key can open raises rather than
        returning ``None``: silently treating an undecryptable secret as absent would
        let a key mistake look like a missing configuration.
        """
        from cryptography.exceptions import InvalidTag  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        candidates: list[bytes] = []
        if key_id and key_id != self.primary_key_id:
            candidates.extend(k for k in self.additional if key_id_for(k) == key_id)
        candidates.append(self.primary)
        candidates.extend(k for k in self.additional if k not in candidates)
        for key in candidates:
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, name.encode("utf-8")).decode("utf-8")
            except InvalidTag:
                continue
        raise SecretStoreError(
            f"secret {name!r} could not be decrypted with any configured key; "
            "if the encryption key was rotated, add the previous key to "
            "CALIBER_SECRET_ENCRYPTION_ADDITIONAL_KEYS"
        )


class SecretStore:
    """Database-backed secret store. One instance per app, held on ``app.state``."""

    def __init__(self, cipher: SecretCipher) -> None:
        self._cipher = cipher

    @classmethod
    def from_config(cls, config: Any) -> SecretStore:
        return cls(SecretCipher.from_config(config))

    # -- writes ------------------------------------------------------------

    def put(
        self,
        session: Any,
        *,
        name: str,
        value: str,
        actor: str,
        description: str = "",
    ) -> int:
        """Store a new version of ``name`` and make it current.

        Returns the new version number. Rotation is exactly this call: the previous
        version is superseded rather than overwritten, so an operator can see when
        each value took effect.
        """
        from caliber.db.models import CaliberSecret, CaliberSecretVersion  # noqa: PLC0415

        resolved = validate_name(name)
        if not value:
            raise SecretStoreError("secret value must not be empty")
        now = datetime.now(timezone.utc)
        record = session.get(CaliberSecret, resolved)
        if record is None:
            record = CaliberSecret(
                name=resolved,
                description=description,
                created_by=actor,
                created_at=now,
                current_version=0,
            )
            session.add(record)
        elif record.revoked_at is not None:
            # Writing a value to a revoked name un-revokes it: otherwise the only
            # recovery would be deleting audit history.
            record.revoked_at = None
            record.revoked_by = None
        if description:
            record.description = description

        nonce, ciphertext, key_id = self._cipher.encrypt(resolved, value)
        next_version = int(record.current_version or 0) + 1
        session.add(
            CaliberSecretVersion(
                name=resolved,
                version=next_version,
                nonce=nonce,
                ciphertext=ciphertext,
                key_id=key_id,
                created_by=actor,
                created_at=now,
            )
        )
        record.current_version = next_version
        record.updated_at = now
        record.updated_by = actor
        session.flush()
        return next_version

    def revoke(self, session: Any, *, name: str, actor: str) -> bool:
        """Stop ``name`` resolving. Ciphertext is retained for audit."""
        from caliber.db.models import CaliberSecret  # noqa: PLC0415

        record = session.get(CaliberSecret, validate_name(name))
        if record is None or record.revoked_at is not None:
            return False
        record.revoked_at = datetime.now(timezone.utc)
        record.revoked_by = actor
        session.flush()
        return True

    def purge(self, session: Any, *, name: str) -> int:
        """Delete every version's ciphertext and the record. Returns versions removed.

        Distinct from :meth:`revoke` because "stop using this" and "destroy this" are
        different operator intents, and conflating them would make one of them
        impossible.
        """
        from caliber.db.models import CaliberSecret, CaliberSecretVersion  # noqa: PLC0415

        resolved = validate_name(name)
        versions = (
            session.execute(
                select(CaliberSecretVersion).where(CaliberSecretVersion.name == resolved)
            )
            .scalars()
            .all()
        )
        for version in versions:
            session.delete(version)
        record = session.get(CaliberSecret, resolved)
        if record is not None:
            session.delete(record)
        session.flush()
        return len(versions)

    # -- reads -------------------------------------------------------------

    def resolve(self, session: Any, name: str) -> str | None:
        """Plaintext of the current version, or ``None`` if absent or revoked."""
        from caliber.db.models import CaliberSecret, CaliberSecretVersion  # noqa: PLC0415

        try:
            resolved = validate_name(name)
        except SecretStoreError:
            return None
        record = session.get(CaliberSecret, resolved)
        if record is None or record.revoked_at is not None or not record.current_version:
            return None
        version = session.execute(
            select(CaliberSecretVersion).where(
                CaliberSecretVersion.name == resolved,
                CaliberSecretVersion.version == record.current_version,
            )
        ).scalar_one_or_none()
        if version is None:
            return None
        return self._cipher.decrypt(resolved, version.nonce, version.ciphertext, version.key_id)

    def describe(self, session: Any, name: str) -> dict[str, Any] | None:
        """Metadata only — never the value. What a UI or audit surface may show."""
        from caliber.db.models import CaliberSecret  # noqa: PLC0415

        record = session.get(CaliberSecret, validate_name(name))
        if record is None:
            return None
        return {
            "name": record.name,
            "description": record.description or "",
            "current_version": int(record.current_version or 0),
            "revoked": record.revoked_at is not None,
            "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
            "revoked_by": record.revoked_by,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "created_by": record.created_by,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            "updated_by": record.updated_by,
        }

    def list_all(self, session: Any) -> list[dict[str, Any]]:
        from caliber.db.models import CaliberSecret  # noqa: PLC0415

        rows = session.execute(select(CaliberSecret).order_by(CaliberSecret.name)).scalars().all()
        return [entry for row in rows if (entry := self.describe(session, row.name)) is not None]


__all__ = [
    "ADDITIONAL_KEYS_FIELD",
    "KEY_SOURCE_FIELD",
    "NAME_RE",
    "SECRET_SCHEME",
    "SecretCipher",
    "SecretNotConfiguredError",
    "SecretStore",
    "SecretStoreError",
    "generate_key",
    "is_secret_reference",
    "key_id_for",
    "reference_name",
    "validate_name",
]
