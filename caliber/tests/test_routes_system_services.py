"""Integration tests for ``/caliber/system/services`` (platform health).

HTTP + TCP probes are monkeypatched; the DB probe runs for real against the
test engine (SELECT 1 on the fixture SQLite), exercising that path end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

import caliber.routes.system_services as svc

SERVICES = "/ajax-api/2.0/mlflow/caliber/system/services"


def _set_config(client: TestClient, **updates: object) -> None:
    client.app.state.config = client.app.state.config.model_copy(update=updates)


def _patch_probes(monkeypatch: pytest.MonkeyPatch, *, http: svc._Probe, tcp: svc._Probe) -> None:
    async def _http(*_a: object, **_k: object) -> svc._Probe:
        return http

    async def _tcp(*_a: object, **_k: object) -> svc._Probe:
        return tcp

    monkeypatch.setattr(svc, "_probe_http", _http)
    monkeypatch.setattr(svc, "_probe_tcp", _tcp)


def test_lists_services_with_health(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(
        client,
        gateway_uri="http://gw:5002",
        knowledge_age_viewer_url="http://localhost:8082",
    )
    _patch_probes(
        monkeypatch,
        http=svc._Probe(True, "HTTP 200", 10),
        tcp=svc._Probe(True, "TCP open", 4),
    )

    resp = client.get(SERVICES)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "checked_at_ms" in data
    by_key = {s["key"]: s for s in data["services"]}

    # Core services always present; graph console appears because age_url is set.
    assert {
        "mlflow",
        "mlflow_gateway",
        "object_store",
        "database",
        "event_bus",
        "graph_console",
    } <= set(by_key)
    assert by_key["mlflow"]["healthy"] is True
    assert by_key["mlflow"]["url"].endswith(":5000")
    assert by_key["event_bus"]["healthy"] is True
    # The DB probe ran for real against the fixture engine.
    assert by_key["database"]["healthy"] is True
    assert "SELECT 1 ok" in by_key["database"]["detail"]
    # Non-HTTP services expose no browse URL.
    assert by_key["event_bus"]["url"] is None


def test_marks_down_service(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(
        monkeypatch,
        http=svc._Probe(False, "connection refused", None),
        tcp=svc._Probe(False, "connection refused", None),
    )
    resp = client.get(SERVICES)
    assert resp.status_code == 200, resp.text
    by_key = {s["key"]: s for s in resp.json()["data"]["services"]}
    assert by_key["mlflow"]["healthy"] is False
    assert "connection refused" in by_key["mlflow"]["detail"]
    assert by_key["mlflow"]["latency_ms"] is None


def test_graph_console_probes_internal_address(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docker-localhost trap: the public :8082 browse URL is unreachable from
    the CALIBER container, but the internal age-viewer:3000 address is up — the
    graph console must still report healthy (regression: showed "down")."""
    _set_config(client, knowledge_age_viewer_url="http://localhost:8082")

    async def _http(_c: object, url: str) -> svc._Probe:
        if ":8082" in url:  # public host — not reachable from inside the container
            return svc._Probe(False, "connection refused", None)
        return svc._Probe(True, "HTTP 200", 5)

    async def _tcp(*_a: object, **_k: object) -> svc._Probe:
        return svc._Probe(True, "TCP open", 2)

    monkeypatch.setattr(svc, "_probe_http", _http)
    monkeypatch.setattr(svc, "_probe_tcp", _tcp)

    by_key = {s["key"]: s for s in client.get(SERVICES).json()["data"]["services"]}
    assert by_key["graph_console"]["healthy"] is True
    # Browse link stays the public URL the user clicks.
    assert by_key["graph_console"]["url"] == "http://localhost:8082"


def test_graph_console_omitted_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(client, knowledge_age_viewer_url=None)
    _patch_probes(
        monkeypatch,
        http=svc._Probe(True, "HTTP 200", 1),
        tcp=svc._Probe(True, "TCP open", 1),
    )
    by_key = {s["key"]: s for s in client.get(SERVICES).json()["data"]["services"]}
    assert "graph_console" not in by_key


