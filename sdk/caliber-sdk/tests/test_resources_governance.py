"""Wave 3a (governance verbs): agent rollback, gate verdicts, release
operations/live/timeline, system-effect recovery, and promotion decisions.

These are the endpoints ``sdk-completeness-plan.md`` names as the highest-value
gap: without them a script can author a governed asset through the SDK but
cannot drive the release/rollback path the platform is built around. Every
test here pins the exact path and method, since that is precisely the class of
bug ``test_sdk_api_coverage.py`` exists to catch (and already caught twice,
elsewhere, before this wave was written).
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- agents: checkpoints + rollback -----------------------------------------


def test_agent_checkpoints_and_rollback_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.url.path.endswith("/checkpoints"):
            return envelope([{"checkpoint_id": "CK-1"}])
        return envelope(
            {
                "checkpoint": {"checkpoint_id": "CK-1"},
                "rotated_to": "prompt@prod",
                "rotated_at": None,
            }
        )

    with client_with(handler) as caliber:
        checkpoints = caliber.agents.checkpoints("AGT-1")
        result = caliber.agents.rollback("AGT-1")

    assert seen == ["GET /agents/AGT-1/checkpoints", "POST /agents/AGT-1/rollback"]
    assert checkpoints == [{"checkpoint_id": "CK-1"}]
    assert result["rotated_to"] == "prompt@prod"


def test_agent_rollback_targets_an_explicit_checkpoint() -> None:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"checkpoint": {"checkpoint_id": "CK-2"}})

    with client_with(handler) as caliber:
        caliber.agents.rollback("AGT-1", checkpoint_id="CK-2")

    assert bodies[0] == b'{"checkpoint_id":"CK-2"}'


# --- gate verdicts -----------------------------------------------------------


def test_gate_verdicts_get_and_record_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.method == "GET":
            return envelope({"state": "none"})
        return envelope({"state": "pass", "score": 0.9})

    with client_with(handler) as caliber:
        none_verdict = caliber.gate_verdicts.get("prompt", "support-triage@3")
        recorded = caliber.gate_verdicts.record(
            "prompt", "support-triage@3", state="pass", score=0.9
        )

    assert seen == [
        "GET /gate-verdicts/prompt/support-triage@3",
        "POST /gate-verdicts/prompt/support-triage@3",
    ]
    assert none_verdict == {"state": "none"}
    assert recorded["state"] == "pass"


# --- releases: report_job, timeline, live, operations, reconcile, resolve ---


def test_releases_new_methods_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.releases.report_job("RJ-1")
        caliber.releases.timeline(limit=10, entity_type="prompt")
        caliber.releases.live()
        caliber.releases.operations(status="prepared")
        caliber.releases.reconcile()
        caliber.releases.resolve_operation("RO-1", action="retry", reason="provider recovered")

    assert seen == [
        "GET /releases/report-jobs/RJ-1",
        "GET /releases/timeline",
        "GET /releases/live",
        "GET /releases/operations",
        "POST /releases/operations/reconcile",
        "POST /releases/operations/RO-1/resolve",
    ]


def test_releases_timeline_and_operations_pass_their_query_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope([])

    with client_with(handler) as caliber:
        caliber.releases.timeline(limit=10, entity_type="prompt")
        caliber.releases.operations(status="prepared")

    assert captured[0] == {"limit": "10", "entity_type": "prompt"}
    assert captured[1] == {"status": "prepared"}


def test_resolve_operation_sends_action_and_extra_params_in_the_body() -> None:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.releases.resolve_operation("RO-1", action="abandon", reason="stale")

    assert bodies[0] == b'{"action":"abandon","reason":"stale"}'


# --- system: effects + webhook dead letters ----------------------------------


def test_system_effects_recovery_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.system.effects(status="in_progress")
        caliber.system.resolve_effect("EFF-1", resolution="skip", reason="confirmed delivered")

    assert seen == [
        "GET /system/effects",
        "POST /system/effects/EFF-1/resolve",
    ]


def test_system_webhook_dead_letters_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"dead_letters": []})

    with client_with(handler) as caliber:
        caliber.system.webhook_dead_letters(status="open")
        caliber.system.acknowledge_dead_letter("DL-1", note="receiver fixed manually")
        caliber.system.replay_dead_letter("DL-2")

    assert seen == [
        "GET /system/webhook-dead-letters",
        "POST /system/webhook-dead-letters/DL-1/acknowledge",
        "POST /system/webhook-dead-letters/DL-2/replay",
    ]


# --- workflow promotions: approve / reject -----------------------------------


def test_workflow_promotion_approve_and_reject_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"rotated": True})

    with client_with(handler) as caliber:
        caliber.workflows.promotions.approve("PR-1", reason="looks good")
        caliber.workflows.promotions.reject("PR-2", reason="regression in eval")

    assert seen == [
        "POST /workflow-promotions/PR-1/approve",
        "POST /workflow-promotions/PR-2/reject",
    ]


def test_workflow_promotion_decisions_omit_reason_when_not_given() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"rotated": True})

    with client_with(handler) as caliber:
        caliber.workflows.promotions.approve("PR-1")

    assert bodies[0] in (b"{}", b"")
