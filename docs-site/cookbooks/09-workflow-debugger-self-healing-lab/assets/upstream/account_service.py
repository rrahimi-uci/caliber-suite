#!/usr/bin/env python3
"""A stand-in account service for the Self-Healing Workflows lab. It is NOT a real system.

The lab's workflow looks an account up over HTTP before it decides a refund. In production that is
your own account API, and it fails in ways you do not control. This server reproduces those
failures on demand so that the lab can show a workflow run failing on an upstream error, and then
being fixed, without depending on anything outside this checkout.

    python3 account_service.py [PORT]        # default 8780, listens on 127.0.0.1 only

    GET /accounts?account_id=A-1007    200 {"account_id", "status", "tier", "refunds_90d"}
    GET /accounts?account_id=A-4040    404 {"error": "unknown account"}      (an unknown id)
    GET /accounts?account_id=A-9001    503 {"error": "account service unavailable"}   (an outage)
    GET /accounts?account_id=A-9002    500 {"error": "internal error"}                 (a server bug)
    GET /health                        200

Every other id in the lab's dataset resolves to a 200 so that the failures are always the same ones.
The server keeps no state: the answer depends only on the id.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

UNAVAILABLE = {
    "A-9001": (503, "account service unavailable"),
    "A-9002": (500, "internal error"),
}
UNKNOWN = {"A-4040", "A-4041"}
SUSPENDED = {"A-2210", "A-3300"}
PREMIUM = {"A-1007", "A-1020", "A-1033"}


def account_response(account_id: str) -> tuple[int, dict]:
    """The status code and JSON body this stand-in returns for an account id."""
    if account_id in UNAVAILABLE:
        status, message = UNAVAILABLE[account_id]
        return status, {"error": message}
    if account_id in UNKNOWN:
        return 404, {"error": "unknown account"}
    return 200, {
        "account_id": account_id,
        "status": "suspended" if account_id in SUSPENDED else "active",
        "tier": "premium" if account_id in PREMIUM else "standard",
        "refunds_90d": sum(ord(ch) for ch in account_id) % 4,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(200, {"ok": True})
        elif url.path == "/accounts":
            account_id = (parse_qs(url.query).get("account_id") or [""])[0]
            self._send(
                *account_response(account_id)
                if account_id
                else (400, {"error": "account_id is required"})
            )
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8780
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
