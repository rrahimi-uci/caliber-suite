from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberAuditLog, CaliberReleaseOperation
from caliber.release_operations import (
    PreparedReleaseResolutionError,
    ReleaseMutationNotStartedError,
    ReleaseOperationConflictError,
    abandon_prepared_prompt_release,
    execute_prompt_alias_release,
    prepare_prompt_alias_release,
    reconcile_prompt_alias_releases,
)


def test_prepared_release_can_be_abandoned_without_provider_ambiguity(
    db_session: Session,
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-abandon",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )

    resolved = abandon_prepared_prompt_release(
        db_session,
        operation_id=operation.operation_id,
        actor="@operator",
        reason="deployment window closed",
    )

    assert resolved.status == "failed"
    assert resolved.active_lock is None
    assert "deployment window closed" in (resolved.last_error or "")
    audit = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "abandon_prepared_prompt_release")
        .one()
    )
    assert audit.details["provider_call_started"] is False


def test_non_prepared_release_cannot_be_abandoned(db_session: Session) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-applying",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )
    operation.status = "applying"
    db_session.commit()

    with pytest.raises(PreparedReleaseResolutionError, match="not a prepared"):
        abandon_prepared_prompt_release(
            db_session,
            operation_id=operation.operation_id,
            actor="@operator",
            reason="unsafe",
        )


def test_prompt_release_intent_is_committed_before_provider_effect(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
        operation_id="REL-test-commit-order",
        effective_scopes=("operator",),
        evidence={"gate_state": "pass", "eval_run_id": "EVR-1"},
    )

    observed: dict[str, object] = {}

    def mutate_alias(**kwargs: object) -> dict[str, object]:
        with session_factory() as observer:
            durable = observer.get(CaliberReleaseOperation, operation.operation_id)
            assert durable is not None
            observed["status_before_effect"] = durable.status
            observed["from_version"] = durable.version_before
            observed["to_version"] = durable.version_after
        return dict(kwargs)

    result = execute_prompt_alias_release(
        db_session,
        operation,
        mutate_alias=mutate_alias,
    )

    assert observed == {
        "status_before_effect": "applying",
        "from_version": 4,
        "to_version": 5,
    }
    assert result["version"] == 5
    db_session.expire_all()
    settled = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert settled is not None and settled.status == "applied"
    assert settled.provider_result == {
        "name": "support-agent",
        "alias": "prod",
        "version": 5,
    }
    actions = {
        row.action
        for row in db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_id == "support-agent")
        .all()
    }
    assert {"prepare_prompt_release", "promote_prompt"} <= actions


def test_provider_error_leaves_reconciliation_obligation(db_session: Session) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
    )

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider timeout after request")

    with pytest.raises(RuntimeError, match="provider timeout"):
        execute_prompt_alias_release(db_session, operation, mutate_alias=fail)

    db_session.expire_all()
    row = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert row is not None
    assert row.status == "reconcile_required"
    assert "provider timeout" in (row.last_error or "")


def test_preflight_failure_clears_lock_without_reconciliation(db_session: Session) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-preflight",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )
    cause = RuntimeError("provider API unavailable")

    def fail_before_call(**_kwargs: object) -> dict[str, object]:
        raise ReleaseMutationNotStartedError(cause)

    with pytest.raises(RuntimeError, match="provider API unavailable"):
        execute_prompt_alias_release(db_session, operation, mutate_alias=fail_before_call)

    db_session.expire_all()
    row = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert row is not None
    assert row.status == "failed"
    assert row.active_lock is None


def test_reconciler_settles_observed_target_and_flags_unknown_state(db_session: Session) -> None:
    applied = prepare_prompt_alias_release(
        db_session,
        name="p-applied",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )
    applied.status = "applying"
    unknown = prepare_prompt_alias_release(
        db_session,
        name="p-unknown",
        alias="prod",
        version_before=3,
        version_after=4,
        actor="@operator",
    )
    unknown.status = "applying"
    db_session.commit()

    def resolve(name: str, alias: str) -> dict[str, object]:
        assert alias == "prod"
        return {"version": 2 if name == "p-applied" else 99}

    reconcile_prompt_alias_releases(db_session, resolve_alias=resolve)
    db_session.expire_all()
    assert db_session.get(CaliberReleaseOperation, applied.operation_id).status == "applied"  # type: ignore[union-attr]
    unresolved = db_session.get(CaliberReleaseOperation, unknown.operation_id)
    assert unresolved is not None and unresolved.status == "reconcile_required"
    assert "unexpected version 99" in (unresolved.last_error or "")


def test_reconciler_does_not_treat_unknown_cold_start_as_absent_alias(
    db_session: Session,
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-cold-start",
        alias="prod",
        version_before=None,
        version_after=1,
        actor="@operator",
    )
    operation.status = "applying"
    db_session.commit()

    reconcile_prompt_alias_releases(
        db_session,
        resolve_alias=lambda _name, _alias: None,
    )
    db_session.expire_all()
    unresolved = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert unresolved is not None
    assert unresolved.status == "reconcile_required"
    assert unresolved.active_lock == "prompt:p-cold-start:prod"


def test_release_operation_id_is_idempotent_but_not_retargetable(db_session: Session) -> None:
    first = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
        operation_id="REL-idempotent",
    )
    same = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
        operation_id="REL-idempotent",
    )
    assert same.operation_id == first.operation_id

    with pytest.raises(ReleaseOperationConflictError):
        prepare_prompt_alias_release(
            db_session,
            name="support-agent",
            alias="prod",
            version_before=4,
            version_after=6,
            actor="@operator",
            operation_id="REL-idempotent",
        )


def test_incomplete_release_serializes_same_alias_across_operation_ids(
    db_session: Session,
) -> None:
    first = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
        operation_id="REL-first",
    )
    with pytest.raises(ReleaseOperationConflictError, match="already owns"):
        prepare_prompt_alias_release(
            db_session,
            name="support-agent",
            alias="prod",
            version_before=4,
            version_after=6,
            actor="@operator",
            operation_id="REL-second",
        )

    execute_prompt_alias_release(
        db_session,
        first,
        mutate_alias=lambda **kwargs: dict(kwargs),
    )
    second = prepare_prompt_alias_release(
        db_session,
        name="support-agent",
        alias="prod",
        version_before=5,
        version_after=6,
        actor="@operator",
        operation_id="REL-second",
    )
    assert second.status == "prepared"
