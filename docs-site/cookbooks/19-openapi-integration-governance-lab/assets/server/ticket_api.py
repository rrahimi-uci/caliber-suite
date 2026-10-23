#!/usr/bin/env python3
"""A stand-in Ticket API for the OpenAPI Integration Governance lab. It is NOT a real system.

CALIBER's OpenAPI Integrations feature imports a spec and then calls a *real* upstream server —
nothing about preview or publish is simulated. This 100-line server is that upstream: it serves its
own OpenAPI document (so the lab can import it by URL, reimport it, and diff two versions) and the
three ticket operations the document describes, behind the same bearer token CALIBER is configured
to send.

    python3 ticket_api.py [PORT]   # default 8781, listens on 127.0.0.1 only

Environment:
    OPENAPI_TICKET_TOKEN   the bearer token every call must present (default "ticket-api-token")
    TICKET_API_LEDGER  path to a JSONL file this server appends one line to per call it executes

Routes:
    GET  /health                    200, no auth
    GET  /openapi.yaml              200, no auth -- the document at its current version (see below)
    POST /admin/bump-spec           200 {"spec_version": 2} -- switches the served document (and the
                                     server's own validation) from v1 to v2. Lab-only: a real upstream
                                     would publish a new document at its own pace, not on a command.
    GET  /tickets                   200 {"total_count", "items": [...]}; v2 accepts ?status=
    POST /tickets                   201 {"ticket_id", "title", "priority", "status"}; v1 requires only
                                     "title"; v2 also requires "priority" (the breaking change the lab's
                                     reimport-and-diff step finds)
    GET  /tickets/{ticket_id}       200, or 404 {"error": "unknown ticket"}

Every route but /health and /openapi.yaml requires ``Authorization: Bearer <OPENAPI_TICKET_TOKEN>``;
anything else is a 401. The server keeps tickets in memory (gone on restart) and appends
``{"method", "path", "ok"}`` to the ledger for every call it actually executes -- refused and
unauthenticated calls never execute, so they never appear there. That is the proof the lab uses that
a draft CALIBER refuses to preview (or a workflow run that is still waiting on approval) never reached
this server.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPEC_V1 = """
openapi: 3.0.3
info:
  title: Ticket API
  version: "2026-08-11"
  description: Manage tickets.
servers:
  - url: https://tickets.example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /tickets:
    get:
      operationId: listTickets
      summary: List tickets
      tags: [tickets]
      responses:
        "200":
          description: ok
    post:
      operationId: createTicket
      summary: Create ticket
      tags: [tickets]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                title:
                  type: string
              required: [title]
      responses:
        "201":
          description: created
          content:
            application/json:
              schema:
                type: object
                properties:
                  ticket_id:
                    type: string
          links:
            GetCreatedTicket:
              operationId: getTicket
              parameters:
                ticket_id: "$response.body#/ticket_id"
  /tickets/{ticket_id}:
    get:
      operationId: getTicket
      summary: Get one ticket
      tags: [tickets]
      parameters:
        - in: path
          name: ticket_id
          required: true
          schema:
            type: string
      responses:
        "200":
          description: ok
"""

# v2 makes three changes, deliberately of three different kinds the lab's reimport-and-diff step
# tells apart:
#   1. listTickets gains an optional `status` filter and a reworded summary. CALIBER's diff compares
#      a fixed set of normalized fields (summary, side_effect_level, deprecated, auth_schemes,
#      request_body_required, request_content_types, response_statuses, tags) -- a new query
#      PARAMETER is not one of them, so only the summary edit shows up, as a non-breaking "changed".
#   2. getTicket is marked deprecated: true -- a tracked field, and the one change `_breaking_reason`
#      (caliber/src/caliber/integrations/openapi/diff.py) flags on its own: "operation was deprecated".
#   3. createTicket's body gains a *required* `priority` field. `request_body_required` (whether a
#      body is required at all) is tracked; which fields *within* it are required is not. The diff
#      reports zero changes for createTicket even though the lab's own write draft now fails at
#      runtime without `priority` -- the real behavioral break the diff does not catch. See the
#      README's "The trust boundary, honestly".
SPEC_V2 = """
openapi: 3.0.3
info:
  title: Ticket API
  version: "2026-09-02"
  description: Manage tickets.
