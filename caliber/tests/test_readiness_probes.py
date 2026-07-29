"""Regression tests for dependency readiness.

The review's finding was that ``/readiness`` "always returns 200 and reports
provider selector/feature flags rather than dependency connectivity". A probe that
cannot fail is worse than no probe: an orchestrator wired to it will never depool
or restart a broken instance. These tests pin the three properties that make the
new probe trustworthy:

1. a broken **required** dependency produces 503 and names the blocker;
2. requiredness is *derived from configuration*, so a dependency the deployment
   does not use is skipped rather than reported as a passing check; and
3. the existing provider/tracing fields the SPA banner reads are unchanged, and
   running on the deterministic fake provider does **not** fail readiness.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber.config import CaliberConfig, WorkflowStorageConfig
from caliber.observability import readiness as readiness_mod
from caliber.observability.queue_health import QueueHealth
from caliber.observability.readiness import collect_readiness
from caliber.routes.health import READINESS_PATH


def _run(**kwargs: Any) -> Any:
    return asyncio.run(collect_readiness(**kwargs))


class _WorkingSession:
    def __enter__(self) -> _WorkingSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _statement: object) -> None:
        return None


def _working_factory() -> _WorkingSession:
    return _WorkingSession()


def _broken_factory() -> _WorkingSession:
    raise RuntimeError("could not connect to server: Connection refused")


# ---------------------------------------------------------------------------
# The database check — always required
# ---------------------------------------------------------------------------


def test_a_reachable_database_is_ready() -> None:
    report = _run(config=CaliberConfig(), session_factory=_working_factory, environ={})
    database = next(check for check in report.checks if check.name == "database")
    assert database.ready is True
    assert database.required is True
    assert database.latency_ms is not None
    assert report.ready is True
    assert report.blockers == []


def test_an_unreachable_database_blocks_readiness() -> None:
    report = _run(config=CaliberConfig(), session_factory=_broken_factory, environ={})
    assert report.ready is False
    assert any("database" in blocker for blocker in report.blockers)
    assert "Connection refused" in report.blockers[0]


def test_a_missing_session_factory_blocks_readiness() -> None:
    report = _run(config=CaliberConfig(), session_factory=None, environ={})
    assert report.ready is False


# ---------------------------------------------------------------------------
# Requiredness is derived from configuration
# ---------------------------------------------------------------------------


def test_mlflow_is_skipped_without_an_http_tracking_server() -> None:
    """A local file/sqlite tracking store has no service to probe. Reporting it as
    a passing dependency would assert something the deployment does not have."""
    report = _run(config=CaliberConfig(), session_factory=_working_factory, environ={})
    mlflow = next(check for check in report.checks if check.name == "mlflow")
    assert mlflow.ready is None
    assert mlflow.required is False
    assert report.ready is True


def test_mlflow_is_required_and_probed_when_a_tracking_server_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(url: str, label: str) -> str:
        del url
        raise RuntimeError(f"{label} unreachable")

    monkeypatch.setattr(readiness_mod, "_probe_http", _fail)
    report = _run(
        config=CaliberConfig(),
        session_factory=_working_factory,
        environ={"MLFLOW_TRACKING_URI": "http://mlflow:5000"},
    )
    mlflow = next(check for check in report.checks if check.name == "mlflow")
    assert mlflow.required is True
    assert mlflow.ready is False
    assert report.ready is False


def test_object_storage_is_skipped_for_a_local_backend_and_required_for_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _run(
        config=CaliberConfig(workflow_storage=WorkflowStorageConfig(base_uri="file:///tmp/x")),
        session_factory=_working_factory,
        environ={},
    )
    store = next(check for check in local.checks if check.name == "object_store")
    assert store.ready is None
    assert store.required is False

    def _broken(_config: object) -> str:
        raise RuntimeError("NoSuchBucket")

    monkeypatch.setattr(readiness_mod, "_probe_object_store_sync", _broken)
    remote = _run(
        config=CaliberConfig(
            workflow_storage=WorkflowStorageConfig(base_uri="s3://caliber-workspaces")
        ),
        session_factory=_working_factory,
        environ={},
    )
    remote_store = next(check for check in remote.checks if check.name == "object_store")
    assert remote_store.required is True
    assert remote_store.ready is False
    assert "NoSuchBucket" in remote_store.detail
    assert remote.ready is False


def test_the_explicit_s3_backend_is_probed_even_with_a_local_base_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2: this is the shipped S3/MinIO topology, and it used to report ready.

    ``base_uri`` is documented as *ignored* when the backend is ``s3``, so a real
    deployment has ``backend="s3"``, a bucket, and the default ``file://`` base URI.
    Inferring the backend from the URI scheme classified that as local storage and
    returned ``required=false, ready=null`` — readiness answered 200 while object
    storage was unreachable.
    """

    def _broken(_config: object) -> str:
        raise RuntimeError("NoSuchBucket")

    monkeypatch.setattr(readiness_mod, "_probe_object_store_sync", _broken)
    report = _run(
        config=CaliberConfig(
            workflow_storage=WorkflowStorageConfig(
                backend="s3",
                bucket="caliber-workspaces",
                # Left at the shipped default on purpose: that is the exact
                # configuration the old classifier mistook for local storage.
                base_uri="file://./caliber-workspaces",
            )
        ),
        session_factory=_working_factory,
        environ={},
    )
    store = next(check for check in report.checks if check.name == "object_store")
    assert store.required is True, "an explicit s3 backend is a required dependency"
    assert store.ready is False
    assert "NoSuchBucket" in store.detail
    assert report.ready is False