def test_the_slo_endpoint_records_and_routes_an_incident(client) -> None:
    """Detection already worked; this pins that evaluating also *remembers*.

    A breach used to be visible only to whoever polled `/system/alerts` while it was still
    true. Reconciling on evaluation is what gives `/system/incidents` anything to show, and routing rides the
    event bus so alert delivery inherits the webhook dispatcher's retry, dead-lettering and
    crash recovery instead of growing a second delivery path.
    """
    published: list[dict] = []
    bus = getattr(client.app.state, "event_bus", None)
    if bus is not None:
        original = bus.publish

        def _spy(payload: dict) -> None:
            published.append(payload)
            original(payload)

        bus.publish = _spy  # type: ignore[method-assign]

    # An objective that cannot hold: a stale-lease count is always non-negative, so the
    # valid supported objective below must fire even on an otherwise empty fixture.
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "slo_objectives": "queue_stale_leases<=-1",
            "slo_severities": "queue_stale_leases<=-1=critical",
        }
    )

    response = client.get("/ajax-api/2.0/mlflow/caliber/system/alerts")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert "incidents" in body, "evaluation must reconcile the durable record"
    assert body["incidents"]["opened"] == ["queue_stale_leases<=-1"], (
        "a firing objective must reach incident reconciliation; an empty result would "
        "make the history/routing assertions below vacuous"
    )

    history = client.get("/ajax-api/2.0/mlflow/caliber/system/incidents")
    assert history.status_code == 200, history.text
    rows = history.json()["data"]["incidents"]
    assert rows, "an opened incident must appear in history"
    assert rows[0]["severity"] == "critical", "severity comes from operator config"
    assert any(e.get("type") == "slo.incident.opened" for e in published)


def test_silencing_an_unknown_incident_is_404(client) -> None:
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/system/incidents/INC-nope/silence",
        json={"minutes": 30},
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("action", "body", "changed_fields"),
    [
        ("silence", {"minutes": 30}, ("silenced_until",)),
        ("acknowledge", {}, ("acknowledged_at", "acknowledged_by")),
    ],
)
def test_incident_mutation_rolls_back_when_its_audit_write_fails(
    client: TestClient,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    body: dict[str, int],
    changed_fields: tuple[str, ...],
) -> None:
    """The route, not the mutation helper, owns the shared commit boundary."""
    from caliber.db.models import CaliberAuditLog, CaliberIncident

    incident_id = "INC-audit-atomicity"
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id=incident_id,
                objective="success_ratio>=0.9",
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="audit must share the transaction",
                opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()

    def _audit_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(svc, "audit_record", _audit_failure)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(
            f"/ajax-api/2.0/mlflow/caliber/system/incidents/{incident_id}/{action}",
            json=body,
        )

    with session_factory() as session:
        stored = session.get(CaliberIncident, incident_id)
        audits = session.query(CaliberAuditLog).filter_by(entity_id=incident_id).all()

    assert stored is not None
    assert all(getattr(stored, field) is None for field in changed_fields)
    assert audits == []


def test_incident_mutation_and_audit_commit_together(client: TestClient, session_factory) -> None:
    from caliber.db.models import CaliberAuditLog, CaliberIncident

    incident_id = "INC-audit-success"
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id=incident_id,
                objective="success_ratio>=0.9",
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="successful atomic mutation",
                opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()

    response = client.post(
        f"/ajax-api/2.0/mlflow/caliber/system/incidents/{incident_id}/acknowledge",
        json={},
    )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        stored = session.get(CaliberIncident, incident_id)
        audit = session.query(CaliberAuditLog).filter_by(entity_id=incident_id).one()

    assert stored is not None
    assert stored.acknowledged_by == "@test"
    assert audit.action == "acknowledge_incident"
