"""The disposable downstream system of record behaves as the EGI scenarios assume.

Two layers: the app is exercised in-process (fast, and measurable by coverage), and the real process
the ``downstream`` fixture starts is used for what only a process can show: a connection dropped after
commit, state kept across a restart, nothing left running at teardown.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from tests.egi_scenarios.conftest import Downstream, free_port
from tests.egi_scenarios.downstream import app as downstream_app
from tests.egi_scenarios.downstream.app import SEED_PATH, build_server, create_app

CASE = "MORT-2026-000184"
POST = f"/cases/{CASE}/document-requests"
BODY = {"document_type": "pay_statement"}


def _key(name: str, headers: dict[str, str] | None = None) -> dict[str, str]:
    return {"Idempotency-Key": name, **(headers or {})}


@pytest.fixture
def app(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(str(tmp_path / "downstream.db"))) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean(request: pytest.FixtureRequest) -> None:
    """Tests that use the shared process start from the seed."""
    if "downstream" in request.fixturenames:
        request.getfixturevalue("downstream").reset()


def _mutations(client: TestClient) -> int:
    return int(client.get("/_test/mutations").json()["count"])


# ---------------------------------------------------------------- routes, in-process


def test_health(app: TestClient) -> None:
    response = app.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_the_seed_is_the_harborlight_case(app: TestClient) -> None:
    assert json.loads(SEED_PATH.read_text(encoding="utf-8"))["case_id"] == CASE
    case = app.get(f"/cases/{CASE}").json()
    assert case == {
        "case_id": CASE,
        "version": 1,
        "missing_documents": ["pay_statement"],
        "document_requests": [],
    }


def test_an_unknown_case_is_404(app: TestClient) -> None:
    assert app.get("/cases/NOPE").status_code == 404
    assert (
        app.post("/cases/NOPE/document-requests", json=BODY, headers=_key("k")).status_code == 404
    )
    assert _mutations(app) == 0


def test_create_then_replay_same_key_mutates_once(app: TestClient) -> None:
    created = app.post(POST, json=BODY, headers=_key("k1"))
    assert created.status_code == 201
    assert created.json() == {"request_id": "DR-0001", "case_version": 2}

    replay = app.post(POST, json=BODY, headers=_key("k1"))
    assert replay.status_code == 200
    assert replay.json() == created.json()

    assert _mutations(app) == 1
    case = app.get(f"/cases/{CASE}").json()
    assert case["version"] == 2
    assert [r["request_id"] for r in case["document_requests"]] == ["DR-0001"]


def test_a_replay_is_answered_even_though_its_if_match_is_now_stale(app: TestClient) -> None:
    """A retry after a lost response still carries the old version; it must read the original result."""
    first = app.post(POST, json=BODY, headers=_key("k1", {"If-Match": "1"}))
    assert first.status_code == 201
    retry = app.post(POST, json=BODY, headers=_key("k1", {"If-Match": "1"}))
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert _mutations(app) == 1


def test_a_new_key_gets_a_new_request_and_version(app: TestClient) -> None:
    app.post(POST, json=BODY, headers=_key("k1"))
    second = app.post(POST, json={"document_type": "id_card"}, headers=_key("k2"))
    assert second.status_code == 201
    assert second.json() == {"request_id": "DR-0002", "case_version": 3}
    assert _mutations(app) == 2


def test_stale_if_match_returns_412(app: TestClient) -> None:
    response = app.post(POST, json=BODY, headers=_key("k1", {"If-Match": "7"}))
    assert response.status_code == 412
    assert response.json() == {"error": "stale_version", "current_version": 1}
    assert _mutations(app) == 0
    assert app.get("/document-requests", params={"idempotency_key": "k1"}).status_code == 404


def test_a_current_if_match_is_accepted(app: TestClient) -> None:
    assert app.post(POST, json=BODY, headers=_key("k1", {"If-Match": "1"})).status_code == 201


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, BODY),  # no Idempotency-Key
        (_key("k"), {}),  # no document_type
        (_key("k"), []),  # body is not an object
        (_key("k", {"If-Match": "not-a-number"}), BODY),
    ],
)
def test_invalid_requests_are_400_and_commit_nothing(
    app: TestClient, headers: dict[str, str], body: object
) -> None:
    assert app.post(POST, json=body, headers=headers).status_code == 400
    assert _mutations(app) == 0


def test_a_body_that_is_not_json_is_400(app: TestClient) -> None:
    assert app.post(POST, content=b"{", headers=_key("k")).status_code == 400


def test_query_by_key(app: TestClient) -> None:
    assert app.get("/document-requests", params={"idempotency_key": "k1"}).status_code == 404
    app.post(POST, json=BODY, headers=_key("k1"))
    found = app.get("/document-requests", params={"idempotency_key": "k1"})
    assert found.status_code == 200
    assert found.json() == {
        "request_id": "DR-0001",
        "case_id": CASE,
        "idempotency_key": "k1",
        "document_type": "pay_statement",
        "case_version": 2,
    }
    assert app.get("/document-requests").status_code == 400


def test_openapi_document_lists_routes(app: TestClient) -> None:
    document = app.get("/openapi.json").json()
    assert document["openapi"].startswith("3.0")
    assert document["servers"] == [{"url": "http://testserver"}]
    operations = {
        (method.upper(), path): op["operationId"]
        for path, item in document["paths"].items()
        for method, op in item.items()
    }
    assert operations == {
        ("GET", "/health"): "health",
        ("GET", "/cases/{case_id}"): "get_case",
        ("POST", "/cases/{case_id}/document-requests"): "request_missing_document",
        ("GET", "/document-requests"): "get_document_request_by_key",
    }
    post = document["paths"]["/cases/{case_id}/document-requests"]["post"]
    headers = {p["name"]: p["required"] for p in post["parameters"] if p["in"] == "header"}
    assert headers == {"Idempotency-Key": True, "If-Match": False}
    assert not any(path.startswith("/_test") for path in document["paths"]), (
        "control routes stay private"
    )


# ---------------------------------------------------------------- faults, in-process


def test_fail_before_commit_mutates_nothing(app: TestClient) -> None:
    app.post("/_test/faults", json={"fault": "fail_before_commit"})
    failed = app.post(POST, json=BODY, headers=_key("k1"))
    assert failed.status_code == 500
    assert _mutations(app) == 0
    # The fault applied to one request only; the retry commits.
    assert app.post(POST, json=BODY, headers=_key("k1")).status_code == 201
    assert _mutations(app) == 1


def test_delay_response(app: TestClient) -> None:
    app.post("/_test/faults", json={"fault": "delay_response_seconds", "seconds": 0.3})
    started = time.monotonic()
    response = app.post(POST, json=BODY, headers=_key("k1"))
    assert response.status_code == 201
    assert time.monotonic() - started >= 0.3
    assert _mutations(app) == 1
    started = time.monotonic()
    assert app.post(POST, json=BODY, headers=_key("k2")).status_code == 201
    assert time.monotonic() - started < 0.3, "the delay fault is consumed by one request"


def test_a_fault_is_not_consumed_by_a_replay_that_commits_nothing(app: TestClient) -> None:
    app.post(POST, json=BODY, headers=_key("k1"))
    app.post("/_test/faults", json={"fault": "drop_response_after_commit"})
    replay = app.post(POST, json=BODY, headers=_key("k1"))
    assert replay.status_code == 200, "a replay commits nothing, so there is nothing to drop"


def test_dropping_without_the_droppable_server_surfaces_as_an_error(app: TestClient) -> None:
    app.post("/_test/faults", json={"fault": "drop_response_after_commit"})
    with pytest.raises(ConnectionAbortedError):
        app.post(POST, json=BODY, headers=_key("k1"))
    assert _mutations(app) == 1, "the mutation committed before the response was dropped"


def test_an_unknown_fault_is_rejected(app: TestClient) -> None:
    response = app.post("/_test/faults", json={"fault": "meteor"})
    assert response.status_code == 400
    assert "drop_response_after_commit" in response.json()["allowed"]


def test_reset_restores_seed(app: TestClient) -> None:
    app.post(POST, json=BODY, headers=_key("k1"))
    app.post("/_test/faults", json={"fault": "fail_before_commit"})
    assert app.post("/_test/reset").status_code == 200
    assert _mutations(app) == 0
    assert app.get(f"/cases/{CASE}").json()["version"] == 1
    assert app.get("/document-requests", params={"idempotency_key": "k1"}).status_code == 404
    # Reset also clears the armed fault, and request ids start over.
    assert app.post(POST, json=BODY, headers=_key("k1")).json()["request_id"] == "DR-0001"


def test_a_database_that_already_has_cases_is_not_reseeded(tmp_path: Path) -> None:
    db = str(tmp_path / "d.db")
    with TestClient(create_app(db)) as first:
        first.post(POST, json=BODY, headers=_key("k1"))
    with TestClient(create_app(db)) as second:
        assert second.get(f"/cases/{CASE}").json()["version"] == 2


# ---------------------------------------------------------------- the real server, in a thread


def test_drop_response_in_a_served_app_closes_the_connection(tmp_path: Path) -> None:
    """Served by ``DroppableH11Protocol`` the response is dropped for real, not turned into a 500."""
    port = free_port()
    server = build_server(str(tmp_path / "d.db"), port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            client.post("/_test/faults", json={"fault": "drop_response_after_commit"})
            with pytest.raises(httpx.TransportError):
                client.post(POST, json=BODY, headers=_key("k1"))
            assert client.get("/_test/mutations").json() == {"count": 1}
            assert (
                client.get("/document-requests", params={"idempotency_key": "k1"}).status_code
                == 200
            )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


# ---------------------------------------------------------------- the command line


class _StubServer:
    def run(self) -> None:
        self.ran = True


def test_main_builds_the_server_from_its_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    stub = _StubServer()

    def fake_build(db: str, port: int, host: str = "127.0.0.1") -> _StubServer:
        seen.update(db=db, port=port, host=host)
        return stub

    monkeypatch.setattr(downstream_app, "build_server", fake_build)
    downstream_app.main(["--port", "4321", "--db", str(tmp_path / "x.db")])
    assert seen == {"db": str(tmp_path / "x.db"), "port": 4321, "host": "127.0.0.1"}
    assert stub.ran is True


def test_main_takes_the_database_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("EGI_DOWNSTREAM_DB", str(tmp_path / "env.db"))
    monkeypatch.setattr(
        downstream_app,
        "build_server",
        lambda db, port, host="127.0.0.1": seen.update(db=db) or _StubServer(),
    )
    downstream_app.main(["--port", "1"])
    assert seen["db"] == str(tmp_path / "env.db")


def test_main_requires_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EGI_DOWNSTREAM_DB", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        downstream_app.main(["--port", "1"])
    assert exit_info.value.code == 2


# ---------------------------------------------------------------- the process fixture


def test_drop_response_after_commit_mutates_once(downstream: Downstream) -> None:
    """V07's lost receipt: the client sees a transport error, the downstream state changed once."""
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(httpx.TransportError):
        downstream.client.post(POST, json=BODY, headers=_key("lost"))
    assert downstream.mutations() == 1

    retry = downstream.client.post(POST, json=BODY, headers=_key("lost"))
    assert retry.status_code == 200, "the retry reads the committed result"
    assert downstream.mutations() == 1, "and does not mutate again"