def test_an_s3_backend_with_no_bucket_fails_closed_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing bucket is a configuration error, and must read as one.

    Letting the probe run would raise inside ``build_backend`` and report the failure
    as if the bucket were unreachable, sending the operator to look at the network.
    """

    def _must_not_run(_config: object) -> str:  # pragma: no cover - asserted by absence
        raise AssertionError("the probe must not run when no bucket is configured")

    monkeypatch.setattr(readiness_mod, "_probe_object_store_sync", _must_not_run)
    report = _run(
        config=CaliberConfig(workflow_storage=WorkflowStorageConfig(backend="s3", bucket=None)),
        session_factory=_working_factory,
        environ={},
    )
    store = next(check for check in report.checks if check.name == "object_store")
    assert store.required is True
    assert store.ready is False
    assert "no bucket is configured" in store.detail
    assert report.ready is False


def test_a_remote_base_uri_still_triggers_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URI scheme remains an *additional* trigger, never a suppressor.

    A deployment that points ``base_uri`` at a remote scheme without setting
    ``backend`` should be probed rather than skipped, so neither signal can turn a
    required check off.
    """
    probed: list[bool] = []

    def _ok(_config: object) -> str:
        probed.append(True)
        return "bucket listable"

    monkeypatch.setattr(readiness_mod, "_probe_object_store_sync", _ok)
    report = _run(
        config=CaliberConfig(
            workflow_storage=WorkflowStorageConfig(backend="local", base_uri="s3://bucket/prefix")
        ),
        session_factory=_working_factory,
        environ={},
    )
    store = next(check for check in report.checks if check.name == "object_store")
    assert probed == [True]
    assert store.required is True
    assert store.ready is True


def test_the_event_bus_is_only_probed_for_an_external_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_process = _run(config=CaliberConfig(), session_factory=_working_factory, environ={})
    bus = next(check for check in in_process.checks if check.name == "event_bus")
    assert bus.ready is None
    assert bus.required is False

    async def _fail(host: str, port: int, label: str) -> str:
        del host, port
        raise ConnectionRefusedError(f"{label} refused")

    monkeypatch.setattr(readiness_mod, "_probe_tcp", _fail)
    nats = _run(
        config=CaliberConfig(workflow_run_event_backend="nats", nats_url="nats://bus:4222"),
        session_factory=_working_factory,
        environ={},
    )
    nats_bus = next(check for check in nats.checks if check.name == "event_bus")
    assert nats_bus.required is True
    assert nats_bus.ready is False
    assert nats.ready is False


# ---------------------------------------------------------------------------
# Worker / queue liveness
# ---------------------------------------------------------------------------


def test_a_degraded_queue_blocks_readiness_when_the_queue_is_enabled() -> None:
    """The signal ``/health`` could not give: a dead worker with a growing backlog
    looked identical to a healthy idle system."""
    report = _run(
        config=CaliberConfig(workflow_run_queue_enabled=True),
        session_factory=_working_factory,
        environ={},
        queue_health=QueueHealth(
            queued=7, degraded_reasons=["7 run(s) queued with no live worker"]
        ),
    )
    queue = next(check for check in report.checks if check.name == "workflow_queue")
    assert queue.required is True
    assert queue.ready is False
    assert "no live worker" in queue.detail
    assert report.ready is False


def test_a_healthy_queue_is_ready() -> None:
    report = _run(
        config=CaliberConfig(workflow_run_queue_enabled=True),
        session_factory=_working_factory,
        environ={},
        queue_health=QueueHealth(queued=0, running=1, workers_alive=1),
    )
    queue = next(check for check in report.checks if check.name == "workflow_queue")
    assert queue.ready is True
    assert report.ready is True


