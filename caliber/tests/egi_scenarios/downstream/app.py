"""A disposable system of record for the EGI scenario tests.

It stores *document requests* for one synthetic mortgage case (``MORT-2026-000184``, the Harborlight
anchor case of the book's companion code) and nothing else: no decision logic. What makes it useful
is that it is independent of CALIBER, so a test can ask it how many times the business state really
changed, whatever CALIBER's own logs say.

Behaviour the effect tests rely on (gap analysis section 13, scenario V07):

* **Stable-key idempotency.** ``POST /cases/{id}/document-requests`` needs an ``Idempotency-Key``. The
  first request with a key commits one mutation and returns 201; every later request with the same key
  returns 200 with the stored body and commits nothing, *whatever* its ``If-Match``. A retry after a lost
  response must be able to read the original result.
* **Expected-version precondition.** An ``If-Match`` that is not the case's current version returns 412
  and commits nothing. The version increases by one per mutation; ``case_version`` in a response is the
  version *after* the mutation.
* **Query by key.** ``GET /document-requests?idempotency_key=...`` answers "did my request commit?".
* **Fault injection** (``/_test/*``, not in the OpenAPI document): the next ``POST`` can commit and then
  never answer (``drop_response_after_commit``), commit and answer late (``delay_response_seconds``), or
  fail before committing (``fail_before_commit``).
* **A mutation counter** (``GET /_test/mutations``) that counts committed mutations.

State lives in a SQLite file, so a restarted process keeps it; faults live in memory and are cleared by
a restart. Run it with ``python app.py --port N --db PATH``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol

SEED_PATH = Path(__file__).with_name("case_000184.json")

#: ``scope["extensions"]`` key under which :class:`DroppableH11Protocol` exposes the connection.
TRANSPORT_EXTENSION = "egi.transport"

FAULTS = ("none", "drop_response_after_commit", "delay_response_seconds", "fail_before_commit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    missing_documents TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_requests (
    request_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    document_type TEXT NOT NULL,
    case_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mutations (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);
"""


class _Store:
    """The SQLite state, one connection guarded by one lock (the service is single-process)."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        if self._db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0:
            self._load_seed()

    def _load_seed(self) -> None:
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        self._db.execute(
            "INSERT INTO cases (case_id, version, missing_documents) VALUES (?, ?, ?)",
            (seed["case_id"], seed["version"], json.dumps(seed["missing_documents"])),
        )

    def reset(self) -> None:
        with self._lock:
            self._db.executescript(
                "DELETE FROM document_requests; DELETE FROM mutations; DELETE FROM cases;"
                " DELETE FROM sqlite_sequence;"
            )
            self._load_seed()

    def mutations(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM mutations").fetchone()[0])

    def case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if row is None:
                return None
            requests = self._db.execute(
                "SELECT request_id, document_type, case_version FROM document_requests"
                " WHERE case_id = ? ORDER BY rowid",
                (case_id,),
            ).fetchall()
        return {
            "case_id": row["case_id"],
            "version": row["version"],
            "missing_documents": json.loads(row["missing_documents"]),
            "document_requests": [dict(r) for r in requests],
        }

    def request_by_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM document_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def create_request(
        self,
        case_id: str,
        key: str,
        document_type: str,
        expected_version: int | None,
        *,
        fail_before_commit: bool,
    ) -> tuple[int, dict[str, Any]]:
        """Return ``(status, body)``: 201 created, 200 replay, 404 no case, 412 stale, 500 injected."""
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM document_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return 200, _receipt(existing)
            case = self._db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if case is None:
                return 404, {"error": "case_not_found"}
            if expected_version is not None and expected_version != case["version"]:
                return 412, {"error": "stale_version", "current_version": case["version"]}
            if fail_before_commit:
                return 500, {"error": "injected_failure_before_commit"}
            self._db.execute("BEGIN")
            new_version = case["version"] + 1
            self._db.execute(
                "UPDATE cases SET version = ? WHERE case_id = ?", (new_version, case_id)
            )
            self._db.execute("INSERT INTO mutations DEFAULT VALUES")
            mutation_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]
            request_id = f"DR-{mutation_id:04d}"
            self._db.execute(
                "INSERT INTO document_requests"
                " (request_id, case_id, idempotency_key, document_type, case_version)"
                " VALUES (?, ?, ?, ?, ?)",
                (request_id, case_id, key, document_type, new_version),
            )
            self._db.execute("COMMIT")
            return 201, {"request_id": request_id, "case_version": new_version}


def _receipt(row: sqlite3.Row) -> dict[str, Any]:
    return {"request_id": row["request_id"], "case_version": row["case_version"]}


class _Faults:
    """The fault armed for the next ``POST``; one request consumes it."""

    def __init__(self) -> None:
        self.fault = "none"
        self.seconds = 0.0

    def arm(self, fault: str, seconds: float) -> None:
        self.fault, self.seconds = fault, seconds

    def take(self) -> tuple[str, float]:
        taken = (self.fault, self.seconds)
        self.fault, self.seconds = "none", 0.0
        return taken


def _openapi_document(base_url: str) -> dict[str, Any]:
    """OpenAPI 3.0 for the business routes; the ``/_test`` control routes are deliberately absent."""
    case_id = {"name": "case_id", "in": "path", "required": True, "schema": {"type": "string"}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "EGI downstream system of record", "version": "1.0.0"},
        "servers": [{"url": base_url}],
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health",
                    "summary": "Liveness probe",
                    "responses": {"200": {"description": "The service is up"}},
                }
            },
            "/cases/{case_id}": {
                "get": {
                    "operationId": "get_case",
                    "summary": "Read a case and its document requests",
                    "parameters": [case_id],
                    "responses": {
                        "200": {"description": "The case"},
                        "404": {"description": "No such case"},
                    },
                }
            },
            "/cases/{case_id}/document-requests": {
                "post": {
                    "operationId": "request_missing_document",
                    "summary": "Request a missing document for a case",
                    "parameters": [
                        case_id,
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "integer"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["document_type"],
                                    "properties": {"document_type": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "Created; one mutation committed"},
                        "200": {"description": "Replay of an earlier request with the same key"},
                        "400": {"description": "Missing Idempotency-Key or invalid body"},
                        "404": {"description": "No such case"},
                        "412": {"description": "If-Match is not the current case version"},
                    },
                }
            },
            "/document-requests": {
                "get": {
                    "operationId": "get_document_request_by_key",
                    "summary": "Find a document request by its idempotency key",
                    "parameters": [
                        {
                            "name": "idempotency_key",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "The stored request"},
                        "404": {"description": "No request with that key"},
                    },
                }
            },
        },
    }


class _DocumentRequests:
    """The ``POST`` route as a raw ASGI app, because dropping a response needs ``send`` and the transport."""

    def __init__(self, store: _Store, faults: _Faults) -> None:
        self._store, self._faults = store, faults

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        response = await self._handle(request, scope)
        if response is not None:
            await response(scope, receive, send)

    async def _handle(self, request: Request, scope: Scope) -> Response | None:
        key = request.headers.get("idempotency-key")
        if not key:
            return JSONResponse({"error": "idempotency_key_required"}, status_code=400)
        if_match = request.headers.get("if-match")
        try:
            expected = int(if_match) if if_match else None
            body = await request.json()
            document_type = str(body["document_type"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        fault, seconds = self._faults.take()
        status, payload = self._store.create_request(
            request.path_params["case_id"],
            key,
            document_type,
            expected,
            fail_before_commit=fault == "fail_before_commit",
        )
        if status == 201 and fault == "drop_response_after_commit":
            _drop_connection(scope)
            return None
        if status == 201 and fault == "delay_response_seconds":
            await asyncio.sleep(seconds)
        return JSONResponse(payload, status_code=status)


def _drop_connection(scope: Scope) -> None:
    """Close the client's connection without writing a response, after the mutation committed."""
    transport = scope.get("extensions", {}).get(TRANSPORT_EXTENSION)
    if transport is None:
        # Not served by DroppableH11Protocol (an in-process test client): an unhandled error is the
        # closest equivalent, since the client then also sees no response body.
        raise ConnectionAbortedError("response dropped after commit")
    transport.abort()


