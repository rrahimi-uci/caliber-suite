"""``runs.causal_links`` and its async twin (B11.3b, #745): the typed, paginated read of a run's
persisted causal links.

The server owns the records; the SDK names them, never fills a missing one in, and decodes a
payload from an older or newer server without raising.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient, CaliberNotFoundError
from caliber_sdk.aio import AsyncCaliberClient
from caliber_sdk.models import (
    CausalApprovalLink,
    CausalContextLink,
    CausalContextManifest,
    CausalDecisionLink,
    CausalEffect,
    CausalEffectOutcomeLink,
    CausalLinks,
    CausalPolicyDecision,
)

BASE = "https://caliber.test"
DIGEST = "d" * 64


def sync_client(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def async_client(handler: Any) -> AsyncCaliberClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncCaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _effects_page(**overrides: Any) -> dict[str, Any]:
    """What the server answers for the default section: one complete chain and one still waiting."""
    page: dict[str, Any] = {
        "workflow_run_id": "WFR-1",
        "project_id": "PRJ-1",
        "trace_id": "tr-abc",
        "section": "effects",
        "run_manifest": {
            "status": "linked",
            "code": None,
            "manifest_id": "mf-1",
            "schema_version": "v1",
            "manifest_digest": DIGEST,
            "created_at": "2026-10-31T01:02:03",
        },
        "acceptance": {"status": "not_available", "code": "acceptance_record_not_available"},
        "evidence_package": {"status": "not_available", "code": "evidence_package_not_available"},
        "effects": [
            {
                "proposal_id": "prop-1",
                "node_id": "ask",
                "occurrence_id": "occ-1",
                "node_step_id": "WFR-1:ask:occ-1",
                "state": "committed",
                "connector": "document-request",
                "connector_version": "1",
                "effect_class": "irreversible_write",
                "intent_hash": "sha256:" + "a" * 64,
                "created_at": "2026-10-31T01:02:04",
                "settled_at": "2026-10-31T01:02:09",
                "context_manifest": {
                    "status": "linked",
                    "context_manifest_id": "cm-1",
                    "revision": 2,
                    "attempt": 1,
                    "manifest_digest": DIGEST,
                },
                "approval": {
                    "status": "linked",
                    "approval_id": "rap-1",
                    "request_status": "approved",
                    "intent_hash": "sha256:" + "a" * 64,
                    "consumed_at": "2026-10-31T01:02:07",
                },
                "decision": {"status": "unrecorded", "code": "decision_recorded_per_run_only"},
                "effect": {
                    "status": "linked",
                    "effect_id": "rcpt-1",
                    "attempt": 1,
                    "receipt_digest": DIGEST,
                    "response_digest": "sha256:" + "b" * 64,
                    "committed": True,
                },
                "span_attributes": {
                    "caliber.proposal_id": "prop-1",
                    "caliber.approval_id": "rap-1",
                    "caliber.effect_id": "rcpt-1",
                },
                "missing_links": [],
                "unrecorded_links": ["decision"],
            },
            {
                "proposal_id": "prop-2",
                "node_id": "ask",
                "occurrence_id": "occ-2",
                "node_step_id": "WFR-1:ask:occ-2",
                "state": "dispatched",
                "connector": "document-request",
                "connector_version": "1",
                "effect_class": "read",
                "intent_hash": "sha256:" + "c" * 64,
                "context_manifest": {
                    "status": "unrecorded",
                    "code": "no_context_manifest_for_node_step",
                },
                "approval": {"status": "not_required", "code": "approved_by_policy_decision"},
                "decision": {"status": "linked", "decision_id": "pd-1", "result": "allow"},
                "effect": {"status": "pending", "code": "outcome_pending"},
                "span_attributes": {"caliber.proposal_id": "prop-2", "caliber.decision_id": "pd-1"},
                "missing_links": [],
                "unrecorded_links": ["context_manifest"],
            },
        ],
        "context_manifests": [],
        "policy_decisions": [],
        "limit": 50,
        "has_more": True,
        "next_cursor": "cHJvcC0y",
    }
    page.update(overrides)
    return page


def test_causal_links_decodes_every_record_into_its_typed_model_and_hits_its_own_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return envelope(_effects_page())

    with sync_client(handler) as caliber:
        page = caliber.workflows.runs.causal_links("WFR-1")

    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path.endswith("/workflow-runs/WFR-1/causal-links")
    assert seen[0].url.query == b"", "no parameter is sent that the caller did not give"
    assert isinstance(page, CausalLinks) and page.section == "effects"
    assert (
        page.workflow_run_id == "WFR-1" and page.project_id == "PRJ-1" and page.trace_id == "tr-abc"
    )
    assert (page.run_manifest.status, page.run_manifest.manifest_id) == ("linked", "mf-1")
    assert page.run_manifest.manifest_digest == DIGEST
    assert (page.acceptance.status, page.acceptance.code) == (
        "not_available",
        "acceptance_record_not_available",
    )
    assert page.evidence_package.code == "evidence_package_not_available"
    complete, waiting = page.effects
    assert isinstance(complete, CausalEffect) and complete.proposal_id == "prop-1"
    assert isinstance(complete.context_manifest, CausalContextLink)
    assert (
        isinstance(complete.approval, CausalApprovalLink)
        and complete.approval.approval_id == "rap-1"
    )
    assert isinstance(complete.decision, CausalDecisionLink)
    assert (complete.decision.status, complete.decision.code) == (
        "unrecorded",
        "decision_recorded_per_run_only",
    )
    assert isinstance(complete.effect, CausalEffectOutcomeLink)
    assert complete.effect.effect_id == "rcpt-1" and complete.effect.committed is True
    assert complete.span_attributes["caliber.effect_id"] == "rcpt-1"
    assert complete.missing_links == [] and complete.unrecorded_links == ["decision"]
    assert (waiting.effect.status, waiting.effect.code, waiting.effect.effect_id) == (
        "pending",
        "outcome_pending",
        None,
    )
    assert waiting.effect.committed is False and "caliber.effect_id" not in waiting.span_attributes
    assert waiting.context_manifest.status == "unrecorded"
    assert page.has_more is True and page.limit == 50 and page.next_cursor == "cHJvcC0y"


def test_section_limit_and_cursor_are_sent_only_when_given() -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return envelope(_effects_page(effects=[], has_more=False, next_cursor=None))

    with sync_client(handler) as caliber:
        runs = caliber.workflows.runs
        runs.causal_links("WFR-1", section="policy_decisions")
        runs.causal_links("WFR-1", limit=2, cursor="abc")
        runs.causal_links("WFR-1", section="effects", limit=200, cursor="x")

    assert queries == [
        {"section": "policy_decisions"},
        {"limit": "2", "cursor": "abc"},
        {"section": "effects", "limit": "200", "cursor": "x"},
    ]


def test_the_other_sections_decode_into_their_own_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("section") == "context_manifests":
            return envelope(
                _effects_page(
                    section="context_manifests",
                    effects=[],
                    context_manifests=[
                        {
                            "context_manifest_id": "cm-1",
                            "step_request_id": "step-1",
                            "node_step_id": "WFR-1:ask:occ-1",
                            "occurrence_id": "occ-1",
                            "attempt": 1,
                            "revision": 2,
                            "schema_version": "v1",
                            "manifest_digest": DIGEST,
                            "created_at": "2026-10-31T01:02:03",
                        }
                    ],
                )
            )
        return envelope(
            _effects_page(
                section="policy_decisions",
                effects=[],
                policy_decisions=[
                    {
                        "decision_id": "pd-1",
                        "grant_id": "gr-1",
                        "action": "workflow.run",
                        "resource": "workflow:wf-1",
                        "result": "allow",
                        "reason_codes": ["grant_active"],
                        "policy_version": "policy-v1",
                        "input_hash": "1" * 64,
                        "evaluated_at": "2026-10-31T01:02:03",
                    }
                ],
            )
        )

    with sync_client(handler) as caliber:
        contexts = caliber.workflows.runs.causal_links("WFR-1", section="context_manifests")
        decisions = caliber.workflows.runs.causal_links("WFR-1", section="policy_decisions")

    (context,) = contexts.context_manifests
    assert isinstance(context, CausalContextManifest)
    assert (context.context_manifest_id, context.attempt, context.revision) == ("cm-1", 1, 2)
    assert context.manifest_digest == DIGEST and contexts.effects == []
    (decision,) = decisions.policy_decisions
    assert isinstance(decision, CausalPolicyDecision)
    assert decision.reason_codes == ["grant_active"] and decision.result == "allow"
    assert decisions.context_manifests == []


def test_a_payload_from_another_version_decodes_without_raising_and_is_never_linked() -> None:
    """A missing status is ``None``, not ``linked``; a missing ``has_more`` is not "more";
    unknown fields are kept."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "workflow_run_id": "WFR-1",
                "future_page_field": 1,
                "effects": [
                    {
                        "proposal_id": "prop-1",
                        "future_effect_field": True,
                        "approval": {"future": 1},
                    },
                    "not an object",
                    None,
                ],
                "context_manifests": "not a list",
            }
        )

    with sync_client(handler) as caliber:
        page = caliber.workflows.runs.causal_links("WFR-1")

    assert page.extra == {"future_page_field": 1}
    assert page.has_more is False and page.next_cursor is None and page.limit is None
    assert page.run_manifest.status is None and page.acceptance.status is None
    (effect,) = page.effects
    assert effect.extra == {"future_effect_field": True} and effect.approval.extra == {"future": 1}
    assert effect.approval.status is None and effect.effect.status is None
    assert effect.effect.effect_id is None and effect.effect.committed is False
    assert page.context_manifests == [] and page.policy_decisions == []
    assert CausalLinks.from_payload(None).effects == []
    assert CausalLinks.from_payload(["not", "a", "dict"]).workflow_run_id == ""


