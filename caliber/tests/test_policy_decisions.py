"""A grant never exceeds the role of the person it is issued for (B03.2, #664; the decision itself is B03.3, #671).

The closed action vocabulary and the explicit set of roles each action allows are ``caliber.resource_access.
PROJECT_ACTIONS``. A grant may name only actions the subject's *current* role is in the set for, and every use
rechecks the current role, so a demotion narrows a standing grant at once. No ordering of roles or actions is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject, CaliberProjectMember
from caliber.policy.grants import ROLE_NOT_ALLOWED, GrantError
from caliber.resource_access import (
    OWNER_ROLE_REQUIRED_SCOPES,
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
)
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.test_run_grants import _check, _denied, _issue, _migrated, _seed

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_REVIEWER, ROLE_EDITOR, ROLE_OWNER])
def test_grant_never_exceeds_role(
    dialect: str, role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        if role == ROLE_OWNER:
            run_id = _seed(engine, member=None)
            with Session(engine) as session, session.begin():
                session.get(CaliberProject, "proj-1").owner = "alice"  # type: ignore[union-attr]
            scopes: frozenset[str] | None = OWNER_ROLE_REQUIRED_SCOPES
        else:
            run_id = _seed(engine, role=role)
            scopes = None

        granted: list[tuple[str, str]] = []
        for action, roles in PROJECT_ACTIONS.items():
            if role in roles:
                grant_id = _issue(engine, run_id, allowed_actions=[action], scopes=scopes)
                assert _check(engine, run_id, grant_id, action, scopes=scopes).allowed_actions == {
                    action
                }
                granted.append((action, grant_id))
            else:
                with pytest.raises(GrantError, match="never exceeds the role"):
                    _issue(engine, run_id, allowed_actions=[action], scopes=scopes)
        assert granted, f"the {role} role is allowed some action"

        # One grant naming a permitted and a forbidden action is refused as a whole.
        permitted = next(a for a, r in PROJECT_ACTIONS.items() if role in r)
        forbidden = [a for a, r in PROJECT_ACTIONS.items() if role not in r]
        if forbidden:
            with pytest.raises(GrantError, match="never exceeds the role"):
                _issue(engine, run_id, allowed_actions=[permitted, forbidden[0]], scopes=scopes)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_demotion_narrows_a_standing_grant_at_once(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, role=ROLE_EDITOR)
        editor_only = [
            a
            for a, roles in PROJECT_ACTIONS.items()
            if ROLE_EDITOR in roles and ROLE_VIEWER not in roles
        ]
        shared = [
            a
            for a, roles in PROJECT_ACTIONS.items()
            if ROLE_EDITOR in roles and ROLE_VIEWER in roles
        ]
        assert editor_only and shared
        grant_id = _issue(engine, run_id, allowed_actions=[editor_only[0], shared[0]])
        assert _check(engine, run_id, grant_id, editor_only[0])
        with Session(engine) as session, session.begin():
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = ROLE_VIEWER
        with _denied(ROLE_NOT_ALLOWED):
            _check(engine, run_id, grant_id, editor_only[0])
        assert _check(engine, run_id, grant_id, shared[0]), (
            "an action the viewer role may perform is still allowed"
        )
        with Session(engine) as session, session.begin():  # promoted again: nothing was cached
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = ROLE_EDITOR
        assert _check(engine, run_id, grant_id, editor_only[0])


def test_an_action_nobody_may_be_granted_stays_out_of_the_vocabulary() -> None:
    """``release.break_glass_apply`` is deliberately not in ``PROJECT_ACTIONS`` (no ordinary role grant), so no
    grant can name it."""
    assert "release.break_glass_apply" not in PROJECT_ACTIONS


# ================================================================ B03.3: the typed decision (#671)

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from caliber import contracts
from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
)
from caliber.policy import decisions
from caliber.policy.decisions import (
    EXPLICIT_DENY,
    POLICY_VERSION_MISMATCH,
    PURPOSE_NOT_ALLOWED,
    REASON_CODES,
    REFERENCE_POLICY_V1,
    RISK_ABOVE_CEILING,
    SUBJECT_MISMATCH,
    UNKNOWN_ACTION_RISK,
    WORKLOAD_MISMATCH,
    DecisionInputError,
    Policy,
    decide,
    load_decision,
)
from caliber.policy.grants import (
    ACTION_NOT_ALLOWED,
    AUTHORITY_UNAVAILABLE,
    DENIAL_CODES,
    GRANT_EXPIRED,
    GRANT_NOT_FOUND,
    GRANT_REVOKED,
    NOT_A_MEMBER,
    RESOURCE_NOT_ALLOWED,
    create_grant,
    revoke_grant,
)
from caliber.policy.principal import Workload
from tests.test_run_grants import DIGEST, NOW, _subject, _workload

PURPOSE = "answer a support ticket"
ACTION = "resource.execute"
RESOURCE = "workflow:wf-1"


def _decide(engine: Any, run: str, grant: str, **overrides: Any) -> decisions.PolicyDecision:
    arguments: dict[str, Any] = {
        "run_id": run,
        "grant_id": grant,
        "subject": _subject(),
        "workload": _workload(),
        "action": ACTION,
        "resource": RESOURCE,
        "purpose": PURPOSE,
        "now": NOW,
        **overrides,
    }
    with Session(engine) as session, session.begin():
        return decide(session, **arguments)


def _decisions(engine: Any) -> list[CaliberPolicyDecision]:
    with Session(engine) as session:
        return list(
            session.query(CaliberPolicyDecision).order_by(CaliberPolicyDecision.evaluated_at)
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_allow_is_recorded_with_everything_needed_to_replay_it(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        decision = _decide(engine, run_id, grant_id)
        assert decision.allowed and decision.result == "allow" and decision.reason_codes == ()
        assert decision.policy_version == "policy-v1" and decision.persisted
        (row,) = _decisions(engine)
        assert (row.workflow_run_id, row.grant_id, row.subject_user_id) == (
            run_id,
            grant_id,
            "alice",
        )
        assert (row.workload_id, row.action, row.resource, row.purpose) == (
            _workload().workload_id,
            ACTION,
            RESOURCE,
            PURPOSE,
        )
        assert (row.result, row.reason_codes, row.policy_version) == ("allow", [], "policy-v1")
        assert row.input_hash == decision.input_hash and len(row.input_hash) == 64
        with Session(engine) as session:
            assert load_decision(session, decision.decision_id) == decision.__class__(
                **{
                    **decision.__dict__,
                    "evaluated_at": decision.evaluated_at.astimezone(timezone.utc),
                }
            )
            assert load_decision(session, "pd_unknown") is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_wire_record_is_a_valid_v1_policy_decision_and_extra_fields_are_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        for decision in (
            _decide(engine, run_id, grant_id),
            _decide(engine, run_id, grant_id, action="release.rollback"),
        ):
            record = decision.to_record()
            contracts.validate_record("policy_decision", record)
            assert record["schema_version"] == "v1" and record["evaluated_at"].endswith("Z")
            assert "run_id" not in record, (
                "run linkage is explicit in storage, not in the v1 wire record"
            )
            with pytest.raises(contracts.ContractValidationError):
                contracts.validate_record("policy_decision", {**record, "run_id": decision.run_id})
            with pytest.raises(contracts.UnsupportedSchemaVersion):
                contracts.validate_record("policy_decision", {**record, "schema_version": "v2"})


# ---------------------------------------------------------------- identity: exact, no substitution


@pytest.mark.parametrize("dialect", DIALECTS)
def test_missing_run_or_grant_denies_and_is_recorded(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        for run, grant in (
            ("run-unknown", grant_id),
            (run_id, "grant-unknown"),
            ("run-unknown", "grant-unknown"),
        ):
            decision = _decide(engine, run, grant)
            assert not decision.allowed and decision.reason_codes == (GRANT_NOT_FOUND,)
        assert [r.reason_codes for r in _decisions(engine)] == [[GRANT_NOT_FOUND]] * 3


@pytest.mark.parametrize("dialect", DIALECTS)
def test_cross_run_substitution_denies_with_zero_protected_calls(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_a, run_b = _seed(engine), _seed(engine)
        grant_a, grant_b = _issue(engine, run_a), _issue(engine, run_b)
        protected: list[str] = []

        def guarded(run: str, grant: str) -> None:
            if _decide(engine, run, grant).allowed:
                protected.append(f"{run}/{grant}")

        guarded(run_b, grant_a)  # run B acting under run A's grant
        guarded(run_a, grant_b)
        assert protected == []
        guarded(run_a, grant_a)
        guarded(run_b, grant_b)
        assert protected == [f"{run_a}/{grant_a}", f"{run_b}/{grant_b}"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_wrong_subject_or_workload_denies_with_zero_protected_calls(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        other_workload = Workload.for_workflow(
            "triage", hashlib.sha256(b"another version").hexdigest()
        )
        renamed = Workload.for_workflow("renamed", DIGEST)
        cases = [
            ({"subject": _subject(user_id="mallory")}, SUBJECT_MISMATCH),
            ({"subject": _subject(auth_method="pat")}, SUBJECT_MISMATCH),
            ({"subject": _subject(project_id="proj-other")}, SUBJECT_MISMATCH),
            ({"workload": other_workload}, WORKLOAD_MISMATCH),
            ({"workload": renamed}, WORKLOAD_MISMATCH),
        ]
        for overrides, code in cases:
            decision = _decide(engine, run_id, grant_id, **overrides)
            assert not decision.allowed and decision.reason_codes == (code,), overrides
        assert _decide(engine, run_id, grant_id, subject=_subject(project_id=None)).allowed, (
            "no project context"
        )


# ---------------------------------------------------------------- authority: revoked, expired, membership, role


@pytest.mark.parametrize("dialect", DIALECTS)
def test_revoked_expired_membership_loss_and_demotion_deny(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        assert _decide(engine, run_id, grant_id).allowed
        assert _decide(engine, run_id, grant_id, now=NOW + timedelta(days=2)).reason_codes == (
            GRANT_EXPIRED,
        )
        assert _decide(engine, run_id, grant_id, action="release.rollback").reason_codes == (
            ACTION_NOT_ALLOWED,
        )
        assert _decide(engine, run_id, grant_id, resource="workflow:other").reason_codes == (
            RESOURCE_NOT_ALLOWED,
        )
        with Session(engine) as session, session.begin():
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = "viewer"
        assert _decide(engine, run_id, grant_id).reason_codes == ("role_not_allowed",)
        with Session(engine) as session, session.begin():
            session.delete(session.query(CaliberProjectMember).filter_by(user_id="alice").one())
        assert _decide(engine, run_id, grant_id).reason_codes == (NOT_A_MEMBER,)
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        assert _decide(engine, run_id, grant_id).reason_codes == (GRANT_REVOKED,)
        assert {r.result for r in _decisions(engine)} == {"allow", "deny"}


def test_an_unavailable_store_is_a_deny_and_an_unrecorded_allow_authorizes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("SELECT", {}, Exception("connection lost"))

        with Session(engine) as session:
            monkeypatch.setattr(session, "execute", broken)
            down = decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            )
            session.commit()  # the decision is the caller's to commit (it used to land only by SQLite's autocommit)
        assert not down.allowed and down.reason_codes == (AUTHORITY_UNAVAILABLE,)

        # An allow whose record cannot be written is returned as a deny that was not recorded.
        with Session(engine) as session, session.begin():
            monkeypatch.setattr(
                session,
                "begin_nested",
                lambda: (_ for _ in ()).throw(
                    OperationalError("INSERT", {}, Exception("disk full"))
                ),
            )
            unrecorded = decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            )
        assert (
            unrecorded.result == "deny" and unrecorded.persisted is False and not unrecorded.allowed
        )
        assert unrecorded.reason_codes == (AUTHORITY_UNAVAILABLE,)
        assert [r.result for r in _decisions(engine)] == ["deny"], (
            "only the outage deny; the allow never landed"
        )


# ---------------------------------------------------------------- the policy itself


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_policy_checks_version_purpose_and_explicit_deny(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, allowed_actions=[ACTION, "project.update"])
        v1 = REFERENCE_POLICY_V1
        assert _decide(
            engine, run_id, grant_id, policy=Policy("policy-v2", v1.action_risk, 4)
        ).reason_codes == (POLICY_VERSION_MISMATCH,)
        only = Policy(
            "policy-v1", v1.action_risk, 4, allowed_purposes=frozenset({"approved purpose"})
        )
        assert _decide(engine, run_id, grant_id, policy=only).reason_codes == (PURPOSE_NOT_ALLOWED,)
        assert _decide(engine, run_id, grant_id, policy=only, purpose="approved purpose").allowed
        denied = Policy("policy-v1", v1.action_risk, 4, deny_rules=frozenset({(ACTION, RESOURCE)}))
        assert _decide(engine, run_id, grant_id, policy=denied).reason_codes == (EXPLICIT_DENY,)
        assert _decide(engine, run_id, grant_id, policy=denied, action="project.update").allowed, (
            "only that pair"
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_risk_above_the_ceiling_and_an_action_with_no_risk_deny(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(
            engine,
            run_id,
            allowed_actions=["resource.execute", "release.apply"],
            allowed_resources=[RESOURCE, "release:r1"],
        )
        low = Policy("policy-v1", REFERENCE_POLICY_V1.action_risk, 3)
        assert _decide(engine, run_id, grant_id, policy=low).allowed  # risk 2 <= 3
        assert _decide(
            engine, run_id, grant_id, policy=low, action="release.apply", resource="release:r1"
        ).reason_codes == (RISK_ABOVE_CEILING,)
        none = Policy("policy-v1", {}, 9)
        assert _decide(engine, run_id, grant_id, policy=none).reason_codes == (UNKNOWN_ACTION_RISK,)


def test_every_action_in_the_vocabulary_has_an_explicit_risk_and_project_administration_is_above_the_ceiling() -> (
    None
):
    assert set(REFERENCE_POLICY_V1.action_risk) == set(PROJECT_ACTIONS), "a new action needs a risk"
    assert REFERENCE_POLICY_V1.risk_ceiling == 4
    for action in (
        "project.archive",
        "project.restore",
        "project.manage_members",
        "project.transfer_owner",
    ):
        assert REFERENCE_POLICY_V1.action_risk[action] > REFERENCE_POLICY_V1.risk_ceiling
    assert REFERENCE_POLICY_V1.action_risk["read"] == 0


def test_a_policy_rejects_nonsense() -> None:
    for kwargs in (
        {"version": "", "action_risk": {}, "risk_ceiling": 1},
        {"version": "v" * 65, "action_risk": {}, "risk_ceiling": 1},
        {"version": "v", "action_risk": {"a": -1}, "risk_ceiling": 1},
        {"version": "v", "action_risk": {"a": True}, "risk_ceiling": 1},
        {"version": "v", "action_risk": {}, "risk_ceiling": -1},
        {"version": "v", "action_risk": {}, "risk_ceiling": 1.5},
    ):
        with pytest.raises(ValueError):
            Policy(**kwargs)  # type: ignore[arg-type]
    assert Policy("v", {"a": 1}, 1).digest() == Policy("v", {"a": 1}, 1).digest()
    assert Policy("v", {"a": 1}, 1).digest() != Policy("v", {"a": 2}, 1).digest()


def test_every_reason_code_is_documented() -> None:
    assert (
        DENIAL_CODES
        | {
            "subject_mismatch",
            "workload_mismatch",
            "policy_version_mismatch",
            "purpose_not_allowed",
            "explicit_deny",
            "unknown_action_risk",
            "risk_above_ceiling",
        }
        == REASON_CODES
    )


# ---------------------------------------------------------------- input hash, malformed input


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_input_hash_is_deterministic_and_changes_with_every_input(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, allowed_resources=[RESOURCE, "workflow:wf-2"])
        base = _decide(engine, run_id, grant_id)
        again = _decide(engine, run_id, grant_id)
        assert base.input_hash == again.input_hash, (
            "the same inputs hash the same, whenever they are asked"
        )
        assert base.decision_id != again.decision_id
        later = _decide(engine, run_id, grant_id, now=NOW + timedelta(minutes=1))
        assert later.input_hash == base.input_hash, "the clock is not an input"
        variants = {
            _decide(engine, run_id, grant_id, resource="workflow:wf-2").input_hash,
            _decide(engine, run_id, grant_id, purpose="another purpose").input_hash,
            _decide(engine, run_id, grant_id, subject=_subject(project_id=None)).input_hash,
            _decide(engine, run_id, "grant-other").input_hash,
            _decide(engine, "run-other", grant_id).input_hash,
            _decide(
                engine,
                run_id,
                grant_id,
                policy=Policy("policy-v1", REFERENCE_POLICY_V1.action_risk, 3),
            ).input_hash,
            base.input_hash,
        }
        assert len(variants) == 7


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"grant_id": " g"},
        {"action": "x" * 257},
        {"resource": "bad\nresource"},
        {"purpose": ""},
        {"purpose": "zero\u200bwidth"},
        {"run_id": "r" * 65},
        {"subject": "alice"},
        {"workload": object()},
        {"now": datetime(2026, 1, 1)},
    ],
)
def test_malformed_input_is_a_caller_bug_not_a_recorded_decision(
    overrides: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with pytest.raises(DecisionInputError):
            _decide(engine, run_id, grant_id, **overrides)
        assert _decisions(engine) == []


# ---------------------------------------------------------------- transactions and immutability


@pytest.mark.parametrize("dialect", DIALECTS)
def test_initial_transaction_rollback_leaves_no_run_grant_or_decision(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial admission: the run, the grant and the decision share one uncommitted transaction; the caller commits
    only after an allow, and a denial rolls all of it back."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        _seed(engine)  # the project and the member
        run_id = f"run-a{uuid.uuid4().hex[:8]}"
        grant_id = f"grant-a{uuid.uuid4().hex[:8]}"

        def admit(session: Session, action: str) -> decisions.PolicyDecision:
            session.add(
                CaliberWorkflowRun(
                    workflow_run_id=run_id, workflow_id="wf-1", manifest_legacy=False
                )
            )
            session.flush()
            create_grant(
                session,
                grant_id=grant_id,
                run_id=run_id,
                subject=_subject(),
                workload=_workload(),
                project_id="proj-1",
                allowed_actions=[ACTION],
                allowed_resources=[RESOURCE],
                policy_version="policy-v1",
                expires_at=NOW + timedelta(hours=1),
                now=NOW,
            )
            return decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=action,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            )

        with Session(engine) as session:
            session.begin()
            denied = admit(session, "release.apply")  # not in the grant
            assert not denied.allowed
            session.rollback()  # a denial rolls everything back
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, run_id) is None
            assert session.get(CaliberRunGrant, grant_id) is None
        assert _decisions(engine) == []

        with Session(engine) as session:
            session.begin()
            allowed = admit(session, ACTION)
            assert allowed.allowed
            assert _decisions_in(session) == 1, "visible inside the transaction"
            session.commit()
        assert len(_decisions(engine)) == 1


def _decisions_in(session: Session) -> int:
    return session.query(CaliberPolicyDecision).count()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_deciding_never_mutates_an_admitted_manifest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        raw = json.dumps({"schema_version": "v1", "workflow_run_id": run_id}).encode()
        with Session(engine) as session, session.begin():
            session.add(
                CaliberRunManifest(
                    manifest_id="m-1",
                    workflow_run_id=run_id,
                    schema_version="v1",
                    manifest_bytes=raw,
                    manifest_digest=hashlib.sha256(raw).hexdigest(),
                )
            )
        grant_id = _issue(engine, run_id)
        for _ in range(3):
            _decide(engine, run_id, grant_id)
            _decide(engine, run_id, grant_id, action="release.rollback")
        with Session(engine) as session:
            manifest = session.get(CaliberRunManifest, "m-1")
            assert (
                bytes(manifest.manifest_bytes) == raw
                and session.query(CaliberRunManifest).count() == 1
            )  # type: ignore[union-attr]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_recorded_decision_cannot_be_changed_or_removed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        decision = _decide(engine, run_id, grant_id, action="release.rollback")
        assert not decision.allowed
        with pytest.raises(ImmutableRecordError):
            with Session(engine) as session, session.begin():
                session.get(CaliberPolicyDecision, decision.decision_id).result = "allow"  # type: ignore[union-attr]
        with pytest.raises(ImmutableRecordError):
            with Session(engine) as session, session.begin():
                session.delete(session.get(CaliberPolicyDecision, decision.decision_id))
        for statement in (
            update(CaliberPolicyDecision.__table__).values(result="allow"),
            delete(CaliberPolicyDecision.__table__),
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(statement)
        if dialect == "postgresql":
            with pytest.raises(IntegrityError, match="TRUNCATE refused"):
                with engine.begin() as connection:
                    connection.execute(text("TRUNCATE caliber_policy_decisions"))
        with pytest.raises(
            IntegrityError
        ):  # the database also refuses a result that is neither allow nor deny
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO caliber_policy_decisions (decision_id, workflow_run_id, grant_id, subject_user_id, "
                        "workload_id, action, resource, purpose, result, reason_codes, policy_version, input_hash, "
                        "evaluated_at) VALUES ('d', 'r', 'g', 'u', 'w', 'a', 'x', 'p', 'maybe', '[]', 'v', :h, '2026-01-01')"
                    ),
                    {"h": "0" * 64},
                )
        (stored,) = _decisions(engine)
        assert stored.result == "deny"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_decisions_migration_rolls_back_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from sqlalchemy import inspect

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, config):
        assert "caliber_policy_decisions" in inspect(engine).get_table_names()
        run_id = _seed(engine)
        _decide(engine, run_id, _issue(engine, run_id))
        command.downgrade(config, "0116")
        assert "caliber_policy_decisions" not in inspect(engine).get_table_names()
        if dialect == "postgresql":
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT proname FROM pg_proc WHERE proname LIKE 'caliber_policy_decisions%'"
                        )
                    ).fetchall()
                    == []
                )
        command.upgrade(config, "head")
        assert _decisions(engine) == []


# ---------------------------------------------------------------- review hardening (B03.3)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_risk_equal_to_the_ceiling_is_allowed_and_no_run_may_approve_its_own_work(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.resource_access import OWNER_ROLE_REQUIRED_SCOPES

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, role="owner")
        grant_id = _issue(
            engine,
            run_id,
            allowed_actions=[
                "release.apply",
                "release.approve",
                "resource.approve",
                "change_request.accept",
            ],
            allowed_resources=["release:r1"],
            scopes=OWNER_ROLE_REQUIRED_SCOPES,
        )
        at_ceiling = _decide(
            engine, run_id, grant_id, action="release.apply", resource="release:r1"
        )
        assert at_ceiling.allowed, "risk 4 equals the ceiling 4: allowed"
        for action in ("release.approve", "resource.approve", "change_request.accept"):
            denied = _decide(
                engine,
                run_id,
                grant_id,
                action=action,
                resource="release:r1",
                scopes=OWNER_ROLE_REQUIRED_SCOPES,
            )
            assert not denied.allowed and denied.reason_codes == (RISK_ABOVE_CEILING,), action


@pytest.mark.parametrize("dialect", DIALECTS)
def test_scopes_are_an_input_and_change_the_hash_and_the_answer(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.resource_access import OWNER_ROLE_REQUIRED_SCOPES

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, member=None)
        with Session(engine) as session, session.begin():
            session.get(CaliberProject, "proj-1").owner = "alice"  # type: ignore[union-attr]
        grant_id = _issue(
            engine,
            run_id,
            allowed_actions=["release.rollback"],
            allowed_resources=[RESOURCE],
            scopes=OWNER_ROLE_REQUIRED_SCOPES,
        )
        kwargs = {"action": "release.rollback"}
        without = _decide(engine, run_id, grant_id, **kwargs)
        wrong = _decide(engine, run_id, grant_id, scopes=frozenset({"caliber.operator"}), **kwargs)
        right = _decide(engine, run_id, grant_id, scopes=OWNER_ROLE_REQUIRED_SCOPES, **kwargs)
        assert without.reason_codes == wrong.reason_codes == ("role_not_allowed",)
        assert right.allowed
        assert len({without.input_hash, wrong.input_hash, right.input_hash}) == 3
        assert (
            _decide(
                engine, run_id, grant_id, scopes=OWNER_ROLE_REQUIRED_SCOPES, **kwargs
            ).input_hash
            == right.input_hash
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_input_and_every_policy_field_changes_the_hash(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id, allowed_resources=[RESOURCE])
        base = _decide(engine, run_id, grant_id).input_hash
        other_workload = Workload.for_workflow("triage", hashlib.sha256(b"x").hexdigest())
        hashes = {
            "action": _decide(engine, run_id, grant_id, action="release.apply").input_hash,
            "workload": _decide(engine, run_id, grant_id, workload=other_workload).input_hash,
            "user": _decide(
                engine, run_id, grant_id, subject=_subject(user_id="mallory")
            ).input_hash,
            "auth": _decide(
                engine, run_id, grant_id, subject=_subject(auth_method="pat")
            ).input_hash,
        }
        assert base not in hashes.values() and len(set(hashes.values())) == 4
    v1 = REFERENCE_POLICY_V1
    digests = {
        v1.digest(),
        Policy("policy-v1", v1.action_risk, 3).digest(),
        Policy("policy-v2", v1.action_risk, 4).digest(),
        Policy("policy-v1", {**v1.action_risk, "read": 1}, 4).digest(),
        Policy("policy-v1", v1.action_risk, 4, deny_rules=frozenset({("a", "b")})).digest(),
        Policy("policy-v1", v1.action_risk, 4, allowed_purposes=frozenset({"p"})).digest(),
        Policy("policy-v1", v1.action_risk, 4, allowed_purposes=frozenset()).digest(),
    }
    assert len(digests) == 7


def test_a_policy_is_read_only_and_type_checked() -> None:
    policy = Policy("v", {"a": 1}, 1)
    with pytest.raises(TypeError):
        policy.action_risk["b"] = 2  # type: ignore[index]
    source = {"a": 1}
    copy = Policy("v", source, 1)
    source["a"] = 9
    assert copy.action_risk["a"] == 1, "a later change to the caller's dict cannot change a policy"
    with pytest.raises(TypeError):
        REFERENCE_POLICY_V1.action_risk["read"] = 5  # type: ignore[index]
    for kwargs in (
        {"deny_rules": {("a", "b")}},
        {"deny_rules": frozenset({("a",)})},
        {"deny_rules": frozenset({("a", 1)})},
        {"allowed_purposes": "purpose"},
        {"allowed_purposes": frozenset({1})},
        {"action_risk": {1: 1}},
    ):
        with pytest.raises(ValueError):
            Policy("v", **{"action_risk": {"a": 1}, "risk_ceiling": 1, **kwargs})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Policy(5, {"a": 1}, 1)  # type: ignore[arg-type]


def test_an_oversized_user_id_is_a_caller_bug_not_lost_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with pytest.raises(DecisionInputError, match="subject.user_id"):
            _decide(engine, run_id, grant_id, subject=_subject(user_id="u" * 300))
        assert _decisions(engine) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_identity_is_checked_before_authority_and_a_non_utc_clock_is_the_same_instant(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session, session.begin():
            revoke_grant(session, run_id=run_id, grant_id=grant_id, by="@o", reason="r", now=NOW)
        wrong = Workload.for_workflow("triage", hashlib.sha256(b"x").hexdigest())
        assert _decide(engine, run_id, grant_id, workload=wrong).reason_codes == (
            WORKLOAD_MISMATCH,
        )
        assert _decide(
            engine, run_id, grant_id, subject=_subject(user_id="mallory")
        ).reason_codes == (SUBJECT_MISMATCH,)
        zone = timezone(timedelta(hours=2))
        local = _decide(engine, run_id, grant_id, now=NOW.astimezone(zone))
        assert local.evaluated_at == NOW and local.reason_codes == (GRANT_REVOKED,)
        stored = next(r for r in _decisions(engine) if r.decision_id == local.decision_id)
        assert stored.evaluated_at == NOW.replace(tzinfo=None)
        with Session(engine) as session:
            assert load_decision(session, local.decision_id).evaluated_at == NOW  # type: ignore[union-attr]
            assert load_decision(session, local.decision_id).subject_user_id == "alice"  # type: ignore[union-attr]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_real_failure_while_recording_keeps_the_transaction_usable_and_the_reason(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trigger that refuses the INSERT (a real database failure, not a mock): the savepoint rolls back, the caller's
    transaction commits its own writes, and the answer is a deny that keeps its original reason, unrecorded."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with engine.begin() as connection:
            if dialect == "postgresql":
                connection.execute(
                    text(
                        "CREATE FUNCTION refuse_decisions() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        "BEGIN RAISE EXCEPTION 'disk full'; END $$"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TRIGGER refuse BEFORE INSERT ON caliber_policy_decisions "
                        "FOR EACH ROW EXECUTE FUNCTION refuse_decisions()"
                    )
                )
            else:
                connection.execute(
                    text(
                        "CREATE TRIGGER refuse BEFORE INSERT ON caliber_policy_decisions "
                        "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
                    )
                )
        with Session(engine) as session:
            session.begin()
            session.add(CaliberWorkflowRun(workflow_run_id="run-side", workflow_id="wf-1"))
            session.flush()
            allowed = decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            )
            denied = decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action="release.rollback",
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            )
            session.commit()  # the caller's own write survives the failed records
        assert (allowed.result, allowed.persisted, allowed.reason_codes) == (
            "deny",
            False,
            (AUTHORITY_UNAVAILABLE,),
        )
        assert not allowed.allowed
        assert (
            denied.reason_codes == (ACTION_NOT_ALLOWED, AUTHORITY_UNAVAILABLE)
            and denied.persisted is False
        )
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, "run-side") is not None
            assert session.query(CaliberPolicyDecision).count() == 0


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="savepoint semantics differ on pysqlite")
def test_postgresql_decide_as_the_first_write_still_rolls_back_with_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On pysqlite a SAVEPOINT that is the first statement of a transaction commits it (development only: the
    driver begins no transaction before the first write); PostgreSQL rolls the decision back with the caller."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session:
            session.begin()
            assert decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            ).allowed
            session.rollback()
        assert _decisions(engine) == []


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_a_revocation_racing_a_decision_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        outcome: dict[str, Any] = {}

        def revoker() -> None:
            try:
                with Session(engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '30s'"))
                    revoke_grant(
                        session, run_id=run_id, grant_id=grant_id, by="@o", reason="race", now=NOW
                    )
                outcome["committed_at"] = time.monotonic()
            except BaseException as exc:
                outcome["error"] = exc

        session = Session(engine)
        session.begin()
        try:
            assert decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            ).allowed
            thread = threading.Thread(target=revoker, daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with engine.connect() as connection:
                    if connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock'"
                        )
                    ).scalar_one():
                        break
                time.sleep(0.05)
            assert thread.is_alive(), "the revocation must wait for the deciding transaction"
            decided_at = time.monotonic()
            session.commit()
        finally:
            session.rollback()
            session.close()
        thread.join(30)
        assert "error" not in outcome, outcome.get("error")
        assert outcome["committed_at"] > decided_at
        assert _decide(engine, run_id, grant_id).reason_codes == (GRANT_REVOKED,)


# ---------------------------------------------------------------- the audit row of a decision (B03.5, #688)


def _audit_rows(engine: Any) -> list[Any]:
    from caliber.db.models import CaliberAuditLog

    with Session(engine) as session:
        return list(
            session.query(CaliberAuditLog)
            .filter(CaliberAuditLog.entity_type == "policy_decision")
            .order_by(CaliberAuditLog.log_id)
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_recorded_decision_has_one_structured_audit_row(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        allowed = _decide(engine, run_id, grant_id)
        denied = _decide(engine, run_id, grant_id, action="release.approve")
        allow_row, deny_row = _audit_rows(engine)
        assert (allow_row.action, allow_row.severity, allow_row.entity_id) == (
            "policy_allow",
            "standard",
            allowed.decision_id,
        )
        assert (deny_row.action, deny_row.severity, deny_row.entity_id) == (
            "policy_deny",
            "high",
            denied.decision_id,
        )
        assert allow_row.actor == deny_row.actor == "alice"
        assert deny_row.details == {
            "run_id": run_id,
            "grant_id": grant_id,
            "workload_id": _workload().workload_id,
            "action": "release.approve",
            "resource": RESOURCE,
            "purpose": PURPOSE,
            "result": "deny",
            "reason_codes": list(denied.reason_codes),
            "policy_version": "policy-v1",
            "input_hash": denied.input_hash,
        }


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_decision_that_cannot_be_audited_is_a_denial_logged_with_its_ids(
    dialect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy outage case: the decision and its audit row are written together or not at all; when they cannot be,
    nothing is allowed, neither row exists, and a structured log line names the decision and run."""
    from sqlalchemy.exc import OperationalError

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)

        def down(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("INSERT", {}, Exception("audit store down"))

        logged: list[dict[str, Any]] = []
        monkeypatch.setattr(decisions, "audit_record", down)
        monkeypatch.setattr(
            decisions.logger,
            "error",
            lambda message, *, extra: logged.append({"message": message, **extra}),
        )
        outcome = _decide(engine, run_id, grant_id)
        assert not outcome.allowed and outcome.persisted is False
        assert outcome.reason_codes == (AUTHORITY_UNAVAILABLE,)
        assert _decisions(engine) == [] and _audit_rows(engine) == []
        (record,) = logged
        assert record["decision_id"] == outcome.decision_id and record["run_id"] == run_id
        assert record["grant_id"] == grant_id and record["result"] == "allow", (
            "what was being decided"
        )
        assert PURPOSE not in str(record), "identifiers and reasons only"


def test_a_decision_and_its_audit_row_roll_back_with_the_caller_even_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On SQLite a savepoint that is the first statement would commit the rows on the spot; they must not."""
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine)
        grant_id = _issue(engine, run_id)
        with Session(engine) as session:
            assert decide(
                session,
                run_id=run_id,
                grant_id=grant_id,
                subject=_subject(),
                workload=_workload(),
                action=ACTION,
                resource=RESOURCE,
                purpose=PURPOSE,
                now=NOW,
            ).allowed
            session.rollback()
        assert _decisions(engine) == [] and _audit_rows(engine) == []


@pytest.mark.parametrize(
    ("reasons", "severity"),
    [
        (("grant_expired",), "standard"),
        (("grant_revoked",), "standard"),
        (("not_a_member",), "standard"),
        (("action_not_allowed",), "high"),
        (("subject_mismatch",), "high"),
        (("explicit_deny",), "high"),
        (("grant_revoked", "explicit_deny"), "high"),
    ],
)
def test_ordinary_lifecycle_denials_are_standard_and_anything_else_is_high(
    reasons: tuple[str, ...], severity: str
) -> None:
    from caliber.policy.decisions import PolicyDecision

    decision = PolicyDecision(
        decision_id="pd_x",
        run_id="r",
        grant_id="g",
        action="a",
        resource="r",
        purpose="p",
        result="deny",
        reason_codes=reasons,
        policy_version="v",
        input_hash="h",
        evaluated_at=NOW,
    )
    assert ("high" if decisions._is_alarming(decision) else "standard") == severity