def create_app(db_path: str) -> Starlette:
    """Build the service on the SQLite file ``db_path`` (created and seeded when empty)."""
    store, faults = _Store(db_path), _Faults()

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def get_case(request: Request) -> Response:
        case = store.case(request.path_params["case_id"])
        if case is None:
            return JSONResponse({"error": "case_not_found"}, status_code=404)
        return JSONResponse(case)

    async def get_by_key(request: Request) -> Response:
        key = request.query_params.get("idempotency_key")
        if not key:
            return JSONResponse({"error": "idempotency_key_required"}, status_code=400)
        found = store.request_by_key(key)
        if found is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(found)

    async def openapi(request: Request) -> Response:
        return JSONResponse(_openapi_document(str(request.base_url).rstrip("/")))

    async def mutations(_: Request) -> Response:
        return JSONResponse({"count": store.mutations()})

    async def reset(_: Request) -> Response:
        store.reset()
        faults.arm("none", 0.0)
        return JSONResponse({"ok": True})

    async def arm_fault(request: Request) -> Response:
        body = await request.json()
        fault = body.get("fault")
        if fault not in FAULTS:
            return JSONResponse(
                {"error": "unknown_fault", "allowed": list(FAULTS)}, status_code=400
            )
        faults.arm(fault, float(body.get("seconds", 0)))
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/openapi.json", openapi),
            Route("/cases/{case_id}", get_case),
            Route(
                "/cases/{case_id}/document-requests",
                _DocumentRequests(store, faults),
                methods=["POST"],
            ),
            Route("/document-requests", get_by_key),
            Route("/_test/mutations", mutations),
            Route("/_test/reset", reset, methods=["POST"]),
            Route("/_test/faults", arm_fault, methods=["POST"]),
        ]
    )


class DroppableH11Protocol(H11Protocol):
    """uvicorn's h11 protocol, plus the connection in ``scope["extensions"]`` so the app can drop it.

    ASGI has no way to abort a connection without a response: an app that returns without sending one
    makes uvicorn answer 500. The request task is only scheduled, not run, when ``handle_events``
    returns, so adding the transport to the scope here is visible to the app.
    """

    def handle_events(self) -> None:
        super().handle_events()
        scope = getattr(self, "scope", None)
        if isinstance(scope, dict) and TRANSPORT_EXTENSION not in scope.get("extensions", {}):
            scope.setdefault("extensions", {})[TRANSPORT_EXTENSION] = self.transport


def build_server(db_path: str, port: int, host: str = "127.0.0.1") -> uvicorn.Server:
    config = uvicorn.Config(
        create_app(db_path), host=host, port=port, http=DroppableH11Protocol, log_level="warning"
    )
    return uvicorn.Server(config)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", default=os.environ.get("EGI_DOWNSTREAM_DB", ""))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db or EGI_DOWNSTREAM_DB is required")
    build_server(args.db, args.port, args.host).run()


if __name__ == "__main__":
    main()
