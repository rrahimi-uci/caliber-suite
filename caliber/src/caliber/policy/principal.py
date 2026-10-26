"""Subject and workload: the two principals a run carries, as pure, immutable value objects (B03.1, #657).

A run has two distinct principals (ADR 0002, ADR 0004). The **subject** is who started it (a user and the way
they authenticated); the **workload** is what runs (a named workflow pinned to one workflow-version digest). They
are never merged and never substituted for each other: a grant, an approval and a decision each name both.

This module only *defines* them. It has no database lookup, no admission writer and no worker integration:
persisting the identity is #680 and resolving it for a run from a manifest and grant is #684. Nothing here
imports a table, a session or a worker, and ``tests/test_policy_principal.py`` pins that.

* ``Workload.workload_id`` is *derived* from the full workflow-version digest. A caller cannot pick it, and a
  value read back from a manifest is accepted only if it equals the derivation, so a forged or stale id is
  rejected rather than trusted.
* ``Subject.project_id`` is server-side context, not part of the subject on the wire: ``to_manifest`` emits only
  the fields the strict ``run_manifest`` subject schema accepts (``user_id``, ``auth_method``).
* ``Subject.to_identity`` is a pure conversion of an already resolved subject (the caller supplies the scopes it
  already resolved); it never widens scopes or reads a request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from caliber.auth import CaliberIdentity

#: How a subject authenticated: the credential kinds ``caliber.auth`` records (``CaliberIdentity.credential_kind``).
AUTH_METHODS: Final[frozenset[str]] = frozenset({"session", "pat", "trusted_header"})

#: ``subject`` and ``workload`` objects of the ``run_manifest`` v1 schema: exactly these keys, no others.
SUBJECT_FIELDS: Final[frozenset[str]] = frozenset({"user_id", "auth_method"})
WORKLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {"workload_id", "name", "workflow_version_digest"}
)

WORKLOAD_ID_PREFIX: Final[str] = "wl_"
_DIGEST = re.compile(r"[0-9a-f]{64}")


class PrincipalError(ValueError):
    """A principal that is malformed, or whose parts do not agree with each other. Never recoverable by retry."""


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PrincipalError(f"{name} must be a non-empty string with no surrounding whitespace")
    return value


def _exact_fields(kind: str, data: object, expected: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise PrincipalError(f"{kind} must be an object")
    keys = set(data)
    if keys != expected:
        extra, missing = sorted(map(str, keys - expected)), sorted(expected - keys)
        raise PrincipalError(
            f"{kind} must have exactly {sorted(expected)}; extra {extra}, missing {missing}"
        )
    return data


def workload_id_for(workflow_version_digest: str) -> str:
    """The workload id for a workflow version: ``wl_`` plus the *full* 64-hex SHA-256 digest, never a prefix."""
    if not isinstance(workflow_version_digest, str) or not _DIGEST.fullmatch(
        workflow_version_digest
    ):
        raise PrincipalError(
            "workflow_version_digest must be a full lowercase 64-hex SHA-256 digest"
        )
    return WORKLOAD_ID_PREFIX + workflow_version_digest


@dataclass(frozen=True)
class Subject:
    """Who started a run: an authenticated user, how they authenticated, and the project they acted in."""

    user_id: str
    auth_method: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        _text("user_id", self.user_id)
        if not isinstance(self.auth_method, str) or self.auth_method not in AUTH_METHODS:
            raise PrincipalError(
                f"auth_method must be one of {sorted(AUTH_METHODS)}, got {self.auth_method!r}"
            )
        if self.project_id is not None:
            _text("project_id", self.project_id)

    def to_manifest(self) -> dict[str, str]:
        """The ``run_manifest`` ``subject`` object. ``project_id`` is deliberately absent (server-side context)."""
        return {"user_id": self.user_id, "auth_method": self.auth_method}

    @classmethod
    def from_manifest(cls, data: object, *, project_id: str | None = None) -> Subject:
        """Read a manifest ``subject``; any key beyond the schema's two (such as a ``project_id``) is refused."""
        fields = _exact_fields("subject", data, SUBJECT_FIELDS)
        return cls(
            user_id=fields["user_id"], auth_method=fields["auth_method"], project_id=project_id
        )

    def to_identity(self, scopes: frozenset[str]) -> CaliberIdentity:
        """The request identity of this already resolved subject, carrying exactly the scopes given."""
        if not isinstance(scopes, frozenset):
            raise PrincipalError(
                "scopes must be a frozenset of the scopes already resolved for the subject"
            )
        return CaliberIdentity(
            user_id=self.user_id,
            scopes=scopes,
            active_project_id=self.project_id,
            credential_kind=self.auth_method,
        )


@dataclass(frozen=True)
class Workload:
    """What runs: a named workflow pinned to one version digest. Its id is derived from that digest."""

    workload_id: str
    name: str
    workflow_version_digest: str

    def __post_init__(self) -> None:
        _text("name", self.name)
        if self.workload_id != workload_id_for(self.workflow_version_digest):
            raise PrincipalError(
                "workload_id must be the one derived from the full workflow_version_digest"
            )

    @classmethod
    def for_workflow(cls, name: str, workflow_version_digest: str) -> Workload:
        """The only way to *create* a workload: the id is derived, never chosen by the caller."""
        return cls(
            workload_id=workload_id_for(workflow_version_digest),
            name=name,
            workflow_version_digest=workflow_version_digest,
        )

    def to_manifest(self) -> dict[str, str]:
        return {
            "workload_id": self.workload_id,
            "name": self.name,
            "workflow_version_digest": self.workflow_version_digest,
        }

    @classmethod
    def from_manifest(cls, data: object) -> Workload:
        """Read a manifest ``workload``; a ``workload_id`` that is not the derived one is refused as forged."""
        fields = _exact_fields("workload", data, WORKLOAD_FIELDS)
        return cls(
            workload_id=fields["workload_id"],
            name=fields["name"],
            workflow_version_digest=fields["workflow_version_digest"],
        )


@dataclass(frozen=True)
class RunPrincipal:
    """The pair a run acts as, and the grant that authorizes it. Both principals stay distinct."""

    subject: Subject
    workload: Workload
    grant_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, Subject):
            raise PrincipalError("subject must be a Subject")
        if not isinstance(self.workload, Workload):
            raise PrincipalError("workload must be a Workload")
        _text("grant_id", self.grant_id)


__all__ = [
    "AUTH_METHODS",
    "SUBJECT_FIELDS",
    "WORKLOAD_FIELDS",
    "WORKLOAD_ID_PREFIX",
    "PrincipalError",
    "RunPrincipal",
    "Subject",
    "Workload",
    "workload_id_for",
]