servers:
  - url: https://tickets.example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /tickets:
    get:
      operationId: listTickets
      summary: List all tickets
      tags: [tickets]
      parameters:
        - in: query
          name: status
          required: false
          schema:
            type: string
            enum: [open, closed]
      responses:
        "200":
          description: ok
    post:
      operationId: createTicket
      summary: Create ticket
      tags: [tickets]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                title:
                  type: string
                priority:
                  type: string
                  enum: [low, normal, high]
              required: [title, priority]
      responses:
        "201":
          description: created
          content:
            application/json:
              schema:
                type: object
                properties:
                  ticket_id:
                    type: string
          links:
            GetCreatedTicket:
              operationId: getTicket
              parameters:
                ticket_id: "$response.body#/ticket_id"
  /tickets/{ticket_id}:
    get:
      operationId: getTicket
      summary: Get one ticket
      deprecated: true
      tags: [tickets]
      parameters:
        - in: path
          name: ticket_id
          required: true
          schema:
            type: string
      responses:
        "200":
          description: ok
"""

TOKEN = os.environ.get("OPENAPI_TICKET_TOKEN", "ticket-api-token")
_spec_version = 1
_tickets: list[dict] = []
_next_id = 1


def _ledger_path() -> str | None:
    return os.environ.get("TICKET_API_LEDGER")


def _ledger(method: str, path: str, ok: bool) -> None:
    ledger = _ledger_path()
    if not ledger:
        return
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method, "path": path, "ok": ok}) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(
        self, status: int, body: str, content_type: str = "application/yaml"
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(200, {"ok": True})
            return
        if url.path == "/openapi.yaml":
            self._send_text(200, SPEC_V2 if _spec_version == 2 else SPEC_V1)
            return
        if not self._authorized():
            self._send(401, {"error": "missing or wrong bearer token"})
            return
        if url.path == "/tickets":
            status = (parse_qs(url.query).get("status") or [None])[0]
            items = [t for t in _tickets if status is None or t["status"] == status]
            # Logged before the response is sent: a client must never be able to observe "success"
            # before the upstream's own record of it exists -- that record is this lab's proof.
            _ledger("GET", "/tickets", True)
            self._send(200, {"total_count": len(items), "items": items})
            return
        if url.path.startswith("/tickets/"):
            ticket_id = url.path.removeprefix("/tickets/")
            found = next((t for t in _tickets if t["ticket_id"] == ticket_id), None)
            if found is None:
                _ledger("GET", url.path, False)
                self._send(404, {"error": "unknown ticket"})
                return
            _ledger("GET", url.path, True)
            self._send(200, found)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        global _spec_version, _next_id
        url = urlparse(self.path)
        if url.path == "/admin/bump-spec":
            _spec_version = 2
            self._send(200, {"spec_version": _spec_version})
            return
        if url.path == "/admin/reset":
            # Back to a pristine v1 upstream. bump-spec is permanent for the process, so anything
            # that runs the lab twice against the same server (cookbook-run-all runs the SDK path
            # and then the UI path) resets here instead of restarting the process.
            _spec_version = 1
            _tickets.clear()
            _next_id = 1
            self._send(200, {"spec_version": _spec_version, "tickets": 0})
            return
        if not self._authorized():
            self._send(401, {"error": "missing or wrong bearer token"})
            return
        if url.path == "/tickets":
            body = self._body()
            title = body.get("title")
            priority = body.get("priority")
            if not title:
                _ledger("POST", "/tickets", False)
                self._send(400, {"error": "title is required"})
                return
            if _spec_version == 2 and not priority:
                _ledger("POST", "/tickets", False)
                self._send(400, {"error": "priority is required"})
                return
            ticket = {
                "ticket_id": f"TCK-{_next_id}",
                "title": title,
                "priority": priority or "normal",
                "status": "open",
            }
            _next_id += 1
            _tickets.append(ticket)
            _ledger("POST", "/tickets", True)
            self._send(201, ticket)
            return
        self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8781
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
