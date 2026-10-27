"""The typed policy decision: may this run do this action on this resource, for this purpose, right now? (B03.3, #671)

:func:`decide` answers with a :class:`PolicyDecision` and records it, allow or deny, in
``caliber_policy_decisions`` (immutable, ADR 0003 ``policy_decision`` v1 plus explicit run linkage).

* **Exact identity, no fallback.** The caller supplies the run id, the grant id, the subject and the workload. The
  grant is found by exactly those ids (:func:`caliber.policy.grants.load_grant`); a different run, an unknown grant,
  another subject or another workload each deny with their own reason code and nothing is looked up by recency.
* **One reason, a fixed order.** The first failing check names the decision: ``grant_not_found``,
  ``subject_mismatch``, ``workload_mismatch``, ``grant_revoked``, ``grant_expired``, ``not_a_member``,
  ``action_not_allowed``, ``role_not_allowed``, ``resource_not_allowed`` (the grant checks of
  :func:`caliber.policy.grants.authorize`, with its current-membership and current-role rechecks), then the policy's
  own: ``policy_version_mismatch``, ``purpose_not_allowed``, ``explicit_deny``, ``unknown_action_risk`` and
  ``risk_above_ceiling``. An unavailable store is ``authority_unavailable``. Allow carries no reason codes.
* **Explicit maps, never ordering.** Risk is an explicit number per action in :class:`Policy.action_risk` (every
  action in ``resource_access.PROJECT_ACTIONS`` must have one; a test enforces it), compared with the policy's ceiling;
  explicit denies are exact ``(action, resource)`` pairs. Nothing ranks actions by name.
* **Replayable.** ``input_hash`` is the SHA-256 of the canonical JSON of every input plus the policy's own digest, so
  the same inputs under the same policy hash identically.
* **Audited.** Each recorded decision writes one audit row through :func:`caliber.audit.record` in the decision's own
  savepoint (action ``policy_allow`` / ``policy_deny``, severity ``standard`` / ``high``, actor the subject, entity
  the decision id; identifiers, reasons and the input hash only, never the run's input or any context), so the row
  exists exactly when the decision does. When neither can be written the decision is returned as an unrecorded deny
  and a structured error log line names the decision, run, grant and what was being decided.
* **Transactions.** The decision is flushed, never committed: for initial admission the run, the grant and the
  decision live in one uncommitted transaction and the caller commits only after an allow. A decision never touches
  the run manifest. If recording an *allow* fails the answer is a deny (``authority_unavailable``) that was not
  recorded (``persisted`` is False): nothing is authorized that is not on record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.contracts import validate_record
from caliber.db.models import CaliberPolicyDecision
from caliber.policy import grants
from caliber.policy.grants import GrantDenied
from caliber.policy.principal import Subject, Workload

logger = logging.getLogger("caliber.policy.decisions")

ALLOW: Final = "allow"
DENY: Final = "deny"

SUBJECT_MISMATCH: Final = "subject_mismatch"
WORKLOAD_MISMATCH: Final = "workload_mismatch"
POLICY_VERSION_MISMATCH: Final = "policy_version_mismatch"
PURPOSE_NOT_ALLOWED: Final = "purpose_not_allowed"
EXPLICIT_DENY: Final = "explicit_deny"
UNKNOWN_ACTION_RISK: Final = "unknown_action_risk"
RISK_ABOVE_CEILING: Final = "risk_above_ceiling"
#: Every reason a decision can carry (the grant reasons are :data:`caliber.policy.grants.DENIAL_CODES`).
REASON_CODES: Final = frozenset(
    {
        *grants.DENIAL_CODES,
        SUBJECT_MISMATCH,
        WORKLOAD_MISMATCH,
        POLICY_VERSION_MISMATCH,
        PURPOSE_NOT_ALLOWED,
        EXPLICIT_DENY,
        UNKNOWN_ACTION_RISK,
        RISK_ABOVE_CEILING,
    }
)

_MAX = 256
_MAX_ID = 64
_PAIR = 2


class DecisionInputError(ValueError):
    """An input that is malformed (blank, oversized, control characters): a bug in the caller, not a denial."""


@dataclass(frozen=True)
class Policy:
    """A policy version: explicit risk per action, a ceiling, explicit denies and the allowed purposes."""

    version: str
    action_risk: Mapping[str, int]
    risk_ceiling: int
    deny_rules: frozenset[tuple[str, str]] = frozenset()
    allowed_purposes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version or len(self.version) > _MAX_ID:
            raise ValueError("a policy needs a version of at most 64 characters")
        if any(type(action) is not str for action in self.action_risk) or any(
            type(risk) is not int or risk < 0 for risk in self.action_risk.values()
        ):
            raise ValueError(
                "every action risk must be a non-negative integer keyed by an action name"
            )
        if not isinstance(self.deny_rules, frozenset) or any(
            type(rule) is not tuple
            or len(rule) != _PAIR
            or not all(type(part) is str for part in rule)
            for rule in self.deny_rules
        ):
            raise ValueError("deny_rules must be a frozenset of (action, resource) string pairs")
        if self.allowed_purposes is not None and (
            not isinstance(self.allowed_purposes, frozenset)
            or any(type(purpose) is not str for purpose in self.allowed_purposes)
        ):
            raise ValueError("allowed_purposes must be None or a frozenset of strings")
        # A read-only copy, so a policy cannot be changed after it has been digested or used.
        object.__setattr__(self, "action_risk", MappingProxyType(dict(self.action_risk)))
        if type(self.risk_ceiling) is not int or self.risk_ceiling < 0:
            raise ValueError("the risk ceiling must be a non-negative integer")

    def digest(self) -> str:
        """SHA-256 of the policy's canonical JSON: part of every decision's ``input_hash``."""
        return _sha256(
            {
                "version": self.version,
                "action_risk": dict(sorted(self.action_risk.items())),
                "risk_ceiling": self.risk_ceiling,
                "deny_rules": sorted([action, resource] for action, resource in self.deny_rules),
                "allowed_purposes": None
                if self.allowed_purposes is None
                else sorted(self.allowed_purposes),
            }
        )


#: Explicit risk per action, 0 (read) to 5. Risk 5 is "a person, never a run": project administration (archive,
#: restore, members, ownership transfer) and every approval or acceptance (``resource.approve``, ``release.approve``,
#: ``release.quality_signoff``, ``change_request.accept``). Every ``PROJECT_ACTIONS`` key has one.
_ACTION_RISK: Final[dict[str, int]] = {
    "read": 0,
    "feedback.submit": 1,
    "change_request.comment": 1,
    "release.evaluate": 1,
    "resource.write.evidence": 2,
    "resource.write.runtime": 2,
    "resource.execute": 2,
    "rework.update": 2,
    "trace.payload.read": 2,
    "revision.import": 2,
    "revision.create": 2,
    "change_request.create": 2,
    "change_request.update": 2,
    "change_request.review": 2,
    "project.update": 3,
    "resource.publish": 3,
    "release.request": 3,
    "release.quality_signoff": 5,
    "resource.approve": 5,
    "release.apply": 4,
    "release.approve": 5,
    "release.rollback": 4,
    "release.reconcile": 4,
    "change_request.manage": 4,
    "change_request.accept": 5,
    "source.manage": 4,
    "environment.manage": 4,
    "project.archive": 5,
    "project.restore": 5,
    "project.manage_members": 5,
    "project.transfer_owner": 5,
}

#: The reference policy: a run's grant may authorize actions up to risk 4. Risk 5 (project administration and the
#: human approvals and acceptances) is never something a run does: Beta keeps a human accountable owner and human
#: approval of protected actions (ADR 0004), so no grant can make a run approve its own work.
REFERENCE_POLICY_V1: Final = Policy(version="policy-v1", action_risk=_ACTION_RISK, risk_ceiling=4)


@dataclass(frozen=True)
class PolicyDecision:
    """The answer, as recorded."""

    decision_id: str
    run_id: str
    grant_id: str
    action: str
    resource: str
    purpose: str
    result: str
    reason_codes: tuple[str, ...]
    policy_version: str
    input_hash: str
    evaluated_at: datetime
    persisted: bool = True
    subject_user_id: str = ""
    workload_id: str = ""

    @property
    def allowed(self) -> bool:
        """True only for a recorded allow: an unrecorded decision never authorizes anything."""
        return self.result == ALLOW and self.persisted

    def to_record(self) -> dict[str, Any]:
        """The ``policy_decision`` v1 wire record (no run linkage: that is explicit in storage)."""
        return {
            "schema_version": "v1",
            "decision_id": self.decision_id,
            "grant_id": self.grant_id,
            "action": self.action,
            "resource": self.resource,
            "result": self.result,
            "reason_codes": list(self.reason_codes),
            "policy_version": self.policy_version,
            "input_hash": self.input_hash,
            "evaluated_at": self.evaluated_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
        }


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _clean(value: object, name: str, limit: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > limit:
        raise DecisionInputError(
            f"{name} must be a non-empty string of at most {limit} characters, unpadded"
        )
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    ):
        raise DecisionInputError(f"{name} must not contain control or format characters")
    return value


def decide(
    session: Session,
    *,
    run_id: str,
    grant_id: str,
    subject: Subject,
    workload: Workload,
    action: str,
    resource: str,
    purpose: str,
    policy: Policy = REFERENCE_POLICY_V1,
    scopes: frozenset[str] | None = None,
    now: datetime | None = None,
) -> PolicyDecision:
    """Decide and record. Flushes, never commits; see the module docstring for every rule and the reason order."""
    if type(subject) is not Subject or type(workload) is not Workload:
        raise DecisionInputError("subject and workload must be a Subject and a Workload")
    _clean(subject.user_id, "subject.user_id", _MAX)
    ids = (_clean(run_id, "run_id", _MAX_ID), _clean(grant_id, "grant_id", _MAX_ID))
    asked = (
        _clean(action, "action", _MAX),
        _clean(resource, "resource", _MAX),
        _clean(purpose, "purpose", _MAX),
    )
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise DecisionInputError("now must be timezone-aware")
    input_hash = _sha256(
        {
            "run_id": ids[0],
            "grant_id": ids[1],
            "subject": subject.to_manifest(),
            "subject_project": subject.project_id,
            "workload": workload.to_manifest(),
            "action": asked[0],
            "resource": asked[1],
            "purpose": asked[2],
            "scopes": None if scopes is None else sorted(scopes),
            "policy": policy.digest(),
        }
    )
    reason = _evaluate(session, ids, subject, workload, asked, policy, scopes, moment)
    decision = _build(ids, asked, policy, input_hash, moment, reason, subject, workload)
    return _record(session, decision)


def _evaluate(  # noqa: PLR0911 - one return per reason reads clearer than nesting
    session: Session,
    ids: tuple[str, str],
    subject: Subject,
    workload: Workload,
    asked: tuple[str, str, str],
    policy: Policy,
    scopes: frozenset[str] | None,
    moment: datetime,
) -> str | None:
    """The first reason this is denied, or ``None`` for an allow."""
    run_id, grant_id = ids
    action, resource, purpose = asked
    try:
        grant = grants.load_grant(session, run_id=run_id, grant_id=grant_id)
        if (subject.user_id, subject.auth_method) != (
            grant.subject.user_id,
            grant.subject.auth_method,
        ) or (subject.project_id is not None and subject.project_id != grant.project_id):
            return SUBJECT_MISMATCH
        if workload != grant.workload:
            return WORKLOAD_MISMATCH
        grant = grants.authorize(
            session,
            run_id=run_id,
            grant_id=grant_id,
            action=action,
            resource=resource,
            now=moment,
            scopes=scopes,
        )
    except GrantDenied as denied:
        return denied.code  # every database failure inside grants is already authority_unavailable
    if grant.policy_version != policy.version:
        return POLICY_VERSION_MISMATCH
    if policy.allowed_purposes is not None and purpose not in policy.allowed_purposes:
        return PURPOSE_NOT_ALLOWED
    if (action, resource) in policy.deny_rules:
        return EXPLICIT_DENY
    risk = policy.action_risk.get(action)
    if risk is None:
        return UNKNOWN_ACTION_RISK
    if risk > policy.risk_ceiling:
        return RISK_ABOVE_CEILING
    return None


def _build(
    ids: tuple[str, str],
    asked: tuple[str, str, str],
    policy: Policy,
    input_hash: str,
    moment: datetime,
    reason: str | None,
    subject: Subject,
    workload: Workload,
) -> PolicyDecision:
    return PolicyDecision(
        decision_id=f"pd_{uuid.uuid4().hex}",
        run_id=ids[0],
        grant_id=ids[1],
        action=asked[0],
        resource=asked[1],
        purpose=asked[2],
        result=ALLOW if reason is None else DENY,
        reason_codes=() if reason is None else (reason,),
        policy_version=policy.version,
        input_hash=input_hash,
        evaluated_at=moment,
        subject_user_id=subject.user_id,
        workload_id=workload.workload_id,
    )


def _record(session: Session, decision: PolicyDecision) -> PolicyDecision:
    """Validate against the contract and persist; an allow that cannot be recorded is returned as a deny."""
    validate_record("policy_decision", decision.to_record())
    row = CaliberPolicyDecision(
        decision_id=decision.decision_id,
        workflow_run_id=decision.run_id,
        grant_id=decision.grant_id,
        subject_user_id=decision.subject_user_id,
        workload_id=decision.workload_id,
        action=decision.action,
        resource=decision.resource,
        purpose=decision.purpose,
        result=decision.result,
        reason_codes=list(decision.reason_codes),
        policy_version=decision.policy_version,
        input_hash=decision.input_hash,
        evaluated_at=decision.evaluated_at.astimezone(timezone.utc).replace(tzinfo=None),
    )
    try:
        with session.begin_nested():
            session.add(row)
            _audit(session, decision)
    except SQLAlchemyError:
        # The decision could not be recorded, so it is not an allow. The store being down is the likely reason, so
        # the audit row cannot be written either: the structured log line is the record of the outage.
        logger.error(
            "policy decision could not be recorded",
            extra={
                "decision_id": decision.decision_id,
                "run_id": decision.run_id,
                "grant_id": decision.grant_id,
                "action": decision.action,
                "result": decision.result,
                "reason_codes": list(decision.reason_codes),
            },
        )
        return PolicyDecision(
            **{
                **decision.__dict__,
                "result": DENY,
                "reason_codes": (*decision.reason_codes, grants.AUTHORITY_UNAVAILABLE),
                "persisted": False,
            }
        )
    return decision


def _audit(session: Session, decision: PolicyDecision) -> None:
    """One audit row per recorded decision, in the decision's own savepoint: it exists exactly when the decision does.

    Identifiers, the reasons and the policy's input hash only: never the run's input, context or any secret (and the
    audit log's redactor still runs over it).
    """
    audit_record(
        session,
        actor=decision.subject_user_id,
        action=f"policy_{decision.result}",
        entity_type="policy_decision",
        entity_id=decision.decision_id,
        severity="standard" if decision.result == ALLOW else "high",
        details={
            "run_id": decision.run_id,
            "grant_id": decision.grant_id,
            "workload_id": decision.workload_id,
            "action": decision.action,
            "resource": decision.resource,
            "purpose": decision.purpose,
            "result": decision.result,
            "reason_codes": list(decision.reason_codes),
            "policy_version": decision.policy_version,
            "input_hash": decision.input_hash,
        },
    )


def load_decision(session: Session, decision_id: str) -> PolicyDecision | None:
    """Read a recorded decision back, validating its wire form against the contract."""
    row = session.execute(
        select(CaliberPolicyDecision).where(CaliberPolicyDecision.decision_id == decision_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    decision = PolicyDecision(
        decision_id=row.decision_id,
        run_id=row.workflow_run_id,
        grant_id=row.grant_id,
        action=row.action,
        resource=row.resource,
        purpose=row.purpose,
        result=row.result,
        reason_codes=tuple(row.reason_codes),
        policy_version=row.policy_version,
        input_hash=row.input_hash,
        evaluated_at=row.evaluated_at.replace(tzinfo=timezone.utc),
        subject_user_id=row.subject_user_id,
        workload_id=row.workload_id,
    )
    validate_record("policy_decision", decision.to_record())
    return decision


__all__ = [
    "ALLOW",
    "DENY",
    "EXPLICIT_DENY",
    "POLICY_VERSION_MISMATCH",
    "PURPOSE_NOT_ALLOWED",
    "REASON_CODES",
    "REFERENCE_POLICY_V1",
    "RISK_ABOVE_CEILING",
    "SUBJECT_MISMATCH",
    "UNKNOWN_ACTION_RISK",
    "WORKLOAD_MISMATCH",
    "DecisionInputError",
    "Policy",
    "PolicyDecision",
    "decide",
    "load_decision",
]
