"""Tests for GitHub webhook-delivery reconciliation (`P4-E` slice 3).

Fully offline: a fake ``GitHubTransport`` (same fixture shape as
``test_github_source_control.py``/``test_github_workspace_provider.py``)
stands in for GitHub's App-scoped ``/app/hooks/deliveries`` endpoints.
Proves the catch-up path actually converges (a delivery already recorded
in the inbox is never re-requested), that pagination is bounded, and that
only the connection's private key is ever touched -- never its webhook
secret.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject, CaliberWorkspaceSource, CaliberWorkspaceSourceEvent
from caliber.github_source_control import GitHubResponse
from caliber.github_webhook_reconciliation import (
    MissedDelivery,
    WebhookReconciliationError,
    find_missed_deliveries,
    list_recent_deliveries,
    reconcile_connection,
    redeliver,
)
from caliber.ids import new_project_id, new_workspace_source_id
from caliber.secret_store import SecretCipher, SecretStore, generate_key
from caliber.workspace_source_connections import configure_connection

WEBHOOK_SECRET_VALUE = "wh-secret"
APP_JWT = "app-level-jwt"


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@dataclass
class _Transport:
    responses: dict[tuple[str, str], list[GitHubResponse]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method, path, *, headers, params=None, json_body=None):
        self.calls.append((method, path, dict(params or {})))
        key = (method, path)
        queue = self.responses.get(key)
        if not queue:
            raise AssertionError(f"unexpected request: {method} {path} {params}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def store() -> SecretStore:
    return SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))


def _project(session: Session) -> CaliberProject:
    project = CaliberProject(project_id=new_project_id(), name="Reconciliation", status="active")
    session.add(project)
    session.flush()
    return project


def _source(session: Session, project: CaliberProject) -> CaliberWorkspaceSource:
    source = CaliberWorkspaceSource(
        source_id=new_workspace_source_id(),
        project_id=project.project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id="github:owner/repo",
        display_path="owner/repo",
        default_branch="main",
        status="active",
        created_by="@test",
    )
    session.add(source)
    session.flush()
    return source


def _delivery_entry(
    *, delivery_id: int, guid: str, event: str = "pull_request", installation_id: int = 1001
) -> dict[str, object]:
    return {
        "id": delivery_id,
        "guid": guid,
        "event": event,
        "action": "opened",
        "delivered_at": "2026-09-19T00:00:00Z",
        "status": "OK",
        "installation_id": installation_id,
    }


# ---------------------------------------------------------------------------
# list_recent_deliveries
# ---------------------------------------------------------------------------


def test_list_recent_deliveries_parses_entries() -> None:
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(
                    status_code=200,
                    payload=[
                        _delivery_entry(delivery_id=1, guid="a"),
                        _delivery_entry(delivery_id=2, guid="b"),
                    ],
                )
            ]
        }
    )

    deliveries = list_recent_deliveries(transport, APP_JWT)

    assert [d.guid for d in deliveries] == ["a", "b"]
    assert deliveries[0].delivery_id == 1
    assert deliveries[0].event_type == "pull_request"
    assert deliveries[0].installation_id == "1001"


def test_list_recent_deliveries_filters_by_installation_id() -> None:
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(
                    status_code=200,
                    payload=[
                        _delivery_entry(delivery_id=1, guid="a", installation_id=1001),
                        _delivery_entry(delivery_id=2, guid="b", installation_id=2002),
                    ],
                )
            ]
        }
    )

    deliveries = list_recent_deliveries(transport, APP_JWT, installation_id="1001")

    assert [d.guid for d in deliveries] == ["a"]


def test_list_recent_deliveries_follows_link_header_pagination() -> None:
    page_one = GitHubResponse(
        status_code=200,
        payload=[_delivery_entry(delivery_id=1, guid="a")],
        headers={"Link": '<https://api.github.com/app/hooks/deliveries?cursor=xyz>; rel="next"'},
    )
    page_two = GitHubResponse(status_code=200, payload=[_delivery_entry(delivery_id=2, guid="b")])
    transport = _Transport({("GET", "/app/hooks/deliveries"): [page_one, page_two]})

    deliveries = list_recent_deliveries(transport, APP_JWT)

    assert [d.guid for d in deliveries] == ["a", "b"]
    assert transport.calls[1][2].get("cursor") == "xyz"


def test_list_recent_deliveries_stops_at_max_pages() -> None:
    looping_page = GitHubResponse(
        status_code=200,
        payload=[_delivery_entry(delivery_id=1, guid="a")],
        headers={"Link": '<https://api.github.com/app/hooks/deliveries?cursor=xyz>; rel="next"'},
    )
    transport = _Transport({("GET", "/app/hooks/deliveries"): [looping_page]})

    deliveries = list_recent_deliveries(transport, APP_JWT, max_pages=3)

    assert len(transport.calls) == 3
    assert len(deliveries) == 3


def test_list_recent_deliveries_skips_malformed_entries() -> None:
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(
                    status_code=200,
                    payload=[
                        {"id": 1},  # missing guid/event
                        "not-a-dict",
                        _delivery_entry(delivery_id=2, guid="b"),
                    ],
                )
            ]
        }
    )

    deliveries = list_recent_deliveries(transport, APP_JWT)

    assert [d.guid for d in deliveries] == ["b"]


def test_list_recent_deliveries_raises_on_non_success_status() -> None:
    transport = _Transport(
        {("GET", "/app/hooks/deliveries"): [GitHubResponse(status_code=401, payload={})]}
    )

    with pytest.raises(WebhookReconciliationError, match="status 401"):
        list_recent_deliveries(transport, APP_JWT)


def test_list_recent_deliveries_raises_when_response_is_not_a_list() -> None:
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(status_code=200, payload={"not": "a list"})
            ]
        }
    )

    with pytest.raises(WebhookReconciliationError, match="not a list"):
        list_recent_deliveries(transport, APP_JWT)


def test_next_cursor_returns_none_with_no_next_relation() -> None:
    from caliber.github_webhook_reconciliation import _next_cursor

    assert _next_cursor({"Link": '<https://api.github.com/x?page=1>; rel="prev"'}) is None
    assert _next_cursor({}) is None


def test_next_cursor_returns_none_when_next_link_has_no_cursor_param() -> None:
    from caliber.github_webhook_reconciliation import _next_cursor

    assert (
        _next_cursor({"Link": '<https://api.github.com/app/hooks/deliveries>; rel="next"'}) is None
    )


def test_next_cursor_reads_lowercase_link_header() -> None:
    from caliber.github_webhook_reconciliation import _next_cursor

    assert _next_cursor({"link": '<https://api.github.com/x?cursor=abc>; rel="next"'}) == "abc"


# ---------------------------------------------------------------------------
# redeliver
# ---------------------------------------------------------------------------


def test_redeliver_succeeds() -> None:
    transport = _Transport(
        {
            ("POST", "/app/hooks/deliveries/42/attempts"): [
                GitHubResponse(status_code=202, payload={})
            ]
        }
    )

    redeliver(transport, APP_JWT, 42)

    assert transport.calls == [("POST", "/app/hooks/deliveries/42/attempts", {})]


def test_redeliver_raises_on_failure_status() -> None:
    transport = _Transport(
        {
            ("POST", "/app/hooks/deliveries/42/attempts"): [
                GitHubResponse(status_code=404, payload={})
            ]
        }
    )

    with pytest.raises(WebhookReconciliationError, match="status 404"):
        redeliver(transport, APP_JWT, 42)


# ---------------------------------------------------------------------------
# find_missed_deliveries
# ---------------------------------------------------------------------------


def test_find_missed_deliveries_filters_out_already_recorded(db_session: Session) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    db_session.add(
        CaliberWorkspaceSourceEvent(
            source_event_id="WSSE-recorded",
            source_id=source.source_id,
            provider_delivery_id="already-recorded",
            event_type="pull_request",
            repository_id=source.canonical_repository_id,
            payload_sha256="a" * 64,
            status="received",
        )
    )
    db_session.flush()
    candidates = [
        MissedDelivery(1, "already-recorded", "pull_request", "opened", None, "OK", "1001"),
        MissedDelivery(2, "genuinely-missed", "pull_request", "opened", None, "OK", "1001"),
    ]

    missed = find_missed_deliveries(db_session, source.source_id, candidates)

    assert [d.guid for d in missed] == ["genuinely-missed"]


# ---------------------------------------------------------------------------
# reconcile_connection: end to end
# ---------------------------------------------------------------------------


def test_reconcile_connection_finds_and_redelivers_missing_deliveries(
    db_session: Session, store: SecretStore, rsa_private_key_pem: str
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="1001",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    db_session.add(
        CaliberWorkspaceSourceEvent(
            source_event_id="WSSE-recorded",
            source_id=source.source_id,
            provider_delivery_id="already-recorded",
            event_type="pull_request",
            repository_id=source.canonical_repository_id,
            payload_sha256="a" * 64,
            status="received",
        )
    )
    db_session.flush()
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(
                    status_code=200,
                    payload=[
                        _delivery_entry(
                            delivery_id=1, guid="already-recorded", installation_id=1001
                        ),
                        _delivery_entry(
                            delivery_id=2, guid="genuinely-missed", installation_id=1001
                        ),
                    ],
                )
            ],
            ("POST", "/app/hooks/deliveries/2/attempts"): [
                GitHubResponse(status_code=202, payload={})
            ],
        }
    )

    result = reconcile_connection(db_session, store, connection, transport)

    assert result.checked == 2
    assert [d.guid for d in result.missed] == ["genuinely-missed"]
    assert result.redelivery_requested == ("genuinely-missed",)
    # Only the missing delivery was ever redelivered -- the already-recorded
    # one never triggered a POST at all.
    redeliver_calls = [c for c in transport.calls if c[0] == "POST"]
    assert redeliver_calls == [("POST", "/app/hooks/deliveries/2/attempts", {})]


def test_reconcile_connection_converges_once_the_delivery_is_recorded(
    db_session: Session, store: SecretStore, rsa_private_key_pem: str
) -> None:
    """A second reconciliation call after the missed delivery lands in the
    inbox (as it would once GitHub's redelivery actually reaches the real
    webhook route) must not request it again."""
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="1001",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    transport = _Transport(
        {
            ("GET", "/app/hooks/deliveries"): [
                GitHubResponse(
                    status_code=200,
                    payload=[
                        _delivery_entry(delivery_id=1, guid="now-recorded", installation_id=1001)
                    ],
                )
            ]
        }
    )
    # Simulate the redelivery having already landed through the real
    # webhook route.
    db_session.add(
        CaliberWorkspaceSourceEvent(
            source_event_id="WSSE-now-recorded",
            source_id=source.source_id,
            provider_delivery_id="now-recorded",
            event_type="pull_request",
            repository_id=source.canonical_repository_id,
            payload_sha256="a" * 64,
            status="received",
        )
    )
    db_session.flush()

    result = reconcile_connection(db_session, store, connection, transport)

    assert result.missed == ()
    assert result.redelivery_requested == ()
    assert all(call[0] != "POST" for call in transport.calls)


def test_reconcile_connection_raises_when_private_key_is_unavailable(
    db_session: Session, store: SecretStore, rsa_private_key_pem: str
) -> None:
    from caliber.workspace_source_connections import private_key_secret_name

    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="1001",
        private_key=rsa_private_key_pem,
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    store.revoke(db_session, name=private_key_secret_name(connection.connection_id), actor="@admin")
    transport = _Transport()

    with pytest.raises(WebhookReconciliationError):
        reconcile_connection(db_session, store, connection, transport)
    assert transport.calls == []


def test_reconcile_connection_raises_when_jwt_minting_fails(
    db_session: Session, store: SecretStore
) -> None:
    project = _project(db_session)
    source = _source(db_session, project)
    connection = configure_connection(
        db_session,
        store,
        source=source,
        app_id="app-1",
        installation_id="1001",
        private_key="not-a-real-pem-key",
        webhook_secret=WEBHOOK_SECRET_VALUE,
        actor="@admin",
    )
    transport = _Transport()

    with pytest.raises(WebhookReconciliationError):
        reconcile_connection(db_session, store, connection, transport)
    assert transport.calls == []
