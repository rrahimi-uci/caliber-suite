"""Tests for the read-only audit-log explorer + export (``routes/audit.py``).

The audit trail is admin-only and append-only; these tests seed rows directly
(bypassing :func:`caliber.audit.record`'s redactor, which is exercised
elsewhere) so timestamps and ordering are deterministic.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/audit-log"
EXPORT_PATH = "/ajax-api/2.0/mlflow/caliber/audit-log/export"
VIEWER_HEADERS = {"X-CALIBER-User": "@viewer"}

_CSV_HEADER = ["log_id", "timestamp", "actor", "action", "entity_type", "entity_id", "details"]


def _seed(session: Session) -> None:
    """Insert three audit rows spanning two actors, three actions, two days."""
    session.add_all(
        [
            CaliberAuditLog(
                timestamp=datetime(2026, 6, 1, 9, 0, 0),
                actor="@alice",
                action="approve",
                entity_type="workflow",
                entity_id="WF-1",
                details={"alias": "prod"},
            ),
            CaliberAuditLog(
                timestamp=datetime(2026, 6, 2, 10, 30, 0),
                actor="@bob",
                action="rollback",
                entity_type="workflow",
                entity_id="WF-1",
                details=None,
            ),
            CaliberAuditLog(
                timestamp=datetime(2026, 6, 3, 14, 15, 0),
                actor="@alice",
                action="dismiss",
                entity_type="verification_item",
                entity_id="VI-9",
                details={"reason": "duplicate"},
            ),
        ]
    )
    session.commit()


def test_list_returns_newest_first_with_total(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = client.get(LIST_PATH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["limit"] == 100
    assert data["offset"] == 0
    assert [entry["action"] for entry in data["entries"]] == ["dismiss", "rollback", "approve"]
    assert data["entries"][0]["details"] == {"reason": "duplicate"}


def test_filter_by_actor(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    data = client.get(LIST_PATH, params={"actor": "@alice"}).json()["data"]
    assert data["total"] == 2
    assert {entry["actor"] for entry in data["entries"]} == {"@alice"}


def test_filter_by_action_and_entity_type(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    data = client.get(
        LIST_PATH, params={"action": "approve", "entity_type": "workflow"}
    ).json()["data"]
    assert data["total"] == 1
    assert data["entries"][0]["entity_id"] == "WF-1"


def test_filter_by_date_window(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    data = client.get(
        LIST_PATH,
        params={"since": "2026-06-02T00:00:00", "until": "2026-06-02T23:59:59"},
    ).json()["data"]
    assert data["total"] == 1
    assert data["entries"][0]["action"] == "rollback"


def test_filter_accepts_trailing_z_timestamp(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    data = client.get(LIST_PATH, params={"since": "2026-06-03T00:00:00Z"}).json()["data"]
    assert data["total"] == 1
    assert data["entries"][0]["action"] == "dismiss"


def test_pagination_limit_and_offset(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    data = client.get(LIST_PATH, params={"limit": "1", "offset": "1"}).json()["data"]
    assert data["total"] == 3
    assert data["limit"] == 1
    assert data["offset"] == 1
    assert len(data["entries"]) == 1
    assert data["entries"][0]["action"] == "rollback"  # second-newest


def test_bad_since_timestamp_returns_400(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    assert client.get(LIST_PATH, params={"since": "not-a-date"}).status_code == 400


def test_list_forbidden_for_non_admin(client: TestClient) -> None:
    assert client.get(LIST_PATH, headers=VIEWER_HEADERS).status_code == 403


def test_csv_export(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = client.get(EXPORT_PATH, params={"actor": "@alice"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "caliber-audit-log.csv" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == _CSV_HEADER
    assert [row[3] for row in rows[1:]] == ["dismiss", "approve"]  # 2 alice rows, newest first
    assert json.loads(rows[1][6]) == {"reason": "duplicate"}


def test_json_export(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = client.get(EXPORT_PATH, params={"format": "json"})
    assert resp.status_code == 200
    assert "caliber-audit-log.json" in resp.headers["content-disposition"]
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 3
    assert body[0]["action"] == "dismiss"


def test_export_bad_format_returns_400(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    assert client.get(EXPORT_PATH, params={"format": "xml"}).status_code == 400


def test_export_forbidden_for_non_admin(client: TestClient) -> None:
    assert client.get(EXPORT_PATH, headers=VIEWER_HEADERS).status_code == 403
