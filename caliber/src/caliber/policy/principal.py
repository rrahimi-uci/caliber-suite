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
* The types are deliberately *stricter* than the ``run_manifest`` v1 schema, which accepts any non-empty string:
  a workload digest must be a full bare SHA-256 and an id must be a clean identifier. A workflow version with no
  digest (``manifest_hash == ""``, a legacy version) therefore cannot form a ``Workload``; the legacy path in #684
  must say so explicitly rather than invent one.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, final

from caliber.auth import ALL_SCOPES, ANONYMOUS, CaliberIdentity

# A service principal is not a user-shaped request identity. It is created by a
# server-owned scheduler or by a validated service token and is admitted only
# after its fixed binding has been checked. Keeping this as an auth method in
# the manifest makes the subject typed without changing the v1 wire shape.
SERVICE_PRINCIPAL_AUTH_METHOD: Final[str] = "service_principal"

#: How a subject authenticated: the credential kinds ``caliber.auth`` records
#: (``CaliberIdentity.credential_kind``). The server-created principal is
#: deliberately separate: it is not a credential resolved from a request.
AUTH_METHODS: Final[frozenset[str]] = frozenset({"session", "pat", "trusted_header"})

SERVICE_PRINCIPAL_UNCONFIGURED: Final[str] = "service_principal_unconfigured"
SERVICE_PRINCIPAL_REVOKED: Final[str] = "service_principal_revoked"
SERVICE_PRINCIPAL_EXPIRED: Final[str] = "service_principal_expired"
SERVICE_PRINCIPAL_ORGANIZATION_MISMATCH: Final[str] = "service_principal_organization_mismatch"
SERVICE_PRINCIPAL_PROJECT_MISMATCH: Final[str] = "service_principal_project_mismatch"
SERVICE_PRINCIPAL_WORKLOAD_MISMATCH: Final[str] = "service_principal_workload_mismatch"
SERVICE_PRINCIPAL_PURPOSE_MISMATCH: Final[str] = "service_principal_purpose_mismatch"
SERVICE_PRINCIPAL_ACTION_NOT_ALLOWED: Final[str] = "service_principal_action_not_allowed"
SERVICE_PRINCIPAL_RESOURCE_NOT_ALLOWED: Final[str] = "service_principal_resource_not_allowed"

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
    """A clean identifier: exactly ``str``, non-empty, no surrounding whitespace, and no control, format or
    line-separator character (a zero-width character makes ``alice`` and ``alice`` differ)."""
    if type(value) is not str or not value or value != value.strip():
        raise PrincipalError(f"{name} must be a non-empty string with no surrounding whitespace")
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    ):
        raise PrincipalError(
            f"{name} must not contain control, format or line-separator characters"
        )
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


