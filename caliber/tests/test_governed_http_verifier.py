# ruff: noqa: F811 - the fixtures are the ones imported from the worker-wiring tests
"""#1055: the worker dispatches a workflow HTTP node only to an endpoint an operator listed, and reads it back by key.

``caliber.effects.governed_context`` used to bind an effect context that dispatched through no backend (no generic connector had an
authoritative verifier), so every webhook and API-request node was refused before it was recorded. With
``CALIBER_EFFECT_HTTP_VERIFIERS`` the context dispatches ``http`` for exactly the URLs the operator listed for the run's project,
confirms a 2xx by a lookup by key, and the reconciler settles an outcome that ended ``unknown`` by the same lookup. The checks that
matter most are the negative ones: a URL nobody listed is refused before a proposal exists, nothing a manifest or a downstream says
adds an endpoint or a receipt, and no path sends the request twice (measured at the fake downstream, which counts its own commits).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import caliber.effects.dispatcher as dispatcher_module
from caliber.db.models import CaliberEffectReceipt
from caliber.effects.connectors import TransportOutcome
from caliber.effects.connectors.http_lookup import receipt_request_id
from caliber.effects.dispatcher import EffectRefused, EffectRequest
from caliber.effects.governed_context import (
    HTTP_BACKEND_KIND,
    effect_context,
    http_receipt_verifier,
    run_authority,
)
from caliber.effects.http_verifiers import VerifierEndpoint
from caliber.effects.reconcile import (
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    UNVERIFIED,
    Answer,
    reconcile_one,
    targets_from_config,
)
from caliber.effects.records import Intent, ProposalView
from caliber.integrations.openapi.executor import OpenApiExecutionError
from caliber.ownership import current_lease
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimeResumeCheckpoint,
    execute,
)
from tests.test_governed_worker_wiring import (  # noqa: F401 - the module's fixtures and helpers
    _authority,
    _config,
    _decide,
    _migrated_template,
    _node_plan,
    _requests,
    _rows,
    _run_row,
    real_validator_stub,
    world,
)
from tests.test_workflow_http_dispatch import _http_manifest

pytestmark = pytest.mark.usefixtures("fixed_step_clock")

URL = "https://example.test/governed"
LOOKUP = "https://example.test/lookups/by-key"


def _listing(project: str, *urls: str) -> str:
    return json.dumps(
        [{"project_id": project, "resource": url, "lookup_url": LOOKUP} for url in urls]
    )


def _authority_for(world: Any, *urls: str, project: str | None = None) -> Any:
    run = _run_row(world)
    return _authority(
        world,
        config=_config(effect_http_verifiers=_listing(project or run.project_id, *urls)),
    )


class Hook:
    """A fake downstream that counts its own commits: what the platform sent, and what its lookup by key answers."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.commits = 0
        self.stored: dict[str, dict[str, Any]] = {}
        self.mode = "ok"  # ok, drop (commit, then the answer is lost), 503 (commit, then an error)
        self.visible = True
        self.lookups: list[tuple[str, str]] = []
        self.lookup_error: Exception | None = None
        self.corrupt: dict[str, Any] = {}

    def sender(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sends.append(request)
        key = request["headers"]["Idempotency-Key"]
        if key not in self.stored:  # the downstream honours the key: a replay commits nothing
            self.commits += 1
            self.stored[key] = {
                "idempotency_key": key,
                "request_id": f"REQ-{self.commits}",
                "method": request.get("method") or "POST",
                "url": request["url"],
                "body": request.get("body"),
            }
        if self.mode == "drop":
            raise TimeoutError("the answer was lost")
        if self.mode == "503":
            return {"status_code": 503, "text": "unavailable"}
        return {"status_code": 201, "request_id": self.stored[key]["request_id"]}

    def lookup(self, connector: Any, base_url: str, key: str) -> Answer:
        self.lookups.append((base_url, key))
        if self.lookup_error is not None:
            raise self.lookup_error
        if not self.visible or key not in self.stored:
            return Answer(404, {"detail": "not found"})
        return Answer(200, {**self.stored[key], **self.corrupt})


def _resume(plan: Any, step: Any) -> Any:
    return execute(
        plan,
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="hook",
            checkpoint_kind="runtime_approval",
            input_by_port=dict(step.input_by_port or {}),
            replay_output=False,
        ),
    )


def _approved_run(world: Any, hook: Hook, authority: Any) -> Any:
    """Run the node to its park, decide the bound request, and resume it: the result of the dispatch."""
    plan = _node_plan(
        world,
        effect_context(authority, lookup=hook.lookup),
        sender=hook.sender,
        manifest=_http_manifest("webhook", URL),
    )
    first = execute(plan, "hello", executor=FakeWorkflowExecutor())
    assert first.status == "blocked" and first.error == "waiting_approval:hook", first.error
    (step,) = [item for item in first.steps if item.node_id == "hook"]
    assert hook.sends == [], "a proposal alone sends nothing"
    _decide(world, _requests(world)[0].runtime_approval_id)
    return _resume(plan, step)


