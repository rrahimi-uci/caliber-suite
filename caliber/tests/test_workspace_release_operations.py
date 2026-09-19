"""P5-C tests for intent-first Workspace release operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from caliber import workspace_release_operations as release_operations
from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceReleaseOperationItem,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    FakeWorkspaceResourceAdapter,
    PreparedAction,
    ProviderOutcome,
    WorkspaceProviderTimeoutError,
    WorkspaceReleaseAdapterError,
    WorkspaceReleaseAdapterUnavailableError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operations import (
    WorkspaceReleaseExecutionError,
    apply_workspace_release_operation,
    cancel_expired_workspace_release_operation,
    observe_workspace_release_operation,
    prepare_workspace_release_operation,
)
from caliber.workspace_release_service import create_workspace_release

PROJECT_ID = "PRJ-p5c"
ENV_ID = "WSE-p5c-prod"
REVISION_ID = "WSR-p5c"
HEX = "a" * 64


def _seed(
    session: Session,
    *,
    release_key: str = "release-1",
    revision_status: str = "ready",
) -> tuple[CaliberWorkspaceRelease, FakeWorkspaceResourceAdapter]:
    session.add(CaliberProject(project_id=PROJECT_ID, name="P5-C", owner="admin"))
    session.add(
        CaliberWorkspaceEnvironment(
            environment_id=ENV_ID,
            project_id=PROJECT_ID,
            name="prod",
            environment_class="production",
            promotion_order=40,
            status="active",
            created_by="admin",
        )
    )
    session.add(
        CaliberWorkspaceRevision(
            revision_id=REVISION_ID,
            project_id=PROJECT_ID,
            revision_number=1,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=HEX,
            source_bundle_sha256=HEX,
            revision_sha256=HEX,
            status=revision_status,
            created_by="developer",
        )
    )
    session.add_all(
        [
            CaliberWorkspaceRevisionResource(
                resource_pin_id="WSRR-p5c-1",
                revision_id=REVISION_ID,
                resource_type="fake",
                logical_name="one",
                resource_id="fake-1",
                version_ref="1",
                content_sha256=HEX,
                provider_ref="fake:one:v1",
                purpose="runtime",
                resolution={},
            ),
            CaliberWorkspaceRevisionResource(
                resource_pin_id="WSRR-p5c-2",
                revision_id=REVISION_ID,
                resource_type="fake",
                logical_name="two",
                resource_id="fake-2",
                version_ref="1",
                content_sha256=HEX,
                provider_ref="fake:two:v1",
                purpose="runtime",
                resolution={},
            ),
        ]
    )
    release = create_workspace_release(
        session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=ENV_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=release_key,
        requested_by="developer",
    )
    release.status = "approved"
    session.flush()
    adapter = FakeWorkspaceResourceAdapter()
    return release, adapter


def _registry(adapter: FakeWorkspaceResourceAdapter) -> WorkspaceResourceAdapterRegistry:
    return WorkspaceResourceAdapterRegistry({"fake": adapter})


def _prepare(
    session: Session,
    release: CaliberWorkspaceRelease,
    adapter: FakeWorkspaceResourceAdapter,
    *,
    key: str = "op-1",
    kind: str = "apply",
    expected_current: str | None = None,
    target: str | None = None,
    expected_lock: int = 1,
):
    return prepare_workspace_release_operation(
        session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=ENV_ID,
        kind=kind,
        idempotency_key=key,
        expected_environment_lock_version=expected_lock,
        expected_current_release_id=expected_current,
        target_release_id=target,
        requested_by="admin",
        adapters=_registry(adapter),
    )


def test_prepare_and_apply_advances_pointer_only_after_all_children(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter)
    assert prepared.operation.status == "prepared"
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.pending_operation_id == prepared.operation.operation_id
    assert environment.current_release_id is None

    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter), actor="admin"
    )
    assert result.operation.status == "applied"
    assert all(item.status == "applied" for item in result.items)
    db_session.refresh(environment)
    assert environment.current_release_id == release.release_id
    assert environment.pending_operation_id is None
    assert environment.operation_state == "idle"
    assert environment.lock_version == 2
    assert len(adapter.apply_calls) == 2


def test_prepare_is_idempotent_and_does_not_reinvoke_adapter(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    first = _prepare(db_session, release, adapter)
    replay = _prepare(db_session, release, adapter)
    assert replay.operation.operation_id == first.operation.operation_id
    assert len(replay.items) == len(first.items) == 2
    assert adapter.apply_calls == []


def test_prepare_rejects_stale_environment_lock(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.lock_version = 2
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="stale"):
        _prepare(db_session, release, adapter)


def test_missing_resource_adapter_fails_before_lock_is_persisted(db_session: Session) -> None:
    release, _adapter = _seed(db_session)
    with pytest.raises(WorkspaceReleaseAdapterUnavailableError):
        prepare_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=ENV_ID,
            kind="apply",
            idempotency_key="missing-adapter",
            expected_environment_lock_version=1,
            requested_by="admin",
            adapters=WorkspaceResourceAdapterRegistry(),
        )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.pending_operation_id is None


def test_timeout_before_effect_fails_and_releases_lock(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter)
    adapter.queue_apply(WorkspaceProviderTimeoutError("connection failed", effect_started=False))
    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert result.operation.status == "failed"
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id is None
    assert environment.pending_operation_id is None
    assert result.items[0].status == "failed"


def test_apply_refuses_environment_that_becomes_inactive(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="inactive-before-effect")
    prepared = _prepare(db_session, release, adapter, key="inactive-before-effect-op")
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.status = "degraded"
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="not active"):
        apply_workspace_release_operation(
            db_session, prepared.operation.operation_id, adapters=_registry(adapter)
        )
    assert adapter.apply_calls == []


def test_timeout_after_effect_requires_observation_then_advances_pointer(
    db_session: Session,
) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter)
    adapter.queue_apply(WorkspaceProviderTimeoutError("response lost", effect_started=True))
    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert result.operation.status == "reconcile_required"
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id is None
    assert environment.pending_operation_id == prepared.operation.operation_id
    observed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert observed.operation.status == "applied"
    db_session.refresh(environment)
    assert environment.current_release_id == release.release_id
    assert environment.pending_operation_id is None


def test_partial_child_outcome_never_reports_success_and_reconcile_settles(
    db_session: Session,
) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter)
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:partial",
            error_code="ambiguous",
            error_summary="provider accepted but response was lost",
        )
    )
    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert result.operation.status == "reconcile_required"
    adapter.queue_observe(ProviderOutcome("applied", "provider:observed"))
    settled = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert settled.operation.status == "applied"
    assert settled.operation.observation_count == 1


def test_rollback_moves_only_environment_pointer_and_keeps_original_release(
    db_session: Session,
) -> None:
    first, adapter = _seed(db_session)
    first_operation = _prepare(db_session, first, adapter, key="apply-first")
    apply_workspace_release_operation(
        db_session, first_operation.operation.operation_id, adapters=_registry(adapter)
    )
    second = create_workspace_release(
        db_session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=ENV_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key="release-second",
        requested_by="developer",
    )
    second.status = "approved"
    db_session.flush()
    second_operation = _prepare(
        db_session,
        second,
        adapter,
        key="apply-second",
        expected_current=first.release_id,
        expected_lock=2,
    )
    apply_workspace_release_operation(
        db_session, second_operation.operation.operation_id, adapters=_registry(adapter)
    )
    rollback = _prepare(
        db_session,
        second,
        adapter,
        key="rollback",
        kind="rollback",
        expected_current=second.release_id,
        target=first.release_id,
        expected_lock=3,
    )
    result = apply_workspace_release_operation(
        db_session, rollback.operation.operation_id, adapters=_registry(adapter)
    )
    assert result.operation.status == "applied"
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id == first.release_id
    assert first.status == "approved"
    assert second.status == "approved"
    assert len(adapter.rollback_calls) == 2


def test_expired_prepared_break_glass_operation_is_cancelled_without_provider_call(
    db_session: Session,
) -> None:
    release, adapter = _seed(db_session)
    authorization = CaliberWorkspaceBreakGlassAuthorization(
        authorization_id="WSBGA-p5c",
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=ENV_ID,
        reason="incident",
        incident_ref="INC-1",
        authorization_ref="AUTH-1",
        authorized_by="admin",
        credential_kind="session",
        credential_id="session-1",
        revision_sha256=HEX,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        gate_evidence_sha256=HEX,
        policy_sha256=HEX,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(authorization)
    db_session.flush()
    prepared = _prepare(db_session, release, adapter, key="expired", kind="apply")
    prepared.operation.break_glass_authorization_id = authorization.authorization_id
    db_session.flush()
    cancelled = cancel_expired_workspace_release_operation(
        db_session, prepared.operation.operation_id
    )
    assert cancelled.operation.status == "cancelled"
    assert adapter.apply_calls == []


def test_adapter_contract_and_registry_reject_invalid_values(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    pin = db_session.query(CaliberWorkspaceRevisionResource).first()
    assert pin is not None
    assert adapter.resolve(None, None, "declaration", None) == "declaration"
    assert adapter.snapshot(None, pin) is pin
    assert adapter.validate(None, pin, environment)["valid"] is True
    fallback = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-fallback",
        revision_id=REVISION_ID,
        resource_type="fake",
        logical_name="fallback",
        resource_id="fallback",
        version_ref="1",
        content_sha256=HEX,
        purpose="runtime",
        resolution={},
    )
    prepared = adapter.prepare_release(fallback, environment, before_ref=None)
    assert prepared.target_ref == "fallback@1"
    assert adapter.rollback_release(None, prepared).status == "applied"
    assert adapter.rollback_calls
    with pytest.raises(ValueError, match="target_ref"):
        PreparedAction("pin", "fake", "promote", "", None, None)
    with pytest.raises(ValueError, match="unsupported"):
        ProviderOutcome("other", "provider")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider_operation_ref"):
        ProviderOutcome("applied", "")
    with pytest.raises(ValueError, match="failed provider"):
        ProviderOutcome("failed", "provider")
    with pytest.raises(ValueError, match="ambiguous"):
        ProviderOutcome("reconcile_required", "provider")
    registry = WorkspaceResourceAdapterRegistry({"ignored": adapter})
    assert registry.names() == ("fake",)
    assert registry.get("FAKE") is adapter
    with pytest.raises(ValueError, match="already"):
        registry.register(adapter)
    registry.register(adapter, replace=True)
    with pytest.raises(ValueError, match="must not be empty"):
        registry.register(FakeWorkspaceResourceAdapter(""))
    missing_observation = adapter.observe_release(
        None, PreparedAction("pin-missing", "fake", "promote", "fake:missing", None, "fake:missing")
    )
    assert missing_observation.status == "failed"


def test_apply_rejects_missing_or_nonprepared_operations(db_session: Session) -> None:
    _release, adapter = _seed(db_session)
    with pytest.raises(WorkspaceReleaseExecutionError, match="not found"):
        apply_workspace_release_operation(db_session, "missing", adapters=_registry(adapter))
    prepared = _prepare(db_session, _release, adapter)
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    with pytest.raises(WorkspaceReleaseExecutionError, match="not prepared"):
        apply_workspace_release_operation(
            db_session, prepared.operation.operation_id, adapters=_registry(adapter)
        )


def test_prepare_rejects_busy_environment_and_stale_current_pointer(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.current_release_id = "WSREL-other"
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="current release"):
        _prepare(
            db_session, release, adapter, key="stale-current", expected_current=release.release_id
        )
    active = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert active is None
    operation = db_session.query(CaliberWorkspaceReleaseOperation).first()
    assert operation is not None
    db_session.delete(operation)
    db_session.flush()
    environment.current_release_id = None
    environment.operation_state = "applying"
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="pending"):
        _prepare(db_session, release, adapter, key="busy")


def test_prepare_rejects_empty_or_invalid_revision_and_pointer(db_session: Session) -> None:
    release, adapter = _seed(db_session, revision_status="invalid")
    with pytest.raises(WorkspaceReleaseExecutionError, match="not ready"):
        _prepare(db_session, release, adapter, key="invalid-revision")


def test_prepare_rejects_revision_without_resource_pins(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="empty-pins")
    db_session.query(CaliberWorkspaceRevisionResource).delete()
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="no resource pins"):
        _prepare(db_session, release, adapter, key="empty-revision")


def test_apply_known_provider_failure_releases_lock(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter, key="provider-failure")
    adapter.queue_apply(
        ProviderOutcome(
            "failed", "provider:failed", error_code="rejected", error_summary="rejected"
        )
    )
    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert result.operation.status == "failed"
    assert result.items[0].status == "failed"


def test_apply_adapter_error_is_terminal_failure(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter, key="adapter-error")

    class BrokenAdapter(FakeWorkspaceResourceAdapter):
        def apply_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
            raise WorkspaceReleaseAdapterError("adapter unavailable")

    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(BrokenAdapter())
    )
    assert result.operation.status == "failed"


def test_apply_missing_item_is_terminal_failure(db_session: Session) -> None:
    release2, adapter2 = _seed(db_session, release_key="missing-item")
    prepared2 = _prepare(db_session, release2, adapter2, key="missing-item-op")
    db_session.execute(
        update(CaliberWorkspaceReleaseOperationItem)
        .where(
            CaliberWorkspaceReleaseOperationItem.workspace_release_operation_id
            == prepared2.operation.operation_id
        )
        .values(revision_resource_id="WSRR-missing")
    )
    db_session.flush()
    missing = apply_workspace_release_operation(
        db_session, prepared2.operation.operation_id, adapters=_registry(adapter2)
    )
    assert missing.operation.status == "failed"


def test_observe_reconcile_stays_blocked_then_known_failure_fails(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter, key="observe-blocked")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    adapter.queue_observe(
        ProviderOutcome(
            "reconcile_required",
            "provider:still-unknown",
            error_code="unknown",
            error_summary="still unknown",
        )
    )
    pending = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert pending.operation.status == "reconcile_required"
    adapter.queue_observe(
        ProviderOutcome("failed", "provider:failed", error_code="gone", error_summary="gone")
    )
    failed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert failed.operation.status == "failed"


def test_observe_rejects_settled_or_unowned_operation(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter, key="observe-state")
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    with pytest.raises(WorkspaceReleaseExecutionError, match="reconcile-required"):
        observe_workspace_release_operation(
            db_session, prepared.operation.operation_id, adapters=_registry(adapter)
        )


def test_cancel_expiry_rejects_nonexpired_or_wrong_state(db_session: Session) -> None:
    release, adapter = _seed(db_session)
    prepared = _prepare(db_session, release, adapter, key="cancel-state")
    with pytest.raises(WorkspaceReleaseExecutionError, match="break-glass"):
        cancel_expired_workspace_release_operation(db_session, prepared.operation.operation_id)


def test_private_coordinate_guards_fail_closed(db_session: Session) -> None:
    release, _adapter = _seed(db_session, release_key="private-guards")
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    operation = db_session.query(CaliberWorkspaceReleaseOperation).first()
    assert operation is None
    with pytest.raises(WorkspaceReleaseExecutionError, match="outside"):
        release_operations._release(db_session, "missing", PROJECT_ID)
    fake_operation = type("Operation", (), {"environment_id": ENV_ID, "project_id": "other"})()
    with pytest.raises(WorkspaceReleaseExecutionError, match="outside"):
        release_operations._require_environment(db_session, fake_operation)
    environment.current_release_id = "WSREL-invalid"
    with pytest.raises(WorkspaceReleaseExecutionError, match="pointer"):
        release_operations._current_refs(db_session, environment)
    environment.current_release_id = release.release_id
    with pytest.raises(WorkspaceReleaseExecutionError, match="outside"):
        release_operations._release(db_session, release.release_id, "other")


def test_apply_requires_ownership_and_rejects_missing_adapter_metadata(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="ownership")
    prepared = _prepare(db_session, release, adapter, key="ownership-op")
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.pending_operation_id = "other-operation"
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="does not own"):
        apply_workspace_release_operation(
            db_session, prepared.operation.operation_id, adapters=_registry(adapter)
        )

    db_session.rollback()
    release, adapter = _seed(db_session, release_key="metadata")
    prepared = _prepare(db_session, release, adapter, key="metadata-op")
    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.provider_result = {}
    item.status = "prepared"
    db_session.flush()
    failed = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert failed.operation.status == "failed"


def test_observation_error_and_missing_metadata_paths_settle_failed(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="observe-errors")
    prepared = _prepare(db_session, release, adapter, key="observe-errors-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )

    class BrokenAdapter(FakeWorkspaceResourceAdapter):
        def apply_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
            raise WorkspaceReleaseAdapterError("apply broke")

    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.status = "prepared"
    db_session.flush()
    failed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(BrokenAdapter())
    )
    assert failed.operation.status == "failed"


def test_observation_handles_prepared_timeout_boundaries(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="observe-timeout")
    prepared = _prepare(db_session, release, adapter, key="observe-timeout-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.status = "prepared"
    db_session.flush()
    adapter.queue_apply(WorkspaceProviderTimeoutError("before effect", effect_started=False))
    failed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert failed.operation.status == "failed"

    db_session.rollback()
    release, adapter = _seed(db_session, release_key="observe-timeout-after")
    prepared = _prepare(db_session, release, adapter, key="observe-timeout-after-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.status = "prepared"
    db_session.flush()
    adapter.queue_apply(WorkspaceProviderTimeoutError("after effect", effect_started=True))
    adapter.queue_observe(ProviderOutcome("applied", "provider:observed"))
    pending = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert pending.operation.status == "reconcile_required"


def test_observation_rejects_unowned_and_failed_items(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="observe-owner")
    prepared = _prepare(db_session, release, adapter, key="observe-owner-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.pending_operation_id = "other-operation"
    db_session.flush()
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="does not own"):
        observe_workspace_release_operation(
            db_session, prepared.operation.operation_id, adapters=_registry(adapter)
        )


def test_observation_handles_failed_and_missing_metadata_items(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="observe-item-errors")
    prepared = _prepare(db_session, release, adapter, key="observe-item-errors-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.status = "failed"
    db_session.flush()
    failed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert failed.operation.status == "failed"

    db_session.rollback()
    release, adapter = _seed(db_session, release_key="observe-missing-meta")
    prepared = _prepare(db_session, release, adapter, key="observe-missing-meta-op")
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "provider:ambiguous",
            error_code="unknown",
            error_summary="unknown",
        )
    )
    apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    item = db_session.query(CaliberWorkspaceReleaseOperationItem).first()
    assert item is not None
    item.provider_result = {}
    item.status = "prepared"
    db_session.flush()
    failed = observe_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=_registry(adapter)
    )
    assert failed.operation.status == "failed"


def test_environment_settlement_guards_and_prepare_cas_loser(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, adapter = _seed(db_session, release_key="cas-loser")
    prepared = _prepare(db_session, release, adapter, key="cas-loser-op")
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    environment.pending_operation_id = "other-operation"
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="no longer owns"):
        release_operations._set_environment_terminal(
            db_session, environment, prepared.operation, current_release_id=None
        )
    with pytest.raises(WorkspaceReleaseExecutionError, match="no longer owns"):
        release_operations._set_environment_reconcile_required(
            db_session, environment, prepared.operation
        )

    db_session.rollback()
    release, adapter = _seed(db_session, release_key="cas-update-loser")
    original_execute = Session.execute

    def lose_environment_update(
        self: Session, statement: object, *args: object, **kwargs: object
    ) -> object:
        if (
            isinstance(statement, Update)
            and statement.table.name == "caliber_workspace_environments"
        ):
            return SimpleNamespace(rowcount=0)
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", lose_environment_update)
    with pytest.raises(WorkspaceReleaseExecutionError, match="lock was lost"):
        _prepare(db_session, release, adapter, key="cas-update-loser-op")


def test_cancel_expiry_rejects_nonexpired_authorization(db_session: Session) -> None:
    release, adapter = _seed(db_session, release_key="nonexpired")
    prepared = _prepare(db_session, release, adapter, key="nonexpired-op")
    authorization = CaliberWorkspaceBreakGlassAuthorization(
        authorization_id="WSBGA-nonexpired",
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=ENV_ID,
        reason="incident",
        incident_ref="INC-2",
        authorization_ref="AUTH-2",
        authorized_by="admin",
        credential_kind="session",
        credential_id="session-2",
        revision_sha256=HEX,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        gate_evidence_sha256=HEX,
        policy_sha256=HEX,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(authorization)
    prepared.operation.break_glass_authorization_id = authorization.authorization_id
    db_session.flush()
    with pytest.raises(WorkspaceReleaseExecutionError, match="not expired"):
        cancel_expired_workspace_release_operation(db_session, prepared.operation.operation_id)