class ServicePrincipalError(PrincipalError):
    """A server-created service principal is absent, stale, or outside its binding."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def workload_id_for(workflow_version_digest: str) -> str:
    """The workload id for a workflow version: ``wl_`` plus the *full* 64-hex SHA-256 digest, never a prefix."""
    if type(workflow_version_digest) is not str or not _DIGEST.fullmatch(workflow_version_digest):
        raise PrincipalError(
            "workflow_version_digest must be a full lowercase 64-hex SHA-256 digest"
        )
    return WORKLOAD_ID_PREFIX + workflow_version_digest


@final
@dataclass(frozen=True)
class Subject:
    """Who started a run: an authenticated user, how they authenticated, and the project they acted in."""

    user_id: str
    auth_method: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        _text("user_id", self.user_id)
        if self.user_id.lower() == ANONYMOUS or "," in self.user_id:
            # ``caliber.auth`` never resolves these to a user: it treats them as anonymous.
            raise PrincipalError(
                "user_id must be an authenticated user, not the anonymous sentinel or a list"
            )
        if type(self.auth_method) is not str or self.auth_method not in (
            AUTH_METHODS | {SERVICE_PRINCIPAL_AUTH_METHOD}
        ):
            raise PrincipalError(
                f"auth_method must be one of {sorted(AUTH_METHODS | {SERVICE_PRINCIPAL_AUTH_METHOD})}, "
                f"got {self.auth_method!r}"
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

    def to_identity(
        self,
        scopes: frozenset[str],
        *,
        credential_id: str | None = None,
        credential_project_id: str | None = None,
    ) -> CaliberIdentity:
        """The request identity of this already resolved subject, carrying exactly the scopes given.

        ``scopes`` must be a ``frozenset`` of the scopes ``caliber.auth`` defines (nothing is widened or
        invented). A ``pat`` subject must say *which* token (``credential_id``) so a project-bound token's
        binding (``credential_project_id``) is never silently dropped into what looks like an unbound one.
        """
        if type(scopes) is not frozenset:
            raise PrincipalError(
                "scopes must be a frozenset of the scopes already resolved for the subject"
            )
        unknown = sorted(str(scope) for scope in scopes if scope not in ALL_SCOPES)
        if unknown:
            raise PrincipalError(f"scopes must be defined by caliber.auth, got {unknown}")
        if self.auth_method == "pat" and credential_id is None:
            raise PrincipalError("a pat subject needs its credential_id to become an identity")
        if self.auth_method not in {"pat", SERVICE_PRINCIPAL_AUTH_METHOD} and (
            credential_id is not None or credential_project_id is not None
        ):
            raise PrincipalError(
                "only a pat subject carries a credential_id or credential_project_id"
            )
        if self.auth_method == SERVICE_PRINCIPAL_AUTH_METHOD and (
            credential_id is None or credential_project_id is not None
        ):
            raise PrincipalError(
                "a service_principal subject needs its server-created credential_id and no project binding"
            )
        return CaliberIdentity(
            user_id=self.user_id,
            scopes=scopes,
            active_project_id=self.project_id,
            credential_kind=self.auth_method,
            credential_id=credential_id,
            credential_project_id=credential_project_id,
        )


@final
@dataclass(frozen=True)
class Workload:
    """What runs: a named workflow pinned to one version digest. Its id is derived from that digest."""

    workload_id: str
    name: str
    workflow_version_digest: str

    def __post_init__(self) -> None:
        _text("name", self.name)
        if type(self.workload_id) is not str or self.workload_id != workload_id_for(
            self.workflow_version_digest
        ):
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


@final
@dataclass(frozen=True)
class ServicePrincipal:
    """A server-controlled, workload-bound subject for non-human run ingress.

    This is intentionally not persisted as a standing credential. A scheduler
    creates one from the active deployment, while a service route creates one
    from a token that was already validated in the same transaction. The run
    grant is the durable authority; these fields are the claims that admission
    checks before that grant exists.
    """

    principal_id: str
    tenant_id: str
    project_id: str
    workflow_id: str
    workload_id: str
    purpose: str
    allowed_actions: frozenset[str]
    allowed_resources: frozenset[str]
    expires_at: Any | None = None
    revoked_at: Any | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("principal_id", self.principal_id),
            ("tenant_id", self.tenant_id),
            ("project_id", self.project_id),
            ("workflow_id", self.workflow_id),
            ("workload_id", self.workload_id),
            ("purpose", self.purpose),
        ):
            _text(name, value)
        self.workload_id_for_digest(self.workload_id)
        if type(self.allowed_actions) is not frozenset or not self.allowed_actions:
            raise PrincipalError("allowed_actions must be a non-empty frozenset")
        if type(self.allowed_resources) is not frozenset or not self.allowed_resources:
            raise PrincipalError("allowed_resources must be a non-empty frozenset")
        if any(
            type(value) is not str or not value or value != value.strip()
            for value in self.allowed_actions
        ):
            raise PrincipalError("allowed_actions must contain clean strings")
        if any(
            type(value) is not str or not value or value != value.strip()
            for value in self.allowed_resources
        ):
            raise PrincipalError("allowed_resources must contain clean strings")

    @staticmethod
    def workload_id_for_digest(workload_id: str) -> str:
        """Return the value unchanged after checking the derived workload shape."""
        if type(workload_id) is not str or not workload_id.startswith(WORKLOAD_ID_PREFIX):
            raise PrincipalError("workload_id must be derived from a workflow-version digest")
        digest = workload_id[len(WORKLOAD_ID_PREFIX) :]
        return workload_id_for(digest)

    @property
    def auth_method(self) -> str:
        return SERVICE_PRINCIPAL_AUTH_METHOD

    def to_identity(self) -> CaliberIdentity:
        """Build the internal identity with no ambient user scopes."""
        return CaliberIdentity(
            user_id=self.principal_id,
            scopes=frozenset(),
            active_project_id=self.project_id,
            credential_kind=SERVICE_PRINCIPAL_AUTH_METHOD,
            credential_id=self.principal_id,
        )

    def to_subject(self) -> Subject:
        return Subject(
            user_id=self.principal_id,
            auth_method=SERVICE_PRINCIPAL_AUTH_METHOD,
            project_id=self.project_id,
        )

    def with_admission_resources(
        self, *, actions: frozenset[str], resources: frozenset[str]
    ) -> ServicePrincipal:
        """Add resources derived by admission from the sealed workload bundle.

        Only the server can call this while resolving pinned context. It does
        not accept caller-supplied resources and preserves the fixed primary
        action/resource claims.
        """
        return ServicePrincipal(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            workflow_id=self.workflow_id,
            workload_id=self.workload_id,
            purpose=self.purpose,
            allowed_actions=self.allowed_actions | actions,
            allowed_resources=self.allowed_resources | resources,
            expires_at=self.expires_at,
            revoked_at=self.revoked_at,
        )

    def validate_binding(
        self,
        *,
        tenant_id: str,
        project_id: str,
        workflow_id: str,
        workload: Workload,
        purpose: str,
        action: str,
        resource: str,
        now: object | None = None,
    ) -> None:
        """Fail closed when the server-created principal no longer matches the run."""
        if self.revoked_at is not None:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_REVOKED, "the service principal is revoked"
            )
        if self.expires_at is not None and now is not None and self.expires_at <= now:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_EXPIRED, "the service principal is expired"
            )
        if tenant_id != self.tenant_id:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_ORGANIZATION_MISMATCH,
                "the service principal is bound to another deployment organization",
            )
        if project_id != self.project_id:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_PROJECT_MISMATCH,
                "the service principal is bound to another project",
            )
        if workflow_id != self.workflow_id or workload.workload_id != self.workload_id:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_WORKLOAD_MISMATCH,
                "the service principal is bound to another workflow workload",
            )
        if purpose != self.purpose:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_PURPOSE_MISMATCH,
                "the service principal is bound to another run purpose",
            )
        if action not in self.allowed_actions:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_ACTION_NOT_ALLOWED,
                "the service principal is not allowed to perform this action",
            )
        if resource not in self.allowed_resources:
            raise ServicePrincipalError(
                SERVICE_PRINCIPAL_RESOURCE_NOT_ALLOWED,
                "the service principal is not allowed to access this resource",
            )


@final
@dataclass(frozen=True)
class RunPrincipal:
    """The pair a run acts as, and the grant that authorizes it. Both principals stay distinct."""

    subject: Subject
    workload: Workload
    grant_id: str

    def __post_init__(self) -> None:
        if type(self.subject) is not Subject:
            raise PrincipalError("subject must be a Subject")
        if type(self.workload) is not Workload:
            raise PrincipalError("workload must be a Workload")
        _text("grant_id", self.grant_id)


__all__ = [
    "AUTH_METHODS",
    "SERVICE_PRINCIPAL_ACTION_NOT_ALLOWED",
    "SERVICE_PRINCIPAL_AUTH_METHOD",
    "SERVICE_PRINCIPAL_EXPIRED",
    "SERVICE_PRINCIPAL_ORGANIZATION_MISMATCH",
    "SERVICE_PRINCIPAL_PROJECT_MISMATCH",
    "SERVICE_PRINCIPAL_PURPOSE_MISMATCH",
    "SERVICE_PRINCIPAL_RESOURCE_NOT_ALLOWED",
    "SERVICE_PRINCIPAL_REVOKED",
    "SERVICE_PRINCIPAL_UNCONFIGURED",
    "SERVICE_PRINCIPAL_WORKLOAD_MISMATCH",
    "SUBJECT_FIELDS",
    "WORKLOAD_FIELDS",
    "WORKLOAD_ID_PREFIX",
    "PrincipalError",
    "RunPrincipal",
    "ServicePrincipal",
    "ServicePrincipalError",
    "Subject",
    "Workload",
    "workload_id_for",
]