def test_a_run_the_caller_cannot_read_is_a_typed_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "workflow run 'WFR-9' not found"})

    with sync_client(handler) as caliber, pytest.raises(CaliberNotFoundError):
        caliber.workflows.runs.causal_links("WFR-9")


def test_the_async_client_reads_the_same_page_from_the_same_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return envelope(_effects_page())

    async def main() -> tuple[CausalLinks, CausalLinks]:
        async with async_client(handler) as caliber:
            plain = await caliber.workflows.causal_links("WFR-1")
            paged = await caliber.workflows.causal_links(
                "WFR-1", section="effects", limit=2, cursor="cHJvcC0x"
            )
        return plain, paged

    plain, paged = asyncio.run(main())
    assert [request.url.path.rsplit("/caliber", 1)[-1] for request in seen] == [
        "/workflow-runs/WFR-1/causal-links"
    ] * 2
    assert dict(seen[1].url.params) == {"section": "effects", "limit": "2", "cursor": "cHJvcC0x"}
    assert seen[0].url.query == b""
    with sync_client(lambda request: envelope(_effects_page())) as caliber:
        sync_page = caliber.workflows.runs.causal_links("WFR-1")
    assert plain == paged == sync_page, "one decoder behind both clients"


def test_the_async_client_raises_the_same_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "workflow run 'WFR-9' not found"})

    async def main() -> None:
        async with async_client(handler) as caliber:
            await caliber.workflows.causal_links("WFR-9")

    with pytest.raises(CaliberNotFoundError):
        asyncio.run(main())