# ----------------------------------------------------------------------------------------- the context


def test_a_project_with_listed_endpoints_gets_an_http_context_covering_exactly_those_urls(
    world: Any,
) -> None:
    other = "https://example.test/other"
    authority = _authority_for(world, URL, other)
    assert set(authority.http_endpoints) == {URL, other}
    assert authority.http_endpoints[URL] == VerifierEndpoint(
        _run_row(world).project_id, URL, LOOKUP
    )

    context = effect_context(authority)

    assert context.dispatchable_backends == frozenset({HTTP_BACKEND_KIND})
    assert context.verifiable_resources == {HTTP_BACKEND_KIND: frozenset({URL, other})}


def test_endpoints_listed_for_another_project_are_not_this_runs(world: Any) -> None:
    authority = _authority_for(world, URL, project="someone-elses-project")
    assert authority.http_endpoints == {}
    context = effect_context(authority)
    assert context.dispatchable_backends == frozenset()
    assert context.verifiable_resources == {}


def test_with_nothing_listed_the_context_dispatches_nothing_as_before(world: Any) -> None:
    authority = _authority(world)
    assert authority.http_endpoints == {}
    context = effect_context(authority)
    assert context.dispatchable_backends == frozenset() and context.verifiable_resources == {}


def test_a_run_with_no_project_has_no_endpoints(world: Any) -> None:
    real = _run_row(world)
    run = SimpleNamespace(
        workflow_run_id=real.workflow_run_id, project_id=None, created_by=real.created_by
    )
    authority = run_authority(
        session_factory=world,
        run=run,  # type: ignore[arg-type]
        config=_config(effect_http_verifiers=_listing("PRJ-ep", URL)),
        lease_provider=lambda: None,
        run_manifest_digest="a" * 64,
    )
    assert authority.http_endpoints == {}


def test_explicit_verifiers_are_the_callers_and_are_not_limited_to_the_operators_list(
    world: Any,
) -> None:
    """Only the platform's own read-back is scoped to listed URLs; a caller that supplies its verifier owns its coverage."""
    authority = _authority_for(world, URL)
    context = effect_context(authority, verifiers={"http": lambda *_: None})
    assert context.dispatchable_backends == frozenset({"http"})
    assert context.verifiable_resources == {}


def test_an_unreadable_listing_makes_the_governed_context_unavailable(world: Any) -> None:
    from caliber.effects.governed_context import GovernedContextUnavailable

    broken = SimpleNamespace(
        approver_users="approver-1",
        effect_callback_connectors="[]",
        effect_http_verifiers="not json",
        approval_allow_self_approval=False,
        approval_request_ttl_seconds=3600,
    )
    with pytest.raises(GovernedContextUnavailable) as caught:
        run_authority(
            session_factory=world,
            run=_run_row(world),
            config=broken,  # type: ignore[arg-type]
            lease_provider=lambda: None,
            run_manifest_digest="a" * 64,
        )
    assert caught.value.code == "configuration_invalid"


# ------------------------------------------------------------------------- refused before a proposal exists


@pytest.mark.parametrize(
    "listed",
    [
        ("https://example.test/not-governed",),
        ("https://example.test/governed?x=1",),
        ("https://example.test/governed/",),
        ("HTTPS://example.test/governed",),
    ],
    ids=["another url", "a query", "a trailing slash", "another spelling"],
)
def test_a_node_whose_url_is_not_listed_is_refused_before_anything_is_recorded(
    world: Any, listed: tuple[str, ...]
) -> None:
    hook = Hook()
    context = effect_context(_authority_for(world, *listed), lookup=hook.lookup)
    plan = _node_plan(world, context, sender=hook.sender, manifest=_http_manifest("webhook", URL))

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "HTTP effect refused: no_authoritative_verifier" in (result.error or "")
    assert hook.sends == [] and hook.lookups == []
    assert _rows(world) == ([], []), "no proposal, and no approval asked for"


def test_the_verifier_itself_refuses_a_resource_nobody_listed() -> None:
    verify = http_receipt_verifier({URL: VerifierEndpoint("PRJ", URL, LOOKUP)})
    view = SimpleNamespace(resource="https://example.test/other")
    with pytest.raises(EffectRefused) as refused:
        verify(SimpleNamespace(view=view), {})  # type: ignore[arg-type]
    assert refused.value.code == "no_authoritative_verifier"


# ---------------------------------------------------------------------------------- the dispatch and its read-back


