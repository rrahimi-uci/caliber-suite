from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberReleaseOperation,
    CaliberVerificationItem,
)
from caliber.release_operations import (
    PreparedReleaseResolutionError,
    ReleaseMutationNotStartedError,
    ReleaseOperationConflictError,
    abandon_prepared_prompt_release,
    execute_prompt_alias_release,
    prepare_prompt_alias_release,
    reconcile_prompt_alias_releases,
    serialize_release_operation,
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


@pytest.mark.parametrize("reason", ["", "   "])
def test_abandon_prepared_release_requires_a_non_empty_reason(
    db_session: Session, reason: str
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-empty-reason",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )

    with pytest.raises(PreparedReleaseResolutionError, match="non-empty reason"):
        abandon_prepared_prompt_release(
            db_session,
            operation_id=operation.operation_id,
            actor="@operator",
            reason=reason,
        )


def test_abandon_prepared_release_reports_a_missing_operation(db_session: Session) -> None:
    with pytest.raises(PreparedReleaseResolutionError, match="not found"):
        abandon_prepared_prompt_release(
            db_session,
            operation_id="REL-missing",
            actor="@operator",
            reason="operator cancelled it",
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


def test_prepare_derives_project_id_from_the_prompts_hidden_target(
    db_session: Session,
) -> None:
    """`P2-R`: a project-scoped hidden prompt target (`P2-G`'s
    ``CaliberAgentConfig`` row, keyed by ``agent_id == prompt_name``) supplies
    the new operation's ``project_id`` going forward."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="scoped-prompt",
            experiment_id="exp-scoped-prompt",
            name="scoped-prompt",
            owner="@owner",
            project_id="PRJ-scoped",
            visibility="project",
            optimizer_config={"source_type": "prompt_target", "model": None, "bound_to": None},
        )
    )
    db_session.commit()

    operation = prepare_prompt_alias_release(
        db_session,
        name="scoped-prompt",
        alias="prod",
        version_before=None,
        version_after=1,
        actor="@operator",
    )

    assert operation.project_id == "PRJ-scoped"
    assert serialize_release_operation(operation)["project_id"] == "PRJ-scoped"


def test_prepare_leaves_project_id_null_when_the_prompt_has_no_target(
    db_session: Session,
) -> None:
    """A bare provider-only/legacy prompt (no ``CaliberAgentConfig`` row at
    all) has no target to derive a project from -- the same
    "no target = personal/global" carve-out ``prompt_targets.py`` already
    applies elsewhere."""
    operation = prepare_prompt_alias_release(
        db_session,
        name="bare-prompt",
        alias="prod",
        version_before=None,
        version_after=1,
        actor="@operator",
    )

    assert operation.project_id is None
    assert serialize_release_operation(operation)["project_id"] is None


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


def test_applied_release_retry_is_idempotent_and_serializes_provenance(
    db_session: Session,
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-serialize",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
        effective_scopes=("operator", "operator"),
        evidence={"gate": "pass"},
        approval_id="AP-serialize",
    )
    expected = {"name": "p-serialize", "alias": "prod", "version": 2}

    assert (
        execute_prompt_alias_release(db_session, operation, mutate_alias=lambda **_: expected)
        == expected
    )

    def must_not_mutate(**_: object) -> dict[str, object]:
        raise AssertionError("an applied release must not call the provider again")

    assert (
        execute_prompt_alias_release(db_session, operation, mutate_alias=must_not_mutate)
        == expected
    )

    serialized = serialize_release_operation(operation)
    assert serialized["operation_id"] == operation.operation_id
    assert serialized["active_lock"] is None
    assert serialized["effective_scopes"] == ["operator"]
    assert serialized["evidence"] == {"gate": "pass"}
    assert serialized["approval_id"] == "AP-serialize"
    assert serialized["status"] == "applied"
    assert serialized["provider_result"] == expected
    assert serialized["applied_at"] is not None
    assert serialized["created_at"] is not None
    assert serialized["updated_at"] is not None


def _seed_reconciliation_job(
    session: Session, *, job_id: str, approval_id: str, item_id: str, agent_id: str
) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name="Reconciliation agent",
            owner="@operator",
        )
    )
    session.add(
        CaliberVerificationItem(
            item_id=item_id,
            agent_id=agent_id,
            category="release",
            free_text="release reconciliation",
            severity="standard",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id=agent_id,
            primary_item_id=item_id,
            artifact_type="prompt",
            status="applying",
            current_stage="apply",
            bundle_targets=[],
        )
    )
    session.add(
        CaliberApprovalRequest(
            approval_id=approval_id,
            job_id=job_id,
            agent_id=agent_id,
            status="approved",
        )
    )
    session.flush()


def test_reconciler_marks_the_linked_applying_job_applied_when_target_is_observed(
    db_session: Session,
) -> None:
    _seed_reconciliation_job(
        db_session,
        job_id="RFN-reconcile-applied",
        approval_id="AP-reconcile-applied",
        item_id="FB-reconcile-applied",
        agent_id="agent-reconcile-applied",
    )
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-reconcile-applied",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
        approval_id="AP-reconcile-applied",
    )
    operation.status = "applying"
    db_session.commit()

    rows = reconcile_prompt_alias_releases(
        db_session,
        resolve_alias=lambda _name, _alias: {"version": 2},
    )

    assert rows == [operation]
    job = db_session.get(CaliberRefinementJob, "RFN-reconcile-applied")
    assert job is not None
    assert job.status == "applied"
    assert job.current_stage == "done"
    assert (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "reconcile_apply_candidate")
        .count()
        == 1
    )


def test_reconciler_returns_the_linked_job_to_candidate_ready_when_release_was_not_applied(
    db_session: Session,
) -> None:
    _seed_reconciliation_job(
        db_session,
        job_id="RFN-reconcile-not-applied",
        approval_id="AP-reconcile-not-applied",
        item_id="FB-reconcile-not-applied",
        agent_id="agent-reconcile-not-applied",
    )
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-reconcile-not-applied",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
        approval_id="AP-reconcile-not-applied",
    )
    operation.status = "reconcile_required"
    db_session.commit()

    reconcile_prompt_alias_releases(
        db_session,
        resolve_alias=lambda _name, _alias: {"version": 1},
    )

    assert operation.status == "failed"
    assert operation.active_lock is None
    job = db_session.get(CaliberRefinementJob, "RFN-reconcile-not-applied")
    assert job is not None
    assert job.status == "candidate_ready"
    assert job.current_stage == "done"
    assert "pre-release version" in (operation.last_error or "")


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


def test_reconciler_resolves_a_first_promotion_that_never_applied_as_failed(
    db_session: Session,
) -> None:
    """A first-promotion whose provider call never took effect must self-heal
    to ``failed`` (retryable), not get stuck in ``reconcile_required``.

    ``version_before`` is ``None`` here because the alias didn't exist before
    this operation (the common first-promotion case). The real resolver
    (``routes/prompts.py::_load_prompt_release_info``) returns ``None`` only
    when it has positively confirmed there is no prompt at the alias
    (``allow_missing=True``) -- it *raises* on a genuine provider failure
    instead of ever returning ``None`` -- so an observed ``None`` here is a
    confirmed "still doesn't exist", exactly as determinable as the ordinary
    version_before/version_after comparison. Before the fix, this fell into
    the same bucket as a genuinely ambiguous observation and required manual
    operator intervention to unstick (this exact test previously asserted
    ``reconcile_required`` here, pinning that gap).
    """
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

    rows = reconcile_prompt_alias_releases(
        db_session,
        resolve_alias=lambda _name, _alias: None,
    )
    assert rows == [operation]
    db_session.expire_all()
    resolved = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert resolved is not None
    assert resolved.status == "failed"
    assert resolved.active_lock is None
    assert "never applied" in (resolved.last_error or "")


def test_reconciler_still_flags_an_ambiguous_first_promotion_as_reconcile_required(
    db_session: Session,
) -> None:
    """The fix above must not broaden the "failed" bucket beyond the exact
    ``observed == version_before`` match.

    Here the provider resolves to some *other* concrete version -- matching
    neither the pre-mutation state (``None``) nor the requested
    ``version_after`` -- so the outcome genuinely cannot be determined
    automatically and must stay ``reconcile_required`` (fail-closed), the
    same as the non-first-promotion ambiguous case covered by
    ``test_reconciler_settles_observed_target_and_flags_unknown_state``.
    """
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-cold-start-ambiguous",
        alias="prod",
        version_before=None,
        version_after=1,
        actor="@operator",
    )
    operation.status = "applying"
    db_session.commit()

    reconcile_prompt_alias_releases(
        db_session,
        resolve_alias=lambda _name, _alias: {"version": 7},
    )
    db_session.expire_all()
    unresolved = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert unresolved is not None
    assert unresolved.status == "reconcile_required"
    assert unresolved.active_lock == "prompt:p-cold-start-ambiguous:prod"
    assert "unexpected version 7" in (unresolved.last_error or "")


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


def test_prepare_recovers_an_exact_request_after_an_insert_race() -> None:
    """A duplicate operation-id race is a safe retry when the request matches."""
    concurrent = SimpleNamespace(
        operation_type="promote",
        resource_name="support-agent",
        target_name="prod",
        version_after=5,
    )
    # `session.get` is now called for two different models: the
    # `CaliberReleaseOperation` existence/race-recovery checks (positional,
    # via the queue below) and `P2-R`'s new `CaliberAgentConfig` hidden-target
    # lookup (dispatched by model instead -- this test has no target, so it
    # always resolves to `None`, i.e. an unscoped/personal release).
    release_operation_lookups = iter([None, concurrent])

    def _session_get(model: object, _pk: object) -> object:
        if model is CaliberAgentConfig:
            return None
        return next(release_operation_lookups)

    session = Mock()
    session.get.side_effect = _session_get
    session.commit.side_effect = IntegrityError("duplicate", {}, RuntimeError("raced insert"))

    result = prepare_prompt_alias_release(
        session,
        name="support-agent",
        alias="prod",
        version_before=4,
        version_after=5,
        actor="@operator",
        operation_id="REL-raced",
    )

    assert result is concurrent
    session.rollback.assert_called_once_with()
