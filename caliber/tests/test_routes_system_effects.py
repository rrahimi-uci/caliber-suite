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

import itertools
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectLedger,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
)
from caliber.effects.records import COMMITTED, RECONCILED, UNKNOWN
from caliber.ownership import release
from caliber.workflows.effect_ledger import COMPLETED, FAILED, IN_PROGRESS
from tests.effect_helpers import leased
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _proposal
from tests.test_effect_records import ATTEMPT, OWNER, _move
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


# ---------------------------------------------------------------- the operator reconciliation view (B05.6, #751)


_LEGACY_ROW_KEYS = {
    "effect_key",
    "workflow_run_id",
    "node_id",
    "status",
    "error",
    "claimed_at",
    "completed_at",
    "has_result",
    "source",
    "source_version",
    "safe_actions",
}
_PROPOSAL_ROW_KEYS = {
    "source",
    "source_version",
    "safe_actions",
    "proposal_id",
    "workflow_run_id",
    "project_id",
    "node_id",
    "occurrence_id",
    "logical_key",
    "connector",
    "connector_version",
    "effect_class",
    "resource",
    "intent_hash",
    "state",
    "version",
    "blocks_dependents",
    "attempt",
    "owner_id",
    "dispatch_token",
    "abandoned",
    "reason",
    "receipt_id",
    "dispatched_at",
    "unknown_at",
    "settled_at",
    "created_at",
}


