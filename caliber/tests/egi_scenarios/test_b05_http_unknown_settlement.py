# ruff: noqa: F811 - the world fixture is the one imported from the governed-worker scenarios
"""#1055 on PostgreSQL: an http effect whose outcome is unknown is settled by a lookup by key, and the downstream counts one mutation.

The reference-profile app and a real downstream (a small server in this process that counts its own commits, honours the
``Idempotency-Key`` header and answers the read-by-key contract of ``caliber.effects.connectors.http_lookup``). An operator lists the
webhook URL in ``CALIBER_EFFECT_HTTP_VERIFIERS``; a project editor admits a run whose only node is a webhook; a different person
approves the bound request over HTTP; an operator resumes the run; the worker (this process, or a child that is killed on purpose)
dispatches it once.

What is measured is the downstream's own count (``hook.commits``), never CALIBER's logs: a timeout after the send, a killed worker and
a lookup that has not caught up all end the same way, one commit and no second send, and only a stored request that is this effect
commits the proposal. A URL nobody listed is refused before a proposal exists. A lookup that finds nothing never fails an effect.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from sqlalchemy import select, update
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.db.models import CaliberEffectReceipt, CaliberWorkflowRun
from caliber.effects.reconcile import (
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    ask_downstream,
    reconcile_pass,
    targets_from_config,
)
from caliber.ownership import current_lease, release
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, Downstream, ReferenceApp, seed_release
from tests.egi_scenarios.harness.process import free_port
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b05_governed_worker_wiring import (
    CALIBER_DIR,
    Owner,
    _reference_only,
    _routes_config,
    bound_requests,
    build_worker,
    decide,
    proposals,
    resume,
    run_row,
    worker_config,
    world,  # noqa: F401 - the fixture
)
from tests.test_workflow_webhook_node import _manifest as webhook_manifest

#: The webhook node's own timeout (seconds): short, so a downstream that commits and then never answers times the sender out.
SEND_TIMEOUT_SECONDS = 1
#: How long the downstream sits on an answer it has already committed: longer than the sender waits.
STALL_SECONDS = 6.0


class Hook:
    """A downstream that counts its own commits, honours the key, and answers the platform's read by key.

    ``POST /hooks/notify`` stores the request under its ``Idempotency-Key`` (the first one commits and counts, a replay of a stored
    key commits nothing) and, when armed, then sits on the answer so the sender times out: committed, and nobody told. ``GET
    /lookups/by-key`` is the read-back contract: the stored request, or 404.
    """

    def __init__(self) -> None:
        self.port = free_port()
        self.lock = threading.Lock()
        self.commits = 0
        self.stored: dict[str, dict[str, Any]] = {}
        self.stall = False
        self.hide = False
        self.asked: list[str] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hooks/notify"

    @property
    def lookup_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/lookups/by-key"

    async def _notify(self, request: Request) -> JSONResponse:
        key = request.headers.get("idempotency-key")
        if not key:
            return JSONResponse({"detail": "an Idempotency-Key is required"}, status_code=400)
        try:
            body = await request.json()
        except ValueError:
            body = (await request.body()).decode()
        with self.lock:
            if key not in self.stored:
                self.commits += 1
                self.stored[key] = {
                    "idempotency_key": key,
                    "request_id": f"HOOK-{self.commits}",
                    "method": request.method,
                    "url": str(request.url),
                    "body": body,
                }
            record = dict(self.stored[key])
            stall = self.stall
        if stall:
            await asyncio.sleep(STALL_SECONDS)  # committed; the caller gave up waiting
        return JSONResponse({"request_id": record["request_id"]}, status_code=201)

    async def _lookup(self, request: Request) -> JSONResponse:
        key = request.query_params.get("idempotency_key", "")
        with self.lock:
            self.asked.append(key)
            record = None if self.hide else self.stored.get(key)
        if record is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(record)

    def start(self) -> None:
        app = Starlette(
            routes=[
                Route("/hooks/notify", self._notify, methods=["POST"]),
                Route("/lookups/by-key", self._lookup, methods=["GET"]),
            ]
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline, "the hook downstream did not start"
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture
def hook() -> Iterator[Hook]:
    downstream = Hook()
    downstream.start()
    try:
        yield downstream
    finally:
        downstream.stop()


def _listing(hook: Hook, *, resource: str | None = None) -> str:
    return json.dumps(
        [
            {
                "project_id": PROJECT_A,
                "resource": resource or hook.url,
                "lookup_url": hook.lookup_url,
            }
        ]
    )


def _changes(hook: Hook, *, listed: bool = True) -> dict[str, Any]:
    """What the worker's configuration adds: the operator's endpoint list, and loopback in the egress allowlist (the hook is on it)."""
    return {
        "effect_http_verifiers": _listing(hook) if listed else "[]",
        "egress_allowed_hosts": "127.0.0.1",
    }


def _manifest_for(hook: Hook) -> Any:
    def build(workflow_id: str) -> dict[str, Any]:
        manifest = webhook_manifest(hook.url)
        manifest["workflow_id"] = workflow_id
        manifest["nodes"]["hook"]["timeout_seconds"] = SEND_TIMEOUT_SECONDS
        return manifest

    return build


def _admit(app: ReferenceApp, downstream: Downstream, hook: Hook, *, key: str) -> str:
    """Create, publish and admit a run of the webhook workflow through the public API as the project editor."""
    _routes_config(app, downstream)
    manifest = _manifest_for(hook)
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "EGI http"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": manifest(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    started = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "tell the downstream",
            "idempotency_key": key,
            **seed_release(app, PROJECT_A),
        },
    )
    assert started.status_code == 202, started.text
    run_id: str = started.json()["data"]["workflow_run_id"]
    return run_id


def _park(
    app: ReferenceApp,
    downstream: Downstream,
    hook: Hook,
    monkeypatch: pytest.MonkeyPatch,
    owner: Owner,
    *,
    listed: bool = True,
) -> tuple[str, str, Any]:
    """Admit a run and let the worker run it to the park behind its approval request."""
    run_id = _admit(app, downstream, hook, key="egi-b05-http")
    worker = build_worker(
        app, downstream, monkeypatch, lease_provider=owner.provider, **_changes(hook, listed=listed)
    )
    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_approval", "waiting_approval"), (
        run.error_code,
        run.error_summary,
    )
    (request,) = bound_requests(app)
    return run_id, request.runtime_approval_id, worker


def _approve_and_resume(app: ReferenceApp, run_id: str, request_id: str) -> None:
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202


def _reconcile(app: ReferenceApp, hook: Hook, owner: Owner) -> list[Any]:
    assert owner.lease is not None
    config = SimpleNamespace(
        effect_callback_connectors="[]",
        effect_http_verifiers=_changes(hook)["effect_http_verifiers"],
    )
    return reconcile_pass(
        app.session_factory,
        lease=owner.lease,
        targets=targets_from_config(config),
        lookup=ask_downstream,
    )


def test_a_listed_webhook_is_sent_once_and_its_2xx_is_confirmed_by_the_lookup(
    world: tuple[ReferenceApp, Downstream], hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = _park(app, downstream, hook, monkeypatch, owner)
    assert hook.commits == 0, "a proposal alone sends nothing"
    (proposal,) = proposals(app)
    assert proposal.state == "proposed" and proposal.connector == "http"
    _approve_and_resume(app, run_id, request_id)

    worker._tick()

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("completed", None), (
        run.error_code,
        run.error_summary,
        [(p.state, p.reason) for p in proposals(app)],
    )
    assert hook.commits == 1, "the effect was sent exactly once"
    (done,) = proposals(app)
    assert done.state == "committed" and done.receipt_id is not None
    assert hook.asked == [done.logical_key], "confirmed once, by the proposal's logical key"
    assert hook.stored[done.logical_key]["url"] == hook.url
    with app.session_factory() as session:
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert (receipt.proposal_id, receipt.verified_by) == (done.proposal_id, "query")
        assert receipt.downstream_request_id == "HOOK-1"
    worker._tick()
    assert hook.commits == 1, "another pass sends nothing"


def test_a_timeout_after_the_send_parks_the_run_and_the_reconciler_settles_it_without_a_second_send(
    world: tuple[ReferenceApp, Downstream], hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance scenario of #1055: the downstream committed, the answer never came; one commit, one receipt, no resend."""
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = _park(app, downstream, hook, monkeypatch, owner)
    _approve_and_resume(app, run_id, request_id)
    hook.stall = True

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "failed" and run.error_code == "runtime_error", (
        "a node whose effect did not commit fails its run: the run does not wait, the effect is the reconciler's"
    )
    assert "did not commit: ambiguous" in (run.error_summary or "")
    assert hook.commits == 1, "the downstream did commit; the platform could not tell"
    (proposal,) = proposals(app)
    assert proposal.state == "unknown", "an answer nobody can read back is not a commit"
    assert hook.asked == [], "there was no 2xx claim to confirm"

    hook.stall = False
    results = _reconcile(app, hook, owner)

    assert [item.outcome for item in results] == [RECONCILED_COMMITTED]
    assert hook.asked == [proposal.logical_key], "asked once, by the key, at the listed URL"
    assert hook.commits == 1, "asked by key, never sent again"
    (settled,) = proposals(app)
    assert settled.state == "committed" and settled.receipt_id is not None
    with app.session_factory() as session:
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert (receipt.proposal_id, receipt.verified_by) == (proposal.proposal_id, "query")
        assert receipt.downstream_request_id == "HOOK-1"
    worker._tick()
    assert hook.commits == 1, "a later pass sends nothing, and an ended run is not resumed"
    assert run_row(app, run_id).status == "failed"