@pytest.mark.usefixtures("real_validator_stub")
def test_a_listed_node_is_sent_once_and_its_2xx_is_confirmed_by_a_lookup_by_key(world: Any) -> None:
    hook = Hook()
    authority = _authority_for(world, URL)

    result = _approved_run(world, hook, authority)

    assert result.status == "completed", result.error
    assert hook.commits == 1 and len(hook.sends) == 1
    (proposal,), (spent,) = _rows(world)
    assert proposal.state == "committed" and proposal.receipt_id is not None
    assert spent.consumed_at is not None
    assert hook.lookups == [(LOOKUP, proposal.logical_key)], (
        "read back once, at the listed URL, by the logical key"
    )
    assert hook.sends[0]["headers"]["Idempotency-Key"] == proposal.logical_key
    with world() as session:
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert (receipt.verified_by, receipt.downstream_request_id) == (
            "query",
            receipt_request_id(URL, "REQ-1"),
        )


@pytest.mark.usefixtures("real_validator_stub")
@pytest.mark.parametrize("mode", ["drop", "503"])
def test_an_outcome_nobody_saw_is_unknown_then_settled_by_the_reconciler_without_a_second_send(
    world: Any, mode: str
) -> None:
    """The acceptance scenario of #1055: the downstream committed, the answer was lost; one commit, no second send."""
    hook = Hook()
    hook.mode = mode
    authority = _authority_for(world, URL)

    result = _approved_run(world, hook, authority)

    assert result.status == "error" and "HTTP effect did not commit" in (result.error or "")
    assert hook.commits == 1, "the downstream did commit; the platform could not tell"
    (proposal,), _ = _rows(world)
    assert proposal.state == "unknown", "an answer nobody can read back is not a commit"
    assert hook.lookups == [], (
        "an ambiguous send is never verified: there is no 2xx claim to confirm"
    )

    with world() as session:
        lease = current_lease(session)
    assert lease is not None
    settled = reconcile_one(
        world,
        proposal.proposal_id,
        lease=lease,
        targets=targets_from_config(
            _config(effect_http_verifiers=_listing(_run_row(world).project_id, URL))
        ),
        lookup=hook.lookup,
    )

    assert (settled.outcome, settled.state) == (RECONCILED_COMMITTED, "committed")
    assert hook.commits == 1 and len(hook.sends) == 1, "asked by key, never sent again"
    assert hook.lookups == [(LOOKUP, proposal.logical_key)]
    with world() as session:
        assert session.execute(select(CaliberEffectReceipt)).scalar_one().proposal_id == (
            proposal.proposal_id
        )


@pytest.mark.usefixtures("real_validator_stub")
def test_a_2xx_the_lookup_cannot_see_yet_is_unknown_and_a_later_lookup_commits_it(
    world: Any,
) -> None:
    hook = Hook()
    hook.visible = (
        False  # delayed visibility: the downstream committed, its lookup does not show it yet
    )
    authority = _authority_for(world, URL)

    result = _approved_run(world, hook, authority)

    assert result.status == "error" and "receipt_unverified" in (result.error or "")
    (proposal,), _ = _rows(world)
    assert proposal.state == "unknown" and hook.commits == 1

    with world() as session:
        lease = current_lease(session)
    assert lease is not None
    targets = targets_from_config(
        _config(effect_http_verifiers=_listing(_run_row(world).project_id, URL))
    )
    still = reconcile_one(
        world, proposal.proposal_id, lease=lease, targets=targets, lookup=hook.lookup
    )
    assert (still.outcome, still.state) == (STILL_UNKNOWN, "unknown"), "a 404 never fails it"
    hook.visible = True
    done = reconcile_one(
        world, proposal.proposal_id, lease=lease, targets=targets, lookup=hook.lookup
    )
    assert (done.outcome, done.state) == (RECONCILED_COMMITTED, "committed")
    assert hook.commits == 1 and len(hook.sends) == 1


@pytest.mark.usefixtures("real_validator_stub")
@pytest.mark.parametrize(
    "corrupt",
    [
        {"body": {"somebody": "else"}},
        {"url": "https://example.test/elsewhere"},
        {"idempotency_key": "another-key"},
        {"method": "DELETE"},
    ],
)
def test_a_stored_request_that_is_not_this_effect_never_commits_it(
    world: Any, corrupt: dict[str, Any]
) -> None:
    hook = Hook()
    hook.corrupt = corrupt
    authority = _authority_for(world, URL)

    result = _approved_run(world, hook, authority)

    assert result.status == "error" and "receipt_unverified" in (result.error or "")
    (proposal,), _ = _rows(world)
    assert proposal.state == "unknown"
    with world() as session:
        assert session.execute(select(CaliberEffectReceipt)).first() is None

    with world() as session:
        lease = current_lease(session)
    assert lease is not None
    again = reconcile_one(
        world,
        proposal.proposal_id,
        lease=lease,
        targets=targets_from_config(
            _config(effect_http_verifiers=_listing(_run_row(world).project_id, URL))
        ),
        lookup=hook.lookup,
    )
    assert (again.outcome, again.state) == (UNVERIFIED, "unknown")