def _proposals(session: Session) -> dict[str, str]:
    """Proposals of every kind in RUN-ep. The first three are dispatched under one lease; ``current`` is dispatched after the
    lease turned over (a new acquisition), so the first dispatched one is *abandoned* and ``current`` is not."""
    from caliber.effects.records import APPROVED
    from tests.test_effect_records import _at, _receipt

    out: dict[str, str] = {}
    out["abandoned"] = _proposal(session, occurrence="o-abandoned")
    out["unknown"] = _proposal(session, occurrence="o-unknown")
    _move(session, out["unknown"], UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
    out["reconciled"] = _proposal(session, occurrence="o-reconciled")
    _move(session, out["reconciled"], UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
    _move(session, out["reconciled"], RECONCILED, reason="found by key")
    out["committed"] = _proposal(session, occurrence="o-committed")
    _move(
        session,
        out["committed"],
        COMMITTED,
        owner_id=OWNER,
        attempt=ATTEMPT,
        receipt_id=_receipt(session, out["committed"]),
    )
    out["approved"] = _at(session, APPROVED, occurrence="o-approved")
    release(
        session, leased(session, OWNER)
    )  # the lease lapses; the next dispatch is a new acquisition
    out["current"] = _proposal(session, occurrence="o-current")
    session.commit()
    return out


def test_lists_legacy_and_proposal_kinds(client: TestClient, db_session: Session) -> None:
    """Two kinds of record, labelled apart, each with the actions that apply to *it* and no payload."""
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    _stuck_effect(db_session, "eff-stuck")
    db_session.add(
        CaliberEffectLedger(
            effect_key="eff-done", workflow_run_id="WFR-1", node_id="n", status=COMPLETED
        )
    )
    db_session.commit()
    ids = _proposals(db_session)

    data = client.get(EFFECTS).json()["data"]

    assert [r["effect_key"] for r in data["effects"]] == ["eff-stuck"]
    legacy = data["effects"][0]
    assert (legacy["source"], legacy["source_version"]) == ("legacy_ledger", "ledger-v0")
    assert legacy["safe_actions"] == ["resolve:skip", "resolve:retry"]
    assert set(legacy) == _LEGACY_ROW_KEYS and legacy["status"] == "in_progress"
    assert data["proposals_truncated"] is False and data["lease"]["expired"] is False

    by_id = {p["proposal_id"]: p for p in data["proposals"]}
    assert set(by_id) == {ids["abandoned"], ids["unknown"], ids["reconciled"], ids["current"]}, (
        "the open states only"
    )
    for row in by_id.values():
        assert (row["source"], row["source_version"]) == ("effect_proposal", "proposal-v1")
        assert row["blocks_dependents"] is True
        assert set(row) == _PROPOSAL_ROW_KEYS, "exactly these fields: no payload under any name"
        assert row["project_id"] == "PRJ-ep" and row["logical_key"] and row["intent_hash"]
    assert by_id[ids["reconciled"]]["abandoned"] is None
    assert by_id[ids["reconciled"]]["state"] == "reconciled"
    assert (by_id[ids["unknown"]]["state"], by_id[ids["unknown"]]["reason"]) == (
        "unknown",
        "timeout",
    )
    assert by_id[ids["unknown"]]["abandoned"] is None, "only a dispatched attempt can be abandoned"
    assert by_id[ids["abandoned"]]["abandoned"] is True
    assert by_id[ids["current"]]["abandoned"] is False
    # the one action is offered where it can do something: not for a dispatch of the lease that is still current
    assert {pid: row["safe_actions"] for pid, row in by_id.items()} == {
        ids["abandoned"]: ["reconcile"],
        ids["unknown"]: ["reconcile"],
        ids["reconciled"]: ["reconcile"],
        ids["current"]: [],
    }
    assert by_id[ids["abandoned"]]["dispatch_token"] < by_id[ids["current"]]["dispatch_token"]

    assert data["proposal_state"] == "open"
    sources = data["sources"]
    assert sources["legacy_ledger"]["settled_by"] == "operator"
    assert sources["effect_proposal"]["settled_by"] == "reconciler"
    assert sources["effect_proposal"]["safe_actions"] == ["reconcile"]
    assert set(data["resolutions"]) == {"skip", "retry"}


def test_listing_exposes_parked_runs_and_effect_resume_reason(
    client: TestClient, db_session: Session
) -> None:
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    run = db_session.get(CaliberWorkflowRun, "RUN-ep")
    assert run is not None
    checkpoint_id = "CP-parked-effect"
    run.status = "waiting_effect"
    run.current_node_id = "ask"
    run.error_code = "effect_reconciliation"
    run.error_summary = "waiting for authoritative effect reconciliation"
    run.summary = {
        "resume_checkpoint_id": checkpoint_id,
        "parked_effects": {
            "state": "waiting_effect",
            "node_id": "ask",
            "reason": "effect_dependency_unresolved",
            "resume_policy": "same_run_same_occurrence",
            "effects": [{"proposal_id": "prop-parked", "state": "unknown"}],
        },
    }
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            sequence=1,
            node_id="ask",
            state_blob={"kind": "effect_reconciliation", "node_id": "ask"},
        )
    )
    db_session.commit()

    response = client.get(EFFECTS, params={"source": "proposal", "workflow_run_id": "RUN-ep"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["parked_runs"] == [
        {
            "workflow_run_id": "RUN-ep",
            "workflow_id": "wf-ep",
            "status": "waiting_effect",
            "current_node_id": "ask",
            "error_code": "effect_reconciliation",
            "reason": "effect_dependency_unresolved",
            "resume_policy": "same_run_same_occurrence",
            "effects": [{"proposal_id": "prop-parked", "state": "unknown"}],
            "resume_checkpoint_id": checkpoint_id,
        }
    ]


def test_a_settled_legacy_claim_offers_no_action(client: TestClient, db_session: Session) -> None:
    db_session.add(
        CaliberEffectLedger(
            effect_key="eff-done", workflow_run_id="WFR-1", node_id="n", status=COMPLETED
        )
    )
    db_session.commit()
    rows = client.get(EFFECTS, params={"status": "any", "source": "legacy"}).json()["data"][
        "effects"
    ]
    assert [(r["effect_key"], r["safe_actions"]) for r in rows] == [("eff-done", [])]


def test_the_view_can_show_one_kind_or_any_proposal_state(
    client: TestClient, db_session: Session
) -> None:
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    _stuck_effect(db_session, "eff-stuck")
    ids = _proposals(db_session)

    only_legacy = client.get(EFFECTS, params={"source": "legacy"}).json()["data"]
    assert "proposals" not in only_legacy and len(only_legacy["effects"]) == 1
    only_proposals = client.get(EFFECTS, params={"source": "proposal"}).json()["data"]
    assert "effects" not in only_proposals and len(only_proposals["proposals"]) == 4

    every = client.get(EFFECTS, params={"source": "proposal", "proposal_state": "any"}).json()[
        "data"
    ]
    assert {p["proposal_id"] for p in every["proposals"]} == set(ids.values())
    committed = client.get(
        EFFECTS, params={"source": "proposal", "proposal_state": "committed"}
    ).json()["data"]["proposals"]
    assert [p["proposal_id"] for p in committed] == [ids["committed"]]
    assert committed[0]["blocks_dependents"] is False and committed[0]["receipt_id"]
    unknown = client.get(
        EFFECTS, params={"source": "proposal", "proposal_state": " UNKNOWN "}
    ).json()["data"]
    assert unknown["proposal_state"] == "unknown"
    assert [p["proposal_id"] for p in unknown["proposals"]] == [ids["unknown"]]


def test_the_view_filters_proposals_by_run_and_bounds_the_list(
    client: TestClient, db_session: Session
) -> None:
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    _proposals(db_session)
    other = client.get(EFFECTS, params={"workflow_run_id": "RUN-other"}).json()["data"]
    assert other["proposals"] == [] and other["effects"] == []
    here = client.get(EFFECTS, params={"workflow_run_id": "RUN-ep", "limit": 2}).json()["data"]
    assert len(here["proposals"]) == 2


def test_the_proposal_list_is_ordered_bounded_and_says_when_it_was_cut(
    client: TestClient, db_session: Session
) -> None:
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    _proposals(db_session)
    params = {"source": "proposal", "proposal_state": "any"}
    every = client.get(EFFECTS, params=params).json()["data"]
    keys = [(r["created_at"], r["proposal_id"]) for r in every["proposals"]]
    assert every["proposals_truncated"] is False and len(keys) == 6
    for (a_time, a_id), (b_time, b_id) in itertools.pairwise(keys):
        assert a_time > b_time or (a_time == b_time and a_id < b_id), "newest first, ties by id"
    cut = client.get(EFFECTS, params={**params, "limit": 2}).json()["data"]
    assert len(cut["proposals"]) == 2 and cut["proposals_truncated"] is True
    assert [r["proposal_id"] for r in cut["proposals"]] == [
        r["proposal_id"] for r in every["proposals"][:2]
    ]
    floor = client.get(EFFECTS, params={**params, "limit": 0}).json()["data"]
    assert len(floor["proposals"]) == 1, "a limit below one is one"
    ceiling = client.get(EFFECTS, params={**params, "limit": 100000}).json()["data"]
    assert len(ceiling["proposals"]) == 6 and ceiling["proposals_truncated"] is False


def test_abandoned_is_unknown_without_a_token_or_a_lease_and_the_lease_is_shown(
    client: TestClient, db_session: Session
) -> None:
    from sqlalchemy import text

    from caliber.ownership import current_lease, release

    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    ids = _proposals(db_session)
    url = {"source": "proposal", "proposal_state": "dispatched"}

    def rows() -> dict[str, Any]:
        found = client.get(EFFECTS, params=url).json()["data"]["proposals"]
        return {r["proposal_id"]: r for r in found}

    body = client.get(EFFECTS, params=url).json()["data"]
    assert body["lease"]["owner_id"] == OWNER and body["lease"]["expired"] is False
    assert body["lease"]["fencing_token"] == rows()[ids["current"]]["dispatch_token"]
    # a row from before the token was recorded proves nothing: neither abandoned nor not
    db_session.execute(
        text("UPDATE caliber_effect_proposals SET dispatch_token = NULL WHERE proposal_id = :p"),
        {"p": ids["abandoned"]},
    )
    db_session.commit()
    assert rows()[ids["abandoned"]]["abandoned"] is None
    # the lease lapses with nobody to replace it: not abandoned yet, and the lease says why
    lease = current_lease(db_session)
    assert lease is not None and release(db_session, lease)
    db_session.commit()
    lapsed = client.get(EFFECTS, params=url).json()["data"]
    assert lapsed["lease"]["expired"] is True
    assert {r["abandoned"] for r in lapsed["proposals"]} == {None, False}
    # no lease was ever acquired: nothing to compare against
    db_session.execute(text("DELETE FROM caliber_execution_owner"))
    db_session.commit()
    gone = client.get(EFFECTS, params=url).json()["data"]
    assert gone["lease"] is None and {r["abandoned"] for r in gone["proposals"]} == {None}


def test_a_proposal_filter_is_only_checked_when_proposals_are_asked_for(client: TestClient) -> None:
    assert (
        client.get(EFFECTS, params={"source": "legacy", "proposal_state": "x"}).status_code == 200
    )
    assert client.get(EFFECTS, params={"proposal_state": "x"}).status_code == 400


@pytest.mark.parametrize(
    "params",
    [
        {"source": "everything"},
        {"proposal_state": "done"},
        {"source": "proposal", "proposal_state": "x"},
    ],
)
def test_an_unknown_source_or_proposal_state_is_a_400(
    client: TestClient, params: dict[str, Any]
) -> None:
    assert client.get(EFFECTS, params=params).status_code == 400


def test_a_proposal_cannot_be_resolved_as_a_ledger_claim(
    client: TestClient, db_session: Session
) -> None:
    """The legacy ``resolve`` is for ledger claims only: a proposal is settled by the reconciler, and the refusal changes nothing."""
    from caliber.effects.records import proposal_state

    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    ids = _proposals(db_session)
    before = proposal_state(db_session, ids["unknown"])
    refused = client.post(
        f"{EFFECTS}/{ids['unknown']}/resolve",
        json={"resolution": "skip", "reason": "operator says so"},
    )
    assert refused.status_code == 400
    db_session.expire_all()
    assert proposal_state(db_session, ids["unknown"]) == before
    audited = db_session.query(CaliberAuditLog).filter(CaliberAuditLog.action == "resolve_effect")
    assert audited.count() == 0


def test_resolving_a_legacy_claim_is_unaffected_by_proposals(
    client: TestClient, db_session: Session
) -> None:
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    _proposals(db_session)
    key = _stuck_effect(db_session, "eff-stuck")
    response = client.post(
        f"{EFFECTS}/{key}/resolve", json={"resolution": "skip", "reason": "confirmed"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == COMPLETED


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


def test_replaying_a_dead_letter_redelivers_and_marks_it(
    client: TestClient, db_session: Session
) -> None:
    """Closes "no automatic redelivery from the durable record". Storing the full
    event was pointless without something able to send it again."""
    from caliber.db.models import CaliberWebhookDeadLetter

    key = _dead_letter(db_session, "WDL-replay")
    sent: list[tuple[str, dict]] = []

    class _Dispatcher:
        # ``is_enabled`` is part of the contract: the route refuses to replay when
        # dispatch is off, because the event could not be signed or delivered anyway.
        is_enabled = True

        def replay_event(self, url, event):
            sent.append((url, event))
            return True, "delivered"

    client.app.state.webhook_dispatcher = _Dispatcher()

    response = client.post(f"{DEAD_LETTERS}/{key}/replay")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["delivered"] is True
    assert sent and sent[0][1]["type"] == "run.completed"
    db_session.expire_all()
    row = db_session.get(CaliberWebhookDeadLetter, key)
    assert row.status == "replayed"
    assert "replay by @test" in row.reason


def test_a_failed_replay_leaves_the_row_open(client: TestClient, db_session: Session) -> None:
    """A failed recovery must never look like a completed one."""
    from caliber.db.models import CaliberWebhookDeadLetter

    key = _dead_letter(db_session, "WDL-badreplay")

    class _Dispatcher:
        is_enabled = True

        def replay_event(self, url, event):
            return False, "receiver returned HTTP 500"

    client.app.state.webhook_dispatcher = _Dispatcher()

    response = client.post(f"{DEAD_LETTERS}/{key}/replay")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["delivered"] is False
    db_session.expire_all()
    row = db_session.get(CaliberWebhookDeadLetter, key)
    assert row.status == "open", "a failed replay must not clear the work item"
    assert "HTTP 500" in row.reason


def test_a_row_already_being_replayed_is_refused(client: TestClient, db_session: Session) -> None:
    """The row is claimed before sending, so a second caller cannot deliver twice.

    Without the claim both callers read ``open``, both POST, and the operator gets two
    deliveries for one request — the opposite of what a recovery tool should do.

    The claimed state is asserted directly rather than by driving two real requests: a
    nested in-handler call would test the test harness's re-entrancy, not the database
    predicate that actually arbitrates.
    """
    from caliber.db.models import CaliberWebhookDeadLetter

    key = _dead_letter(db_session, "WDL-claimed")
    db_session.get(CaliberWebhookDeadLetter, key).status = "replaying"
    db_session.commit()

    class _Dispatcher:
        is_enabled = True

        def replay_event(self, url, event):  # pragma: no cover - must not be reached
            raise AssertionError("a claimed row must not be replayed again")

    client.app.state.webhook_dispatcher = _Dispatcher()

    response = client.post(f"{DEAD_LETTERS}/{key}/replay")

    assert response.status_code == 409, response.text
    assert "another replay may be in flight" in response.json()["detail"]


def test_a_successful_replay_leaves_no_claim_behind(
    client: TestClient, db_session: Session
) -> None:
    """The claim is transient: success settles the row as ``replayed``, and failure
    releases it back to ``open`` so it stays a work item rather than becoming
    permanently unreplayable."""
    from caliber.db.models import CaliberWebhookDeadLetter

    key = _dead_letter(db_session, "WDL-settle")

    class _Dispatcher:
        is_enabled = True

        def replay_event(self, url, event):
            return True, "delivered"

    client.app.state.webhook_dispatcher = _Dispatcher()
    assert client.post(f"{DEAD_LETTERS}/{key}/replay").status_code == 200
    db_session.expire_all()
    assert db_session.get(CaliberWebhookDeadLetter, key).status == "replayed"


def test_replay_is_refused_when_dispatch_is_disabled(
    client: TestClient, db_session: Session
) -> None:
    """The app always installs a dispatcher object, so checking only that it exists let
    an operator reach replay on a deployment with no URLs or no signing secret."""
    key = _dead_letter(db_session, "WDL-disabled")

    class _Dispatcher:
        is_enabled = False

        def replay_event(self, url, event):  # pragma: no cover - must not be reached
            raise AssertionError("replay must not be attempted while dispatch is disabled")

    client.app.state.webhook_dispatcher = _Dispatcher()

    response = client.post(f"{DEAD_LETTERS}/{key}/replay")

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


# ---------------------------------------------------------------- the signed receipt callback (B05.4b, #725)

import json
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from caliber.config import CaliberConfig
from caliber.db.models import CaliberEffectReceipt, CaliberInboxEvent
from caliber.effects.callbacks import sign
from caliber.effects.connectors.document_request import submit_config, submit_input
from caliber.effects.records import (
    DISPATCHED,
    proposal_state,
    proposal_view,
)
from caliber.effects.records import FAILED as PROPOSAL_FAILED
from caliber.integrations.openapi.executor import _send_raw
from caliber.routes.system_effects import RECEIPT_CALLBACK_PATH
from caliber.server import create_app

SECRET = "callback-secret-for-tests"  # gitleaks:allow
SECRET_ENV = "EFFECT_CALLBACK_TEST_SECRET"  # gitleaks:allow
CASE = "MORT-2026-000184"
PROJECT = "PRJ-ep"
CONNECTOR = "document-request"


@pytest.fixture(scope="module")
def _downstream_process(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from tests.egi_scenarios.conftest import Downstream

    handle = Downstream(tmp_path_factory.mktemp("downstream"))
    try:
        handle.start()
        yield handle
    finally:
        handle.close()


@pytest.fixture
def downstream(_downstream_process: Any) -> Iterator[Any]:
    from caliber.egress import EgressPolicy
    from caliber.integrations.openapi.executor import bind_egress_policy

    _downstream_process.reset()
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield _downstream_process
    bind_egress_policy(None)


def _callback_app(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    bindings: list[dict[str, str]],
) -> Iterator[TestClient]:
    monkeypatch.setenv(SECRET_ENV, SECRET)
    config = app_config.model_copy(
        update={
            "effect_callback_connectors": json.dumps(bindings),
            "egress_allowed_hosts": "127.0.0.1",
        }
    )
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(
        app
    ) as test_client:  # no CALIBER identity: the signature is the only credential
        yield test_client


@pytest.fixture
def callback_client(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": downstream.base_url,
    }
    yield from _callback_app(app_config, engine, session_factory, monkeypatch, [binding])


def _unknown_proposal(
    factory: sessionmaker[Session],
    downstream: Any,
    *,
    occurrence: str = "occ-1",
    expected: str = "1",
    dispatched: bool = True,
) -> tuple[str, str]:
    """A proposal whose dispatch response was lost (``unknown``) and, if ``dispatched``, whose request the downstream holds."""
    with factory() as session:
        proposal_id = _proposal(session, occurrence=occurrence, expected=expected)
        key = proposal_view(session, proposal_id).logical_key
        _move(session, proposal_id, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
        session.commit()
    if dispatched:
        _send_raw(
            execution_config=submit_config(downstream.base_url),
            input_schema=None,
            input_data=submit_input(
                case_id=CASE,
                idempotency_key=key,
                expected_resource_version=int(expected),
                body={"document_type": "pay_statement"},
            ),
        )
    return proposal_id, key


def _call(
    client: TestClient,
    key: str,
    *,
    connector: str = CONNECTOR,
    project: str = PROJECT,
    delivery: str = "delivery-1",
    timestamp: int | None = None,
    secret: str = SECRET,
    body: bytes | None = None,
    signed_body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    raw = json.dumps({"idempotency_key": key}).encode() if body is None else body
    stamp = int(time.time()) if timestamp is None else timestamp
    signature = sign(
        secret, connector, project, stamp, delivery, raw if signed_body is None else signed_body
    )
    sent = {
        "X-Caliber-Connector": connector,
        "X-Caliber-Project": project,
        "X-Caliber-Timestamp": str(stamp),
        "X-Caliber-Delivery": delivery,
        "X-Caliber-Signature": signature,
        "Content-Type": "application/json",
    }
    sent.update(headers or {})
    return client.post(RECEIPT_CALLBACK_PATH, content=raw, headers=sent)


def _count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def test_callback_commits_after_query(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    """The callback names an idempotency key; the platform asks the downstream itself, records the verified receipt and
    commits (an ``unknown`` proposal goes through ``reconciled``)."""
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    response = _call(callback_client, key)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "committed" and data["receipt_id"]
    with session_factory() as session:
        state = proposal_state(session, proposal_id)
        assert (state.state, state.receipt_id) == (COMMITTED, data["receipt_id"])
        assert state.version == 6  # approved 2, dispatched 3, unknown 4, reconciled 5, committed 6
        audit = session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "effect_receipt_callback")
        ).scalar_one()
        assert audit.actor == f"connector:{CONNECTOR}" and audit.entity_id == proposal_id
    assert _count(session_factory, CaliberEffectReceipt) == 1
    assert downstream.mutations() == 1, "the callback sent nothing: it only asked"


def test_callback_wakes_a_parked_run_after_commit(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    checkpoint_id = "CP-callback-parked"
    with session_factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None
        run.status = "waiting_effect"
        run.current_node_id = "ask"
        run.error_code = "effect_reconciliation"
        run.error_summary = "waiting for authoritative effect reconciliation"
        run.summary = {
            "resume_checkpoint_id": checkpoint_id,
            "parked_effects": {
                "state": "waiting_effect",
                "node_id": "ask",
                "reason": "effect_dependency_unresolved",
                "resume_policy": "same_run_same_occurrence",
                "effects": [{"proposal_id": proposal_id, "state": "unknown"}],
            },
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id="RUN-ep",
                project_id="PRJ-ep",
                sequence=1,
                node_id="ask",
                state_blob={
                    "kind": "effect_reconciliation",
                    "node_id": "ask",
                    "input_by_port": {"input": "hello"},
                    "effects": [{"proposal_id": proposal_id, "state": "unknown"}],
                },
            )
        )
        session.commit()

    response = _call(callback_client, key, delivery="delivery-parked")
    assert response.status_code == 200, response.text
    with session_factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None and run.status == "queued"
        assert run.error_code is None and run.error_summary is None
        assert dict(run.summary or {})["parked_effects"]["wake_reason"] == (
            "all_effect_dependencies_settled"
        )
        event_types = [
            event.event_type
            for event in session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == "RUN-ep")
            .all()
        ]
        assert "workflow.run.effect_resumed" in event_types


def test_callback_is_idempotent(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    first = _call(callback_client, key, delivery="delivery-A")
    again = _call(callback_client, key, delivery="delivery-A")  # the same delivery, retried
    other = _call(callback_client, key, delivery="delivery-B")  # another delivery of the same news
    assert [r.status_code for r in (first, again, other)] == [200, 200, 200]
    assert first.json()["data"]["status"] == "committed"
    assert again.json()["data"]["status"] == "duplicate", (
        "the same delivery: answered from the inbox, without a query"
    )
    assert other.json()["data"]["status"] == "already_committed"
    assert _count(session_factory, CaliberEffectReceipt) == 1
    assert _count(session_factory, CaliberInboxEvent) == 1, "one delivery consumed, once"


def test_callback_without_downstream_record_stays_unknown(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    """The downstream holds nothing under the key (yet): not a receipt, not a failure, and the delivery is not consumed, so
    the same delivery can be sent again and will commit once the downstream has it."""
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream, dispatched=False)
    early = _call(callback_client, key, delivery="delivery-1")
    assert early.status_code == 202 and early.json()["data"]["status"] == "unknown"
    with session_factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN
    assert (
        _count(session_factory, CaliberEffectReceipt) == 0
        and _count(session_factory, CaliberInboxEvent) == 0
    )
    _send_raw(
        execution_config=submit_config(downstream.base_url),
        input_schema=None,
        input_data=submit_input(
            case_id=CASE,
            idempotency_key=key,
            expected_resource_version=1,
            body={"document_type": "pay_statement"},
        ),
    )
    late = _call(callback_client, key, delivery="delivery-1")  # the very same delivery, retried
    assert late.status_code == 200 and late.json()["data"]["status"] == "committed"


def test_the_callback_commits_a_reconciled_proposal_without_going_through_reconciled_again(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    with session_factory() as session:
        _move(session, proposal_id, RECONCILED, reason="found by key")
        session.commit()
    response = _call(callback_client, key)
    assert response.status_code == 200 and response.json()["data"]["status"] == "committed"
    with session_factory() as session:
        assert proposal_state(session, proposal_id).version == 6


@pytest.mark.parametrize(
    "tamper",
    [
        {"secret": "another-secret-value"},
        {"connector": "other-connector"},
        {"project": "PRJ-other"},
        {"timestamp": 1},
        {"timestamp": 4_000_000_000},
        {"signed_body": b'{"idempotency_key": "something-else"}'},
        {"headers": {"X-Caliber-Signature": "v1=" + "0" * 64}},
        {"headers": {"X-Caliber-Signature": "garbage"}},
        {"headers": {"X-Caliber-Signature": ""}},
        {"headers": {"X-Caliber-Timestamp": "soon"}},
        {"headers": {"X-Caliber-Delivery": ""}},
        {"headers": {"X-Caliber-Connector": ""}},
        {"headers": {"X-Caliber-Project": ""}},
    ],
)
def test_every_authentication_failure_is_the_same_401(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    tamper: dict[str, Any],
) -> None:
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    response = _call(callback_client, key, **tamper)
    assert response.status_code == 401 and response.json() == {"detail": "callback rejected"}
    with session_factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN
    assert downstream.mutations() == 1 and _count(session_factory, CaliberEffectReceipt) == 0


def test_a_request_without_any_signature_headers_is_the_same_401(
    callback_client: TestClient,
) -> None:
    response = callback_client.post(RECEIPT_CALLBACK_PATH, content=b"{}")
    assert response.status_code == 401 and response.json() == {"detail": "callback rejected"}


def test_a_connector_that_is_not_bound_or_whose_secret_cannot_be_read_is_refused(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": "env://EFFECT_CALLBACK_NOT_SET",
        "base_url": downstream.base_url,
    }
    for bindings in (
        [],
        [binding],
        [{**binding, "connector": "another-connector", "secret_source": f"env://{SECRET_ENV}"}],
    ):
        for client in _callback_app(app_config, engine, session_factory, monkeypatch, bindings):
            response = _call(client, key)
            assert response.status_code == 401 and response.json() == {
                "detail": "callback rejected"
            }
    assert _count(session_factory, CaliberEffectReceipt) == 0


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"k"',
        b"{}",
        b'{"idempotency_key": 5}',
        b'{"idempotency_key": ""}',
        b'{"idempotency_key": " k"}',
        b'{"idempotency_key": "k", "extra": 1}',
        b'{"idempotency_key": "cl\\u00e9"}',
        b'{"idempotency_key": "' + b"k" * 256 + b'"}',
        b'{"idempotency_key": "a\\nb"}',
        b"\xff\xfe",
    ],
)
def test_an_authenticated_callback_with_a_malformed_body_is_a_400_and_changes_nothing(
    callback_client: TestClient, session_factory: sessionmaker[Session], body: bytes
) -> None:
    _seed_run(session_factory)
    response = _call(callback_client, "ignored", body=body)
    assert response.status_code == 400
    assert (
        _count(session_factory, CaliberEffectReceipt) == 0
        and _count(session_factory, CaliberInboxEvent) == 0
    )


def test_an_oversized_body_is_refused_before_it_is_read_into_memory(
    callback_client: TestClient,
) -> None:
    response = _call(callback_client, "k", body=b'{"idempotency_key": "' + b"k" * 8000 + b'"}')
    assert response.status_code == 413


def test_a_key_that_belongs_to_another_project_or_connector_is_simply_not_found(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    """No answer may tell the caller whether a key exists somewhere it is not allowed to see."""
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    with session_factory() as session:  # the same key's run moves to another project
        from caliber.db.models import CaliberProject, CaliberWorkflowRun

        session.add(CaliberProject(project_id="PRJ-elsewhere", name="Elsewhere", owner="@t"))
        session.flush()
        session.get(CaliberWorkflowRun, "RUN-ep").project_id = "PRJ-elsewhere"  # type: ignore[union-attr]
        session.commit()
    foreign = _call(callback_client, key)
    nothing = _call(callback_client, "a-key-nobody-has")
    assert foreign.status_code == nothing.status_code == 404
    assert foreign.json() == nothing.json() == {"detail": "no such proposal"}


def test_a_connector_cannot_report_on_another_connectors_proposal_in_the_same_project(
    callback_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    from caliber.effects.records import Intent, record_proposal

    _seed_run(session_factory)
    with session_factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id="RUN-ep",
            node_id="ask",
            occurrence_id="occ-other-connector",
            intent=Intent(
                connector="some-other-connector",
                connector_version="1",
                effect_class="read",
                resource="things/1",
                args={},
            ),
        )
        session.commit()
    response = _call(callback_client, recorded.logical_key)
    assert (response.status_code, response.json()) == (404, {"detail": "no such proposal"})


def test_a_proposal_that_is_not_waiting_on_a_receipt_is_answered_without_a_query(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    with session_factory() as session:
        dispatched = _proposal(session, occurrence="occ-d", expected="1")
        failed = _proposal(session, occurrence="occ-f", expected="1")
        _move(session, failed, PROPOSAL_FAILED, owner_id=OWNER, attempt=ATTEMPT, reason="rejected")
        keys = {
            n: proposal_view(session, p).logical_key
            for n, p in (("dispatched", dispatched), ("failed", failed))
        }
        session.commit()
    in_flight = _call(callback_client, keys["dispatched"])
    assert (in_flight.status_code, in_flight.json()["data"]["status"]) == (202, "in_flight")
    refused = _call(callback_client, keys["failed"])
    assert (refused.status_code, refused.json()) == (409, {"reason": "proposal_failed"})
    with session_factory() as session:
        assert proposal_state(session, dispatched).state == DISPATCHED


def test_a_proposal_that_was_never_dispatched_is_refused(
    callback_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    from caliber.effects.connectors.document_request import build_intent
    from caliber.effects.records import record_proposal

    _seed_run(session_factory)
    with session_factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id="RUN-ep",
            node_id="ask",
            occurrence_id="occ-new",
            intent=build_intent(
                case_id=CASE,
                document_type="pay_statement",
                expected_resource_version=1,
                effect_class="read",
            ),
        )
        session.commit()
    response = _call(callback_client, recorded.logical_key)
    assert (response.status_code, response.json()) == (409, {"reason": "not_dispatched"})


def test_a_downstream_that_cannot_be_asked_is_a_retryable_502_and_consumes_nothing(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": f"http://127.0.0.1:{closed}",
    }
    for client in _callback_app(app_config, engine, session_factory, monkeypatch, [binding]):
        response = _call(client, key)
        assert response.status_code == 502 and response.json() == {"status": "lookup_failed"}
    assert (
        _count(session_factory, CaliberEffectReceipt) == 0
        and _count(session_factory, CaliberInboxEvent) == 0
    )


def test_a_downstream_record_that_is_not_this_proposals_effect_stays_unknown(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream, dispatched=False)
    _send_raw(  # the downstream holds a different document type under the key
        execution_config=submit_config(downstream.base_url),
        input_schema=None,
        input_data=submit_input(
            case_id=CASE,
            idempotency_key=key,
            expected_resource_version=1,
            body={"document_type": "bank_statement"},
        ),
    )
    response = _call(callback_client, key)
    assert response.status_code == 202 and response.json()["data"]["status"] == "unknown"
    with session_factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN


def test_the_same_delivery_id_with_a_different_body_is_an_inbox_payload_mismatch(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    _, first_key = _unknown_proposal(session_factory, downstream, occurrence="occ-1", expected="1")
    _, second_key = _unknown_proposal(session_factory, downstream, occurrence="occ-2", expected="2")
    assert _call(callback_client, first_key, delivery="delivery-X").status_code == 200
    clash = _call(callback_client, second_key, delivery="delivery-X")
    assert (clash.status_code, clash.json()) == (409, {"reason": "inbox_payload_mismatch"})
    with session_factory() as session:
        assert _count(session_factory, CaliberEffectReceipt) == 1
    assert _call(callback_client, second_key, delivery="delivery-Y").status_code == 200


def test_two_callbacks_racing_for_one_proposal_commit_it_once(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both callbacks read the proposal while it was ``unknown``; the second's transaction then finds it already committed
    (another delivery) or finds its own delivery id already consumed (the same delivery): neither records a second receipt."""
    from caliber.routes import system_effects

    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    with session_factory() as session:
        stale_view = system_effects._resolve_callback_proposal(
            session,
            system_effects.parse_bindings(
                callback_client.app.state.config.effect_callback_connectors
            )[0],
            key,  # type: ignore[attr-defined]
        )
    first = _call(callback_client, key, delivery="delivery-1")
    assert first.status_code == 200
    monkeypatch.setattr(
        system_effects, "_resolve_callback_proposal", lambda *args, **kwargs: stale_view
    )
    same_delivery = _call(callback_client, key, delivery="delivery-1")
    other_delivery = _call(callback_client, key, delivery="delivery-2")
    assert same_delivery.json()["data"]["status"] == "duplicate"
    assert other_delivery.json()["data"]["status"] == "committed"
    assert other_delivery.json()["data"]["receipt_id"] == first.json()["data"]["receipt_id"]
    assert _count(session_factory, CaliberEffectReceipt) == 1


def test_a_failed_transaction_is_retryable_and_never_marks_the_delivery_consumed(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.effects.records import ProposalError
    from caliber.routes import system_effects

    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    real = system_effects._commit_from_callback

    def failing(*args: Any, **kwargs: Any) -> str:
        real(*args, **kwargs)  # the work is done inside the transaction...
        raise ProposalError("receipt_conflict", "refused after the writes")  # ...and then refused

    monkeypatch.setattr(system_effects, "_commit_from_callback", failing)
    refused = _call(callback_client, key, delivery="delivery-R")
    assert (refused.status_code, refused.json()) == (409, {"reason": "receipt_conflict"})
    with session_factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN
    assert (
        _count(session_factory, CaliberEffectReceipt) == 0
        and _count(session_factory, CaliberInboxEvent) == 0
    )
    monkeypatch.setattr(system_effects, "_commit_from_callback", real)
    retried = _call(callback_client, key, delivery="delivery-R")
    assert retried.status_code == 200 and retried.json()["data"]["status"] == "committed"


def test_the_callback_needs_no_session_and_the_settings_page_hides_its_secrets(
    callback_client: TestClient,
) -> None:
    from tests.conftest import DEFAULT_TEST_USER
    from tests.workflow_helpers import PREFIX

    assert callback_client.get(f"{PREFIX}/system/effects").status_code == 401, (
        "no CALIBER identity here"
    )
    response = callback_client.get(
        f"{PREFIX}/settings/runtime", headers={"X-CALIBER-User": DEFAULT_TEST_USER}
    )
    assert response.status_code == 200, response.text
    text = response.text
    assert "1 connector binding(s)" in text
    for secret in (SECRET_ENV, "127.0.0.1", SECRET, "env://"):
        assert secret not in text


def test_the_callback_is_exempt_from_csrf_and_rate_limiting(
    tmp_path: Any,
    engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CSRF and a one-request-a-minute rate limit genuinely on, a signed call with no CSRF token and no CALIBER identity
    still reaches the handler (here: refused as unsigned, a 401 from the handler, not a 403 or 429 from a middleware)."""
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'csrf.db'}")
    monkeypatch.setenv("CALIBER_CSRF_ENABLED", "true")
    monkeypatch.setenv("CALIBER_CSRF_SIGNING_SECRET_ENV", "CSRF_TEST_KEY")
    monkeypatch.setenv("CSRF_TEST_KEY", "csrf-test-secret")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_BURST", "1")
    app = create_app(config=CaliberConfig.load())
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as client:
        statuses = {client.post(RECEIPT_CALLBACK_PATH, content=b"{}").status_code for _ in range(5)}
        other = client.post(
            "/ajax-api/2.0/mlflow/caliber/system/effects/x/resolve", json={}
        ).status_code
    assert statuses == {401}, "the handler answered every time"
    assert other in (403, 429), (
        "an ordinary POST is stopped by the middleware that exempts the callback"
    )


def test_a_delivery_id_belongs_to_one_binding_and_never_collides_across_projects(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    from caliber.effects.callbacks import CallbackBinding
    from caliber.events.inbox import process_once
    from caliber.routes import system_effects

    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    elsewhere = CallbackBinding(CONNECTOR, "PRJ-another", "env://X", "http://x.example.com")
    here = CallbackBinding(CONNECTOR, PROJECT, "env://X", "http://x.example.com")
    assert system_effects._inbox_source(elsewhere) != system_effects._inbox_source(here)
    assert len(system_effects._inbox_source(here)) == 32
    with (
        session_factory() as session
    ):  # another project's connector already consumed delivery id "1" for its own key
        process_once(
            session,
            consumer="effect-callback",
            source=system_effects._inbox_source(elsewhere),
            event_id="1",
            payload_hash=system_effects._callback_payload_hash(elsewhere, "some-other-key"),
            apply=lambda: None,
        )
        session.commit()
    response = _call(callback_client, key, delivery="1")
    assert response.status_code == 200 and response.json()["data"]["status"] == "committed"


def test_a_timestamp_that_is_not_spelled_canonically_is_refused(
    callback_client: TestClient, session_factory: sessionmaker[Session], downstream: Any
) -> None:
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    now = int(time.time())
    padded = "0" + str(now)
    response = _call(callback_client, key, timestamp=now, headers={"X-Caliber-Timestamp": padded})
    assert response.status_code == 401


def test_a_secret_that_is_too_short_to_trust_is_refused(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": downstream.base_url,
    }
    for client in _callback_app(app_config, engine, session_factory, monkeypatch, [binding]):
        monkeypatch.setenv(SECRET_ENV, "short")
        response = _call(client, key, secret="short")
        assert response.status_code == 401 and _count(session_factory, CaliberEffectReceipt) == 0


def test_a_project_id_that_differs_only_in_case_or_a_run_with_no_project_is_not_found(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.db.models import CaliberWorkflowRun

    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    upper = {
        "connector": CONNECTOR,
        "project_id": PROJECT.upper(),
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": downstream.base_url,
    }
    for client in _callback_app(app_config, engine, session_factory, monkeypatch, [upper]):
        assert _call(client, key, project=PROJECT.upper()).status_code == 404
    with session_factory() as session:
        session.get(CaliberWorkflowRun, "RUN-ep").project_id = None  # type: ignore[union-attr]
        session.commit()
    bound = {**upper, "project_id": PROJECT}
    for client in _callback_app(app_config, engine, session_factory, monkeypatch, [bound]):
        assert _call(client, key).status_code == 404, "a run with no project belongs to no binding"


def test_a_downstream_answer_that_cannot_be_digested_is_not_a_server_error(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import system_effects

    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    real = system_effects.execute_openapi_http_tool

    def with_nan(**kwargs: Any) -> Any:
        found = real(**kwargs)
        found["json"] = {
            **found["json"],
            "latency": float("nan"),
        }  # Python parses NaN; it is not JSON
        return found

    monkeypatch.setattr(system_effects, "execute_openapi_http_tool", with_nan)
    response = _call(callback_client, key)
    assert response.status_code == 202 and response.json()["data"]["status"] == "unknown"
    with session_factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN


def test_a_callback_that_loses_a_race_to_the_commit_is_told_the_effect_is_committed(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.effects.receipts import RECEIPT_STATE
    from caliber.effects.records import ProposalError
    from caliber.routes import system_effects

    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    with session_factory() as session:
        view = system_effects._resolve_callback_proposal(
            session,
            system_effects.parse_bindings(
                callback_client.app.state.config.effect_callback_connectors
            )[0],
            key,  # type: ignore[attr-defined]
        )
    assert view is not None and view.state == UNKNOWN, (
        "the loser read it before the winner committed"
    )
    assert _call(callback_client, key, delivery="delivery-1").status_code == 200  # the winner
    monkeypatch.setattr(
        system_effects, "_resolve_callback_proposal", lambda *a, **k: view
    )  # the loser read it unknown

    reached: list[bool] = []

    def lost(*args: Any, **kwargs: Any) -> Any:
        reached.append(True)
        raise ProposalError(RECEIPT_STATE, "committed meanwhile")

    monkeypatch.setattr(system_effects, "_commit_from_callback", lost)
    response = _call(callback_client, key, delivery="delivery-2")
    assert reached, "the refused commit path was exercised, not an earlier already-committed answer"
    assert (response.status_code, response.json()["data"]["status"]) == (200, "already_committed")
    assert _count(session_factory, CaliberEffectReceipt) == 1


def test_a_binding_has_a_bounded_number_of_lookups_in_flight(
    callback_client: TestClient,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.effects.callbacks import CallbackBinding
    from caliber.routes import system_effects

    _seed_run(session_factory)
    proposal_id, key = _unknown_proposal(session_factory, downstream)
    binding = CallbackBinding(CONNECTOR, PROJECT, f"env://{SECRET_ENV}", downstream.base_url)
    slot = system_effects._lookup_slot(binding)
    held = [slot.acquire(blocking=False) for _ in range(system_effects._MAX_CONCURRENT_LOOKUPS)]
    assert all(held)
    try:
        busy = _call(callback_client, key)
        assert (busy.status_code, busy.json()) == (503, {"status": "busy"})
        with session_factory() as session:
            assert proposal_state(session, proposal_id).state == UNKNOWN
    finally:
        for _ in held:
            slot.release()
    assert _call(callback_client, key).status_code == 200, "and the slots are free again"


def test_the_slot_is_released_when_the_downstream_cannot_be_asked(
    app_config: CaliberConfig,
    engine: Any,
    session_factory: sessionmaker[Session],
    downstream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from caliber.effects.callbacks import CallbackBinding
    from caliber.routes import system_effects

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    _seed_run(session_factory)
    _, key = _unknown_proposal(session_factory, downstream)
    base = f"http://127.0.0.1:{closed}"
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": base,
    }
    for client in _callback_app(app_config, engine, session_factory, monkeypatch, [binding]):
        for _ in range(system_effects._MAX_CONCURRENT_LOOKUPS + 2):
            assert _call(client, key).status_code == 502, "every failure gives its slot back"
    assert system_effects._lookup_slot(CallbackBinding(CONNECTOR, PROJECT, "x", base)).acquire(
        blocking=False
    )


def _race_callbacks(
    factory: sessionmaker[Session], config: CaliberConfig, key: str, *, shared_delivery: bool
) -> list[tuple[int, Any]]:
    """Eight callbacks for one key, released together; each answer as ``(status code, body)``."""
    import threading

    from caliber.routes import system_effects

    barrier = threading.Barrier(8)
    answers: list[tuple[int, Any]] = []
    lock = threading.Lock()

    def call(index: int) -> None:
        delivery = "delivery-shared" if shared_delivery else f"delivery-{index}"
        raw = json.dumps({"idempotency_key": key}).encode()
        stamp = int(time.time())
        headers = {
            "x-caliber-connector": CONNECTOR,
            "x-caliber-project": PROJECT,
            "x-caliber-timestamp": str(stamp),
            "x-caliber-delivery": delivery,
            "x-caliber-signature": sign(SECRET, CONNECTOR, PROJECT, stamp, delivery, raw),
        }
        barrier.wait(10)
        response = system_effects._process_receipt_callback(factory, config, headers, raw)
        with lock:
            answers.append((response.status_code, json.loads(bytes(response.body))))

    threads = [threading.Thread(target=call, args=(i,), daemon=True) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return answers


def test_concurrent_callbacks_commit_a_proposal_once_on_postgresql(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, downstream: Any
) -> None:
    """Eight callbacks race for one ``unknown`` proposal on PostgreSQL (READ COMMITTED): one commit, one receipt, and every
    caller is told the truth (never a failure for an effect that committed; the callers over the per-binding lookup bound are
    told to come back, which is not a wrong answer)."""
    from tests.dialect_helpers import POSTGRES_TEST_URL
    from tests.test_event_outbox import _database

    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")
    monkeypatch.setenv(SECRET_ENV, SECRET)
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": downstream.base_url,
    }
    config = CaliberConfig.load(
        environ={
            "CALIBER_EFFECT_CALLBACK_CONNECTORS": json.dumps([binding]),
            "CALIBER_EGRESS_ALLOWED_HOSTS": "127.0.0.1",
        }
    )
    for shared_delivery in (True, False):
        with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
            _seed_run(factory)
            downstream.reset()
            proposal_id, key = _unknown_proposal(factory, downstream)
            answers = _race_callbacks(factory, config, key, shared_delivery=shared_delivery)
            assert len(answers) == 8
            for code, body in answers:
                busy = (code, body.get("status")) == (503, "busy")
                told_the_truth = code == 200 and body["data"]["status"] in {
                    "committed",
                    "duplicate",
                    "already_committed",
                }
                assert busy or told_the_truth, answers
            assert any(code == 200 for code, _ in answers)
            with factory() as session:
                assert proposal_state(session, proposal_id).state == COMMITTED
            assert _count(factory, CaliberEffectReceipt) == 1
