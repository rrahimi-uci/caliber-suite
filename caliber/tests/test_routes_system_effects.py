"""``/caliber/system/effects`` — the operational surface for indeterminate effects.

The independent review's L4 finding had two halves. The occurrence-identity half is
covered in ``tests/test_effect_ledger.py``; this file covers the other:

    the instructed manual resolution for ``indeterminate`` has no API, CLI, or UI.

A fail-closed control whose only remediation is direct database access leaves a run
blocked until someone with a psql prompt happens to notice. These tests assert the
endpoints exist, are scoped, are audited, and refuse the operations that would let a
recovery tool corrupt the evidence it recovers from.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog, CaliberEffectLedger
from caliber.workflows.effect_ledger import COMPLETED, FAILED, IN_PROGRESS
from tests.workflow_helpers import PREFIX

EFFECTS = f"{PREFIX}/system/effects"


def _stuck_effect(session: Session, key: str = "eff-stuck", run_id: str = "WFR-1") -> str:
    """A claim from an attempt that died before recording an outcome."""
    session.add(
        CaliberEffectLedger(
            effect_key=key,
            workflow_run_id=run_id,
            node_id="charge",
            status=IN_PROGRESS,
        )
    )
    session.commit()
    return key


def test_listing_defaults_to_the_claims_that_need_a_decision(
    client: TestClient, db_session: Session
) -> None:
    """An unbounded list of completed effects buries the handful blocking a run, so
    ``in_progress`` is the default rather than everything."""
    _stuck_effect(db_session, "eff-stuck")
    db_session.add(
        CaliberEffectLedger(
            effect_key="eff-done",
            workflow_run_id="WFR-1",
            node_id="notify",
            status=COMPLETED,
            result={"status_code": 200},
        )
    )
    db_session.commit()

    response = client.get(EFFECTS)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [row["effect_key"] for row in data["effects"]] == ["eff-stuck"]
    # The available decisions are advertised, so a client does not have to hard-code
    # the vocabulary of a fail-closed control.
    assert set(data["resolutions"]) == {"skip", "retry"}


def test_listing_can_widen_to_any_status_and_filter_by_run(
    client: TestClient, db_session: Session
) -> None:
    _stuck_effect(db_session, "eff-a", run_id="WFR-1")
    _stuck_effect(db_session, "eff-b", run_id="WFR-2")

    every = client.get(EFFECTS, params={"status": "any"}).json()["data"]["effects"]
    assert len(every) == 2

    scoped = client.get(EFFECTS, params={"workflow_run_id": "WFR-2"}).json()["data"]["effects"]
    assert [row["effect_key"] for row in scoped] == ["eff-b"]


def test_a_non_integer_limit_is_a_400_not_a_500(client: TestClient) -> None:
    response = client.get(EFFECTS, params={"limit": "lots"})
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_resolving_skip_marks_the_effect_completed_and_audits_who_decided(
    client: TestClient, db_session: Session
) -> None:
    key = _stuck_effect(db_session)

    response = client.post(
        f"{EFFECTS}/{key}/resolve",
        json={"resolution": "skip", "reason": "confirmed in the provider dashboard"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == COMPLETED
    db_session.expire_all()
    assert db_session.get(CaliberEffectLedger, key).status == COMPLETED
    # Asserting an unverifiable fact about the outside world has to be attributable.
    entries = (
        db_session.query(CaliberAuditLog).filter(CaliberAuditLog.action == "resolve_effect").all()
    )
    assert len(entries) == 1
    assert entries[0].entity_id == key
    assert entries[0].details["resolution"] == "skip"
    assert "provider dashboard" in entries[0].details["reason"]


def test_resolving_retry_marks_the_effect_failed(client: TestClient, db_session: Session) -> None:
    key = _stuck_effect(db_session)

    response = client.post(f"{EFFECTS}/{key}/resolve", json={"resolution": "retry"})

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(CaliberEffectLedger, key).status == FAILED


def test_an_invalid_resolution_is_refused(client: TestClient, db_session: Session) -> None:
    key = _stuck_effect(db_session)
    response = client.post(f"{EFFECTS}/{key}/resolve", json={"resolution": "maybe"})
    assert response.status_code == 400
    assert "resolution must be one of" in response.json()["detail"]


def test_an_already_completed_effect_cannot_be_re_resolved(
    client: TestClient, db_session: Session
) -> None:
    """Otherwise the recovery tool becomes a way to rewrite a genuine effect record."""
    db_session.add(
        CaliberEffectLedger(
            effect_key="eff-done",
            workflow_run_id="WFR-1",
            node_id="charge",
            status=COMPLETED,
            result={"status_code": 200},
        )
    )
    db_session.commit()

    response = client.post(f"{EFFECTS}/eff-done/resolve", json={"resolution": "skip"})

    assert response.status_code == 400
    assert "only an indeterminate claim" in response.json()["detail"]


def test_an_unknown_effect_is_refused(client: TestClient) -> None:
    response = client.post(f"{EFFECTS}/eff-nope/resolve", json={"resolution": "skip"})
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_resolution_requires_admin_scope(client: TestClient, db_session: Session) -> None:
    """Reading a stuck effect is an on-call need; *deciding* it is an admin act,
    because the decision can silently accept a lost mutation."""
    key = _stuck_effect(db_session)

    response = client.post(
        f"{EFFECTS}/{key}/resolve",
        json={"resolution": "skip"},
        headers={"X-CALIBER-User": "@viewer"},
    )

    assert response.status_code == 403
    db_session.expire_all()
    assert db_session.get(CaliberEffectLedger, key).status == IN_PROGRESS


def test_listing_requires_operator_scope(client: TestClient) -> None:
    response = client.get(EFFECTS, headers={"X-CALIBER-User": "@viewer"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# L6 — the durable webhook dead-letter surface
# ---------------------------------------------------------------------------

DEAD_LETTERS = f"{PREFIX}/system/webhook-dead-letters"


def _dead_letter(
    session: Session,
    dead_letter_id: str = "WDL-1",
    *,
    kind: str = "exhausted",
    status: str = "open",
) -> str:
    from caliber.db.models import CaliberWebhookDeadLetter

    session.add(
        CaliberWebhookDeadLetter(
            dead_letter_id=dead_letter_id,
            url="https://broken.example/hook",
            event_type="run.completed",
            event={"type": "run.completed", "id": "R-1"},
            reason="receiver returned HTTP 500",
            attempts=3,
            kind=kind,
            status=status,
        )
    )
    session.commit()
    return dead_letter_id


def test_dead_letters_default_to_the_open_work_queue(
    client: TestClient, db_session: Session
) -> None:
    _dead_letter(db_session, "WDL-open")
    _dead_letter(db_session, "WDL-done", status="acknowledged")

    data = client.get(DEAD_LETTERS).json()["data"]

    assert [row["dead_letter_id"] for row in data["dead_letters"]] == ["WDL-open"]
    assert data["open_count"] == 1
    # The event body is retained for manual replay but not inlined into every list
    # read — this is an operations queue, not a data export.
    assert data["dead_letters"][0]["has_event"] is True
    assert "event" not in data["dead_letters"][0]


def test_dead_letters_can_be_filtered_by_loss_mode(client: TestClient, db_session: Session) -> None:
    """``exhausted`` means the receiver is broken; ``overflow`` means CALIBER shed
    load. Different diagnoses, so they must be separable."""
    _dead_letter(db_session, "WDL-x", kind="exhausted")
    _dead_letter(db_session, "WDL-o", kind="overflow")

    rows = client.get(DEAD_LETTERS, params={"kind": "overflow"}).json()["data"]["dead_letters"]

    assert [row["dead_letter_id"] for row in rows] == ["WDL-o"]


def test_acknowledging_keeps_the_evidence_and_records_who(
    client: TestClient, db_session: Session
) -> None:
    """Acknowledged rather than deleted: the row is evidence about what a downstream
    system was never told, so an operator must not be able to erase it."""
    from caliber.db.models import CaliberWebhookDeadLetter

    key = _dead_letter(db_session)

    response = client.post(f"{DEAD_LETTERS}/{key}/acknowledge", json={"note": "replayed by hand"})

    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = db_session.get(CaliberWebhookDeadLetter, key)
    assert row is not None, "acknowledging must not delete the record"
    assert row.status == "acknowledged"
    assert row.acknowledged_by == "@test"
    assert "replayed by hand" in row.reason

    entries = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "acknowledge_webhook_dead_letter")
        .all()
    )
    assert len(entries) == 1


def test_acknowledging_an_unknown_dead_letter_is_404(client: TestClient) -> None:
    response = client.post(f"{DEAD_LETTERS}/WDL-nope/acknowledge", json={})
    assert response.status_code == 404


def test_dead_letters_require_operator_scope(client: TestClient) -> None:
    assert client.get(DEAD_LETTERS, headers={"X-CALIBER-User": "@viewer"}).status_code == 403
