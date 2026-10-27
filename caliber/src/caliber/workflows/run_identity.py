"""Who a run acts as, from what it was admitted with, and whether they may still do it now (B03.4, #684).

Admission (:mod:`caliber.workflows.admission`) pins a run, issues it one grant and seals the manifest naming that grant.
:func:`identity_for_run` is the way back: from a run id it reads the persisted manifest (digest, schema and run id
checked, :func:`caliber.workflows.run_pins.load_run_manifest`), takes the **exact** grant id the manifest names (there
is no lookup of the newest grant for the run and no ambient fallback) and the subject and workload the manifest
recorded (never anything the caller supplies), and asks the policy (:func:`caliber.policy.decisions.decide`) whether
that subject, running that workload, may still execute that workflow for the manifest's purpose, *now*: the grant must
be the run's and the subject's and the workload's, not revoked or expired, the subject must still be a member of the
project in a role that permits the action, and the policy's own checks must allow it. The decision is recorded
(allow or deny) and nothing about the admission records changes: the manifest, the grant and the earlier decisions are
only read, and a denial revokes nothing.

A run admitted with no manifest (``manifest_legacy``) has no identity; the caller keeps its explicitly labelled legacy
behaviour and no grant is manufactured. An admitted run whose manifest is missing, unreadable or names no grant is
refused, never run. Every refusal is an :class:`IdentityDenied` with a stable ``code``; when the policy refused it
carries the decision's reason codes. An authority store that cannot answer is a refusal (``authority_unavailable``),
never an allow.

The worker calls this after it has verified the pins and before it builds anything (a resumed run is dispatched by the
same worker, so a resume is checked the same way).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkflowRun
from caliber.policy.decisions import (
    REFERENCE_POLICY_V1,
    DecisionInputError,
    Policy,
    PolicyDecision,
    decide,
)
from caliber.policy.grants import AUTHORITY_UNAVAILABLE
from caliber.policy.principal import PrincipalError, Subject, Workload
from caliber.workflows.admission import RUN_ACTION, run_resource
from caliber.workflows.run_pins import PinsError, load_run_manifest

MANIFEST_MISSING: Final = "manifest_missing"
MANIFEST_CORRUPT: Final = "manifest_corrupt"
GRANT_UNBOUND: Final = "grant_unbound"
IDENTITY_INVALID: Final = "identity_invalid"
AUTHORITY_DENIED: Final = "authority_denied"
CODES: Final = frozenset(
    {
        MANIFEST_MISSING,
        MANIFEST_CORRUPT,
        GRANT_UNBOUND,
        IDENTITY_INVALID,
        AUTHORITY_DENIED,
        AUTHORITY_UNAVAILABLE,
    }
)


class IdentityDenied(Exception):  # noqa: N818 - a refusal, named for what it says
    """The run may not act: ``code`` is one of :data:`CODES`, ``reasons`` the policy decision's reason codes."""

    def __init__(self, code: str, message: str, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.reasons = reasons


@dataclass(frozen=True)
class RunIdentity:
    """The run's principals, read from its manifest, with the decision that allowed them to act now."""

    run_id: str
    manifest_id: str
    grant_id: str
    subject: Subject
    workload: Workload
    project_id: str | None
    purpose: str
    decision: PolicyDecision


def identity_for_run(
    session: Session,
    run: CaliberWorkflowRun,
    *,
    policy: Policy = REFERENCE_POLICY_V1,
    now: datetime | None = None,
) -> RunIdentity | None:
    """The run's identity if it may execute now; ``None`` for a legacy run; :class:`IdentityDenied` otherwise.

    Flushes the recorded decision (allow or deny) and never commits: the caller owns the transaction. Reads, and
    never changes, the manifest and the grant.
    """
    if run.manifest_legacy:
        return None
    try:
        loaded = load_run_manifest(session, run.workflow_run_id)
    except PinsError as exc:
        raise IdentityDenied(MANIFEST_CORRUPT, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise IdentityDenied(AUTHORITY_UNAVAILABLE, "the run manifest could not be read") from exc
    if loaded is None:
        raise IdentityDenied(
            MANIFEST_MISSING, "an admitted run has no manifest, so it has no identity"
        )
    record = loaded.record
    grant_id = record.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise IdentityDenied(
            GRANT_UNBOUND,
            "the run's manifest names no grant; the newest grant is never assumed",
        )
    try:
        subject = Subject.from_manifest(record["subject"], project_id=record.get("project_id"))
        workload = Workload.from_manifest(record["workload"])
    except (PrincipalError, KeyError) as exc:
        raise IdentityDenied(
            IDENTITY_INVALID, f"the manifest's principals are invalid: {exc}"
        ) from exc
    purpose = str(record["purpose"])
    try:
        decision = decide(
            session,
            run_id=run.workflow_run_id,
            grant_id=grant_id,
            subject=subject,
            workload=workload,
            action=RUN_ACTION,
            resource=run_resource(run.workflow_id),
            purpose=purpose,
            policy=policy,
            now=now if now is not None else datetime.now(timezone.utc),
        )
    except DecisionInputError as exc:
        raise IdentityDenied(IDENTITY_INVALID, f"the decision could not be asked: {exc}") from exc
    except SQLAlchemyError as exc:
        raise IdentityDenied(AUTHORITY_UNAVAILABLE, "the authority store is unavailable") from exc
    if not decision.allowed:
        code = (
            AUTHORITY_UNAVAILABLE
            if AUTHORITY_UNAVAILABLE in decision.reason_codes
            else AUTHORITY_DENIED
        )
        raise IdentityDenied(
            code,
            "the run is no longer authorized to execute: "
            + ", ".join(decision.reason_codes or ("denied",)),
            decision.reason_codes,
        )
    return RunIdentity(
        run_id=run.workflow_run_id,
        manifest_id=loaded.manifest_id,
        grant_id=grant_id,
        subject=subject,
        workload=workload,
        project_id=record.get("project_id"),
        purpose=purpose,
        decision=decision,
    )


__all__ = [
    "AUTHORITY_DENIED",
    "CODES",
    "GRANT_UNBOUND",
    "IDENTITY_INVALID",
    "MANIFEST_CORRUPT",
    "MANIFEST_MISSING",
    "IdentityDenied",
    "RunIdentity",
    "identity_for_run",
]
