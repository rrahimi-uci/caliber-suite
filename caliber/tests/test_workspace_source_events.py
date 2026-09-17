"""Persistence tests for the P4-E source-event inbox and actor links."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceSource,
    CaliberWorkspaceSourceActorLink,
    CaliberWorkspaceSourceEvent,
)
from caliber.ids import new_workspace_source_actor_link_id, new_workspace_source_event_id
from caliber.workspace_source_control import SourceControlWebhook
from caliber.workspace_source_events import (
    ACTOR_LINK_ACTIVE,
    ACTOR_LINK_REVOKED,
    SOURCE_EVENT_FAILED,
    SOURCE_EVENT_PROCESSED,
    SOURCE_EVENT_RECEIVED,
    WorkspaceSourceActorLinkConflictError,
    WorkspaceSourceActorLinkNotFoundError,
    WorkspaceSourceEventConflictError,
    WorkspaceSourceEventNotFoundError,
    WorkspaceSourceEventTransitionError,
    get_active_source_actor_link,
    link_source_actor,
    record_source_event,
    requeue_failed_source_event,
    revoke_source_actor_link,
    transition_source_event,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
PROJECT_ID = "PRJ-source-events"
SOURCE_ID = "WSS-source-events"
REPOSITORY_ID = "github:owner/workspace"


def _source(session: Session, *, project_id: str = PROJECT_ID, source_id: str = SOURCE_ID) -> None:
    session.add(CaliberProject(project_id=project_id, name="Source events", owner="@admin"))
    session.flush()
    session.add(
        CaliberWorkspaceSource(
            source_id=source_id,
            project_id=project_id,
            provider="github",
            provider_host="github.com",
            canonical_repository_id=REPOSITORY_ID,
            display_path="owner/workspace",
            status="active",
        )
    )
    session.flush()


def _event(
    delivery_id: str = "delivery-1", *, payload_label: str = "payload"
) -> SourceControlWebhook:
    import hashlib

    return SourceControlWebhook(
        delivery_id=delivery_id,
        event_type="pull_request",
        repository_id=REPOSITORY_ID,
        payload_sha256=hashlib.sha256(payload_label.encode()).hexdigest(),
        signature_verified=True,
    )


def test_workspace_source_ids_use_distinct_event_and_actor_prefixes() -> None:
    assert new_workspace_source_event_id().startswith("WSSE-")
    assert new_workspace_source_actor_link_id().startswith("WSSAL-")


def test_source_event_recording_is_idempotent_and_digest_bound(db_session: Session) -> None:
    _source(db_session)
    event = _event()
    first, inserted = record_source_event(db_session, source_id=SOURCE_ID, event=event, now=NOW)
    second, replayed_insert = record_source_event(
        db_session, source_id=SOURCE_ID, event=event, now=NOW
    )

    assert inserted is True
    assert replayed_insert is False
    assert second.source_event_id == first.source_event_id
    assert first.status == SOURCE_EVENT_RECEIVED
    assert first.received_at == NOW.replace(tzinfo=None)
    assert not hasattr(first, "payload")
    assert db_session.query(CaliberWorkspaceSourceEvent).count() == 1

    with pytest.raises(WorkspaceSourceEventConflictError, match="reused"):
        record_source_event(
            db_session,
            source_id=SOURCE_ID,
            event=_event(payload_label="different-payload"),
            now=NOW,
        )


def test_source_event_ingress_fails_closed_for_untrusted_or_wrong_sources(
    db_session: Session,
) -> None:
    _source(db_session)
    with pytest.raises(WorkspaceSourceEventConflictError, match="signature-verified"):
        record_source_event(
            db_session,
            source_id=SOURCE_ID,
            event=SourceControlWebhook(
                "delivery-untrusted",
                "push",
                REPOSITORY_ID,
                "a" * 64,
                False,
            ),
        )
    with pytest.raises(WorkspaceSourceEventConflictError, match="does not match"):
        record_source_event(
            db_session,
            source_id=SOURCE_ID,
            event=SourceControlWebhook(
                "delivery-wrong-repo",
                "push",
                "github:other/repository",
                "b" * 64,
                True,
            ),
        )
    with pytest.raises(WorkspaceSourceEventNotFoundError, match="source"):
        record_source_event(db_session, source_id="WSS-missing", event=_event())


def test_source_event_transitions_are_literal_and_requeue_is_explicit(db_session: Session) -> None:
    _source(db_session)
    row, _ = record_source_event(db_session, source_id=SOURCE_ID, event=_event(), now=NOW)

    processed = transition_source_event(
        db_session, row.source_event_id, status=SOURCE_EVENT_PROCESSED, now=NOW
    )
    assert processed.processed_at == NOW.replace(tzinfo=None)
    assert processed.error_code is None
    assert (
        transition_source_event(
            db_session, row.source_event_id, status=SOURCE_EVENT_PROCESSED
        ).status
        == SOURCE_EVENT_PROCESSED
    )
    with pytest.raises(WorkspaceSourceEventTransitionError, match="from 'processed'"):
        transition_source_event(db_session, row.source_event_id, status=SOURCE_EVENT_FAILED)

    failed_row, _ = record_source_event(
        db_session, source_id=SOURCE_ID, event=_event("delivery-2"), now=NOW
    )
    failed = transition_source_event(
        db_session,
        failed_row.source_event_id,
        status=SOURCE_EVENT_FAILED,
        error_code="provider_timeout",
        error_summary="temporary outage",
        now=NOW,
    )
    assert failed.error_code == "provider_timeout"
    assert failed.error_summary == "temporary outage"
    assert failed.processed_at == NOW.replace(tzinfo=None)
    assert (
        transition_source_event(
            db_session, failed_row.source_event_id, status=SOURCE_EVENT_FAILED
        ).status
        == SOURCE_EVENT_FAILED
    )
    reopened = requeue_failed_source_event(db_session, failed_row.source_event_id)
    assert reopened.status == SOURCE_EVENT_RECEIVED
    assert reopened.processed_at is None
    assert reopened.error_code is None
    transition_source_event(db_session, failed_row.source_event_id, status=SOURCE_EVENT_RECEIVED)
    with pytest.raises(WorkspaceSourceEventTransitionError, match="only failed"):
        requeue_failed_source_event(db_session, failed_row.source_event_id)
    with pytest.raises(WorkspaceSourceEventTransitionError, match="unsupported"):
        transition_source_event(db_session, failed_row.source_event_id, status="ignored")
    with pytest.raises(WorkspaceSourceEventNotFoundError, match="source event"):
        transition_source_event(db_session, "WSSE-missing", status=SOURCE_EVENT_PROCESSED)
    with pytest.raises(WorkspaceSourceEventNotFoundError, match="source event"):
        requeue_failed_source_event(db_session, "WSSE-missing")


def test_source_event_errors_are_bounded_and_required_for_failure(db_session: Session) -> None:
    _source(db_session)
    row, _ = record_source_event(db_session, source_id=SOURCE_ID, event=_event(), now=NOW)
    with pytest.raises(ValueError, match="error_code is required"):
        transition_source_event(db_session, row.source_event_id, status=SOURCE_EVENT_FAILED)
    with pytest.raises(ValueError, match="error_code must be a non-empty"):
        transition_source_event(
            db_session, row.source_event_id, status=SOURCE_EVENT_FAILED, error_code=""
        )
    with pytest.raises(ValueError, match="at most 64"):
        transition_source_event(
            db_session,
            row.source_event_id,
            status=SOURCE_EVENT_FAILED,
            error_code="x" * 65,
        )
    failed_without_summary = transition_source_event(
        db_session,
        row.source_event_id,
        status=SOURCE_EVENT_FAILED,
        error_code="provider_timeout",
    )
    assert failed_without_summary.error_summary is None
    with pytest.raises(ValueError, match="at most 2048"):
        transition_source_event(
            db_session,
            row.source_event_id,
            status=SOURCE_EVENT_FAILED,
            error_code="failed",
            error_summary="x" * 2049,
        )
    with pytest.raises(ValueError, match="delivery_id"):
        record_source_event(
            db_session,
            source_id=SOURCE_ID,
            event=_event("x" * 257),
        )


def test_source_event_insert_race_replays_the_committed_row(
    db_session: Session, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(db_session)
    db_session.commit()
    other = session_factory()
    try:
        first, inserted = record_source_event(other, source_id=SOURCE_ID, event=_event(), now=NOW)
        other.commit()
        assert inserted is True
        source = db_session.get(CaliberWorkspaceSource, SOURCE_ID)
        assert source is not None

        original_flush = db_session.flush
        raised = False

        def race_flush(*args: object, **kwargs: object) -> None:
            nonlocal raised
            if not raised and db_session.new:
                raised = True
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("insert", {}, RuntimeError("unique race"))
            original_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", race_flush)

        class EmptyResult:
            def scalar_one_or_none(self) -> None:
                return None

        original_execute = db_session.execute
        execute_calls = 0

        def race_execute(statement, *args: object, **kwargs: object):
            nonlocal execute_calls
            execute_calls += 1
            return (
                EmptyResult()
                if execute_calls == 1
                else original_execute(statement, *args, **kwargs)
            )

        monkeypatch.setattr(db_session, "execute", race_execute)
        replay, replay_inserted = record_source_event(
            db_session, source_id=SOURCE_ID, event=_event(), now=NOW
        )
        assert replay.source_event_id == first.source_event_id
        assert replay_inserted is False
    finally:
        other.close()


def test_actor_links_are_project_bound_idempotent_and_revocable(db_session: Session) -> None:
    _source(db_session)
    link, inserted = link_source_actor(
        db_session,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        provider_subject_id="github-user-1",
        caliber_user_id="@alice",
        verified_method="admin-confirmed",
        now=NOW,
    )
    same, inserted_again = link_source_actor(
        db_session,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        provider_subject_id="github-user-1",
        caliber_user_id="@alice",
        verified_method="admin-confirmed",
        now=NOW,
    )
    assert inserted is True
    assert inserted_again is False
    assert same.source_actor_link_id == link.source_actor_link_id
    assert link.status == ACTOR_LINK_ACTIVE
    assert (
        get_active_source_actor_link(
            db_session, source_id=SOURCE_ID, provider_subject_id="github-user-1"
        )
        is link
    )
    with pytest.raises(WorkspaceSourceActorLinkConflictError, match="another"):
        link_source_actor(
            db_session,
            project_id=PROJECT_ID,
            source_id=SOURCE_ID,
            provider_subject_id="github-user-1",
            caliber_user_id="@bob",
            verified_method="admin-confirmed",
        )

    revoked, changed = revoke_source_actor_link(
        db_session,
        link.source_actor_link_id,
        revoked_by="@admin",
        reason="identity revoked",
        now=NOW,
    )
    assert changed is True
    assert revoked.status == ACTOR_LINK_REVOKED
    assert revoked.revoked_at == NOW.replace(tzinfo=None)
    assert (
        get_active_source_actor_link(
            db_session, source_id=SOURCE_ID, provider_subject_id="github-user-1"
        )
        is None
    )
    revoked_again, changed_again = revoke_source_actor_link(
        db_session,
        link.source_actor_link_id,
        revoked_by="@admin",
        reason="duplicate revoke",
    )
    assert revoked_again is link
    assert changed_again is False

    relinked, relinked_inserted = link_source_actor(
        db_session,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        provider_subject_id="github-user-1",
        caliber_user_id="@bob",
        verified_method="reverified",
        now=NOW,
    )
    assert relinked_inserted is True
    assert relinked.source_actor_link_id != link.source_actor_link_id
    assert db_session.query(CaliberWorkspaceSourceActorLink).count() == 2


def test_actor_link_scope_and_missing_rows_fail_closed(db_session: Session) -> None:
    _source(db_session)
    with pytest.raises(WorkspaceSourceActorLinkNotFoundError, match="source"):
        link_source_actor(
            db_session,
            project_id="PRJ-other",
            source_id=SOURCE_ID,
            provider_subject_id="subject",
            caliber_user_id="@user",
            verified_method="manual",
        )
    with pytest.raises(ValueError, match="provider_subject_id"):
        link_source_actor(
            db_session,
            project_id=PROJECT_ID,
            source_id=SOURCE_ID,
            provider_subject_id="",
            caliber_user_id="@user",
            verified_method="manual",
        )
    with pytest.raises(WorkspaceSourceActorLinkNotFoundError, match="actor link"):
        revoke_source_actor_link(
            db_session,
            "WSSAL-missing",
            revoked_by="@admin",
            reason="not found",
        )


def test_actor_link_insert_race_replays_same_user_link(
    db_session: Session, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(db_session)
    db_session.commit()
    other = session_factory()
    try:
        first, inserted = link_source_actor(
            other,
            project_id=PROJECT_ID,
            source_id=SOURCE_ID,
            provider_subject_id="race-subject",
            caliber_user_id="@alice",
            verified_method="admin-confirmed",
            now=NOW,
        )
        other.commit()
        assert inserted is True
        source = db_session.get(CaliberWorkspaceSource, SOURCE_ID)
        assert source is not None

        original_flush = db_session.flush
        raised = False

        def race_flush(*args: object, **kwargs: object) -> None:
            nonlocal raised
            if not raised and db_session.new:
                raised = True
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("insert", {}, RuntimeError("unique race"))
            original_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", race_flush)

        class EmptyResult:
            def scalar_one_or_none(self) -> None:
                return None

        original_execute = db_session.execute
        execute_calls = 0

        def race_execute(statement, *args: object, **kwargs: object):
            nonlocal execute_calls
            execute_calls += 1
            return (
                EmptyResult()
                if execute_calls == 1
                else original_execute(statement, *args, **kwargs)
            )

        monkeypatch.setattr(db_session, "execute", race_execute)
        replay, replay_inserted = link_source_actor(
            db_session,
            project_id=PROJECT_ID,
            source_id=SOURCE_ID,
            provider_subject_id="race-subject",
            caliber_user_id="@alice",
            verified_method="admin-confirmed",
            now=NOW,
        )
        assert replay.source_actor_link_id == first.source_actor_link_id
        assert replay_inserted is False
    finally:
        other.close()