def test_the_queue_check_is_skipped_when_the_queue_is_disabled() -> None:
    """With the queue off, runs execute synchronously and a missing worker is not
    an outage — failing readiness for it would be wrong."""
    report = _run(
        config=CaliberConfig(workflow_run_queue_enabled=False),
        session_factory=_working_factory,
        environ={},
        queue_health=QueueHealth(queued=7, degraded_reasons=["no worker"]),
    )
    queue = next(check for check in report.checks if check.name == "workflow_queue")
    assert queue.ready is None
    assert queue.required is False
    assert report.ready is True


# ---------------------------------------------------------------------------
# Provider simulation must not fail an orchestrator probe
# ---------------------------------------------------------------------------


def test_simulated_providers_are_reported_but_do_not_fail_readiness() -> None:
    report = _run(config=CaliberConfig(), session_factory=_working_factory, environ={})
    providers = next(check for check in report.checks if check.name == "providers")
    assert providers.ready is False  # honestly reported...
    assert providers.required is False  # ...but the fake provider is a valid mode
    assert report.ready is True


def test_a_probe_that_raises_becomes_a_verdict_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the probe surface itself 500s, it becomes the outage it was meant to
    report."""

    async def _explode(url: str, label: str) -> str:
        del url, label
        raise ZeroDivisionError("bug in the probe")

    monkeypatch.setattr(readiness_mod, "_probe_http", _explode)
    report = _run(
        config=CaliberConfig(),
        session_factory=_working_factory,
        environ={"MLFLOW_TRACKING_URI": "http://mlflow:5000"},
    )
    mlflow = next(check for check in report.checks if check.name == "mlflow")
    assert mlflow.ready is False
    assert "bug in the probe" in mlflow.detail


def test_a_hung_probe_times_out_rather_than_hanging_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _hang(url: str, label: str) -> str:
        del url, label
        await asyncio.sleep(60)
        return "never"

    monkeypatch.setattr(readiness_mod, "_probe_http", _hang)
    monkeypatch.setattr(readiness_mod, "PROBE_TIMEOUT_SECONDS", 0.05)
    report = _run(
        config=CaliberConfig(),
        session_factory=_working_factory,
        environ={"MLFLOW_TRACKING_URI": "http://mlflow:5000"},
    )
    mlflow = next(check for check in report.checks if check.name == "mlflow")
    assert mlflow.ready is False
    assert "timed out" in mlflow.detail


# ---------------------------------------------------------------------------
# The endpoint contract
# ---------------------------------------------------------------------------


def test_readiness_endpoint_keeps_its_existing_fields(client: TestClient) -> None:
    """The SPA honesty banner reads these; growing the payload must not break it."""
    body = client.get(READINESS_PATH).json()["data"]
    for field in ("providers", "simulated", "all_real", "tracing_enabled"):
        assert field in body
    # ...and now also carries the operational verdict.
    assert "ready" in body
    assert "checks" in body
    assert {check["name"] for check in body["checks"]} >= {
        "database",
        "mlflow",
        "object_store",
        "event_bus",
        "workflow_queue",
        "providers",
    }


def test_readiness_returns_503_when_a_required_dependency_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the status code has to move, or an orchestrator cannot act
    on it."""

    def _broken(_factory: object) -> str:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(readiness_mod, "_probe_database_sync", _broken)
    response = client.get(READINESS_PATH)
    assert response.status_code == 503
    assert response.json()["data"]["ready"] is False
    assert any("database" in blocker for blocker in response.json()["data"]["blockers"])


def test_readiness_does_not_disclose_server_paths_or_bucket_names(client: TestClient) -> None:
    """``/readiness`` is unauthenticated, so a probe detail must not describe the
    server's filesystem layout."""
    body = client.get(READINESS_PATH).json()["data"]
    store = next(check for check in body["checks"] if check["name"] == "object_store")
    assert "/" not in store["detail"]


def test_unset_code_execution_allowlists_are_reported_but_not_blocking() -> None:
    """A control that is off by default with no surface is indistinguishable from a
    control that does not exist — the "decorative control" pattern being removed.

    Not required, though: an unset allowlist is legitimate for a single-operator
    install, and failing readiness on it would make an orchestrator refuse traffic to a
    working deployment.
    """
    report = _run(config=CaliberConfig(), session_factory=_working_factory, environ={})

    check = next(c for c in report.checks if c.name == "code_execution_allowlists")
    assert check.required is False
    assert check.ready is False
    assert report.ready is True, "a reported-but-unset control must not block readiness"
    # The two defaults mean *opposite* things, so the detail must not lump them together.
    assert "unrestricted" in check.detail
    assert "fail-closed" in check.detail


def test_configured_allowlists_report_clean() -> None:
    report = _run(
        config=CaliberConfig(
            registered_tool_module_allowlist="mycompany.tools.*",
            external_app_entrypoint_allowlist="mycompany.apps:run",
        ),
        session_factory=_working_factory,
        environ={},
    )
    check = next(c for c in report.checks if c.name == "code_execution_allowlists")
    assert check.ready is True