def test_a_lookup_that_never_shows_it_keeps_the_effect_unknown_and_never_fails_it(
    world: tuple[ReferenceApp, Downstream], hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing answer is not proof of absence: pass after pass, nothing is failed and nothing is re-sent."""
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = _park(app, downstream, hook, monkeypatch, owner)
    _approve_and_resume(app, run_id, request_id)
    hook.stall = True
    hook.hide = True
    worker._tick()
    assert run_row(app, run_id).status == "failed"
    hook.stall = False

    for _ in range(3):
        results = _reconcile(app, hook, owner)
        assert [item.outcome for item in results] == [STILL_UNKNOWN]
        assert results[0].detail == "http_404"

    assert proposals(app)[0].state == "unknown"
    assert hook.commits == 1 and len(hook.asked) == 3

    hook.hide = False  # the downstream catches up: the same lookup now settles it
    assert [item.outcome for item in _reconcile(app, hook, owner)] == [RECONCILED_COMMITTED]
    assert hook.commits == 1
    assert proposals(app)[0].state == "committed"


def test_a_webhook_whose_url_is_not_listed_is_refused_before_a_proposal_exists(
    world: tuple[ReferenceApp, Downstream], hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id = _admit(app, downstream, hook, key="egi-b05-http-unlisted")
    other_url = f"http://127.0.0.1:{hook.port + 1}/hooks/notify"  # only the exact resource of a node is verifiable
    worker = build_worker(
        app,
        downstream,
        monkeypatch,
        lease_provider=owner.provider,
        effect_http_verifiers=_listing(hook, resource=other_url),
        egress_allowed_hosts="127.0.0.1",
    )

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "failed", (run.status, run.error_code, run.error_summary)
    assert "no_authoritative_verifier" in (run.error_summary or "")
    assert hook.commits == 0 and proposals(app) == [] and bound_requests(app) == []


def test_with_no_endpoint_listed_a_webhook_is_refused_as_before(
    world: tuple[ReferenceApp, Downstream], hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id = _admit(app, downstream, hook, key="egi-b05-http-none")
    worker = build_worker(
        app, downstream, monkeypatch, lease_provider=owner.provider, **_changes(hook, listed=False)
    )
    worker._tick()
    run = run_row(app, run_id)
    assert run.status == "failed" and "no_authoritative_verifier" in (run.error_summary or "")
    assert hook.commits == 0 and proposals(app) == []


# ----------------------------------------------------------------------- a worker killed mid-dispatch


def _child_env(
    app: ReferenceApp, downstream: Downstream, hook: Hook, barrier: str
) -> dict[str, str]:
    config = worker_config(app, downstream, **_changes(hook))
    return {
        **app.env,
        "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED": "true",
        "CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED": "true",
        "CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED": "true",
        "CALIBER_ADMISSION_ENABLED": "true",
        "CALIBER_AGENT_STEP_TOOL_BACKENDS": config.agent_step_tool_backends,
        "CALIBER_EFFECT_CALLBACK_CONNECTORS": config.effect_callback_connectors,
        "CALIBER_EFFECT_HTTP_VERIFIERS": config.effect_http_verifiers,
        "CALIBER_EGRESS_ALLOWED_HOSTS": "127.0.0.1",
        "CALIBER_FAULT_BARRIERS": barrier,
        "PYTHONPATH": str(CALIBER_DIR / "src") + ":" + str(CALIBER_DIR),
    }


@pytest.mark.parametrize(
    ("barrier", "downstream_saw_it"),
    [("before_dispatch", False), ("after_downstream_commit", True)],
)
def test_a_worker_killed_mid_dispatch_of_a_webhook_never_sends_a_second_time(
    world: tuple[ReferenceApp, Downstream],
    hook: Hook,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
    downstream_saw_it: bool,
) -> None:
    app, downstream = world
    _reference_only(app)
    owner = Owner(app)
    owner.take()
    run_id, request_id, _worker = _park(app, downstream, hook, monkeypatch, owner)
    _approve_and_resume(app, run_id, request_id)
    owner.give_up()

    killed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.egi_scenarios.harness.worker_child",
            "--owner-id",
            "egi-child-owner",
        ],
        cwd=CALIBER_DIR,
        env=_child_env(app, downstream, hook, barrier),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert killed.returncode == 137, killed.stderr.decode()[-2000:]
    assert hook.commits == (1 if downstream_saw_it else 0)
    (proposal,) = proposals(app)
    assert proposal.state == "dispatched", "the claim committed before the process died"
    (request,) = bound_requests(app)
    assert request.consumed_at is not None, "and the approval was spent by it, once"

    with app.session_factory() as session:
        old = current_lease(session)
        assert old is not None
        release(session, old)
        session.commit()
    new_owner = Owner(app, "egi-owner-3")
    new_owner.take()
    with app.session_factory() as session:
        session.execute(
            update(CaliberWorkflowRun)
            .where(CaliberWorkflowRun.workflow_run_id == run_id)
            .values(lease_expires_at=None, status="queued", claimed_by=None)
        )
        session.commit()
    worker = build_worker(
        app, downstream, monkeypatch, lease_provider=new_owner.provider, **_changes(hook)
    )
    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_effect", "effect_reconciliation"), (
        run.error_code,
        run.error_summary,
    )
    assert hook.commits == (1 if downstream_saw_it else 0), "the recovery sent nothing"

    results = _reconcile(app, hook, new_owner)
    if downstream_saw_it:
        assert [item.outcome for item in results] == [RECONCILED_COMMITTED]
        assert proposals(app)[0].state == "committed"
        assert run_row(app, run_id).status == "queued", "settling it woke the parked run"
        worker._tick()
        # The woken node finds its one logical effect already committed and does not send it again. The response of the
        # original send was never durable, so the node cannot complete from it: the run fails with that fixed reason.
        run = run_row(app, run_id)
        assert (run.status, run.error_code) == ("failed", "runtime_error")
        assert "already recorded as committed; not run again" in (run.error_summary or "")
    else:
        # Killed before the send: the downstream never saw it, and its 404 is not proof it never will, so it stays unknown.
        assert [item.outcome for item in results] == [STILL_UNKNOWN]
        assert run_row(app, run_id).status == "waiting_effect"
    assert hook.commits == (1 if downstream_saw_it else 0)