@pytest.mark.usefixtures("real_validator_stub")
def test_a_lookup_that_cannot_be_made_leaves_the_2xx_unknown_and_raises_nothing(
    world: Any,
) -> None:
    hook = Hook()
    hook.lookup_error = OpenApiExecutionError("refused", never_sent=False, attempts=1)
    authority = _authority_for(world, URL)

    result = _approved_run(world, hook, authority)

    assert result.status == "error" and "receipt_unverified" in (result.error or "")
    (proposal,), _ = _rows(world)
    assert proposal.state == "unknown" and hook.commits == 1 and len(hook.sends) == 1


@pytest.mark.usefixtures("real_validator_stub")
def test_an_unreconciled_unknown_blocks_what_depends_on_it_until_it_is_settled(world: Any) -> None:
    """The reconciler's gate still holds: an http effect that is unknown stops its dependents, not the platform."""
    from caliber.effects.reconcile import UnknownEffectError, effect_gate

    hook = Hook()
    hook.mode = "drop"
    _approved_run(world, hook, _authority_for(world, URL))
    (proposal,), _ = _rows(world)
    assert proposal.state == "unknown"
    with pytest.raises(UnknownEffectError) as blocked:
        effect_gate(world, proposal.workflow_run_id)({proposal.node_id})
    assert blocked.value.open_effects == ((proposal.proposal_id, proposal.node_id, "unknown"),)


# --------------------------------------------------------------------------------- the backend's key


def _http_backend_request(headers: dict[str, str] | None) -> EffectRequest:
    args: dict[str, Any] = {"url": URL, "method": "POST", "body": {"a": 1}}
    if headers is not None:
        args["headers"] = headers
    intent = Intent(
        connector="http",
        connector_version="1",
        effect_class="irreversible_write",
        resource=URL,
        args=args,
        expected_resource_version=None,
    )
    return EffectRequest(
        view=ProposalView(
            proposal_id="p-http",
            logical_key="logical-key-1",
            connector="http",
            connector_version="1",
            effect_class="irreversible_write",
            resource=URL,
            expected_resource_version=None,
            intent_hash=intent.digest,
            attempt=1,
            state="dispatched",
        ),
        intent={"args": intent.plain_args},
    )


def _perform(headers: dict[str, str] | None, sender: Callable[[Any], dict[str, Any]]) -> Any:
    return dispatcher_module._HttpBackend().perform(
        _http_backend_request(headers),
        {"sender": sender, "receipt_verifier": lambda *_: None},
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": "mine"},
        {"idempotency-key": "mine"},
        {"IDEMPOTENCY-KEY": ""},
        {"Idempotency-Key": "logical-key-1", "idempotency-key": "mine"},
        {"Idempotency-Key": "logical-key-1", "idempotency-key": "logical-key-1"},
    ],
)
def test_a_key_the_workflow_supplied_is_refused_before_anything_is_sent(
    headers: dict[str, str],
) -> None:
    """The lookup that settles an effect asks by the logical key: a different caller-supplied key would commit under one
    nobody can ask about, and two spellings would send two. Refused, never replaced, never sent."""
    sent: list[Any] = []
    with pytest.raises(RuntimeError) as refused:
        _perform(headers, lambda request: sent.append(request) or {"status_code": 201})
    assert refused.value.code == "connector_intent_unsupported"  # type: ignore[attr-defined]
    assert refused.value.never_sent is True  # type: ignore[attr-defined]
    assert sent == []


@pytest.mark.parametrize("name", ["Idempotency-Key", "idempotency-key", "IDEMPOTENCY-KEY"])
def test_the_platforms_own_key_may_be_repeated_by_the_workflow_in_any_case(name: str) -> None:
    captured: dict[str, Any] = {}

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {"status_code": 202}

    result = _perform({name: "logical-key-1", "X-Other": "1"}, sender)
    assert captured["headers"] == {name: "logical-key-1", "X-Other": "1"}, (
        "exactly one key header, as written"
    )
    assert result.outcome.outcome is TransportOutcome.AMBIGUOUS


def test_the_platforms_key_is_added_when_the_workflow_carried_none() -> None:
    captured: dict[str, Any] = {}
    _perform(
        {"X-Other": "1"},
        lambda request: captured.update(request) or {"status_code": 202},
    )
    assert captured["headers"] == {"X-Other": "1", "Idempotency-Key": "logical-key-1"}