def test_query_helper(downstream: Downstream) -> None:
    assert downstream.query("never-sent") is None
    downstream.client.post(POST, json=BODY, headers=_key("sent"))
    found = downstream.query("sent")
    assert found is not None
    assert found["document_type"] == "pay_statement"
    assert downstream.mutations() == 1


def test_restart_keeps_state(downstream: Downstream) -> None:
    downstream.client.post(POST, json=BODY, headers=_key("before-restart"))
    version = downstream.client.get(f"/cases/{CASE}").json()["version"]
    assert version == 2

    downstream.restart()

    assert downstream.client.get(f"/cases/{CASE}").json()["version"] == version
    assert downstream.mutations() == 1
    assert downstream.query("before-restart") is not None


def test_a_restart_clears_an_armed_fault(downstream: Downstream) -> None:
    downstream.set_fault("fail_before_commit")
    downstream.restart()
    assert downstream.client.post(POST, json=BODY, headers=_key("k1")).status_code == 201


def test_fixture_starts_within_5_seconds(downstream: Downstream) -> None:
    started = time.monotonic()
    downstream.restart()
    assert time.monotonic() - started < 5.0
    assert downstream.running


def test_stop_leaves_no_process_behind(tmp_path: Path) -> None:
    handle = Downstream(tmp_path / "own")
    handle.start()
    assert handle.running
    proc = handle._proc
    assert proc is not None
    handle.close()
    assert not handle.running
    assert proc.poll() is not None
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{handle.base_url}/health", timeout=1)


def test_a_process_that_exits_at_startup_is_reported_with_its_log(tmp_path: Path) -> None:
    handle = Downstream(tmp_path / "bad")
    # A path that cannot be a database file makes the service exit during startup.
    handle.db_path = str(tmp_path / "missing-dir" / "x.db")
    with pytest.raises(RuntimeError, match="exited at startup"):
        handle.start()
    handle.close()
