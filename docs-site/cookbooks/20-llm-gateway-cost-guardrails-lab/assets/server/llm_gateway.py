#!/usr/bin/env python3
"""A stand-in LLM gateway for the Metered LLM Gateway lab. It is NOT a real model.

CALIBER's gateway surface is about CALIBER's *view* of an OpenAI-compatible gateway: discovery,
routing, usage, and cost. This server plays the gateway-plus-provider role the way the lab's other
stand-ins play a ticketing SaaS or an account service: it exposes the same two surfaces the real
MLflow AI Gateway exposes -- the discovery listing CALIBER's Endpoints tab reads, and the
OpenAI-compatible chat-completions route CALIBER's workflow executor posts to -- and it bills a
flat, fixed token count per call so every cost number downstream is checkable by eye.

    python3 llm_gateway.py [PORT]   # default 8791, listens on 127.0.0.1 only

Environment:
    LAB20_GATEWAY_KEY    the bearer key chat completions require (default "lab20-gateway-key").
                         CALIBER holds this key as the secret ``secret://lab20-gateway-key``.
    LLM_GATEWAY_LEDGER   path to a JSONL file this server appends one line to per call

Routes:
    GET  /health                200, no auth
    GET  /api/2.0/endpoints/    200 -- the MLflow AI Gateway discovery shape CALIBER's /gateway
                                 route fetches; 503 while paused
    POST /v1/chat/completions   200 -- an OpenAI chat-completions response with a canned reply and
                                 fixed usage (1000 prompt + 1000 completion tokens, every call);
                                 401 without ``Authorization: Bearer <LAB20_GATEWAY_KEY>``;
                                 503 while paused
    POST /admin/pause           200 -- discovery and completions answer 503 until /admin/resume.
                                 Lab-only: it stands in for a gateway outage.
    POST /admin/resume          200
    POST /admin/reset           200 -- unpause; a fresh start for a re-run on the same process

Usage is fixed at 1000/1000 so the arithmetic stays visible: at the lab's pricing row of $1 per 1K
prompt tokens and $2 per 1K completion tokens, every call costs exactly $3.00. The ledger line
``{"path", "model", "ok", "prompt_tokens", "completion_tokens"}`` is written before the response is
sent: a client must never observe "success" before the gateway's own record of it exists -- that
record is this lab's proof, the same pattern the MCP and OpenAPI labs use.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROMPT_TOKENS = 1000
COMPLETION_TOKENS = 1000
REPLY = "Filed through the metered gateway."

ENDPOINTS = {
    "endpoints": [
        {
            "name": "chat-lab",
            "endpoint_type": "llm/v1/chat",
            "model": {"name": "lab-chat-mini", "provider": "lab"},
            "endpoint_url": "/v1/chat/completions",
            "limit": None,
        },
        {
            "name": "embeddings-lab",
            "endpoint_type": "llm/v1/embeddings",
            "model": {"name": "lab-embed-mini", "provider": "lab"},
            "endpoint_url": "/v1/embeddings",
            "limit": None,
        },
    ]
}

KEY = os.environ.get("LAB20_GATEWAY_KEY", "lab20-gateway-key")
_paused = False


def _ledger(
    path: str, model: str, ok: bool, prompt: int = 0, completion: int = 0
) -> None:
    ledger = os.environ.get("LLM_GATEWAY_LEDGER")
    if not ledger:
        return
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "path": path,
                    "model": model,
                    "ok": ok,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                }
            )
            + "\n"
        )


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
            return
        if self.path.rstrip("/") == "/api/2.0/endpoints":
            if _paused:
                self._send(503, {"error": "gateway paused"})
                return
            self._send(200, ENDPOINTS)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        global _paused
        if self.path == "/admin/pause":
            _paused = True
            self._send(200, {"paused": True})
            return
        if self.path == "/admin/resume":
            _paused = False
            self._send(200, {"paused": False})
            return
        if self.path == "/admin/reset":
            _paused = False
            self._send(200, {"paused": False})
            return
        if self.path == "/v1/chat/completions":
            body = self._body()
            model = str(body.get("model", ""))
            if _paused:
                _ledger("/v1/chat/completions", model, False)
                self._send(503, {"error": "gateway paused"})
                return
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                _ledger("/v1/chat/completions", model, False)
                self._send(
                    401,
                    {
                        "error": {
                            "message": "missing or wrong gateway key",
                            "type": "auth_error",
                        }
                    },
                )
                return
            # Logged before the response is sent: a caller must never observe "success"
            # before the gateway's own record of the call exists.
            _ledger(
                "/v1/chat/completions", model, True, PROMPT_TOKENS, COMPLETION_TOKENS
            )
            self._send(
                200,
                {
                    "id": "chatcmpl-lab20",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": REPLY},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": PROMPT_TOKENS,
                        "completion_tokens": COMPLETION_TOKENS,
                        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                    },
                },
            )
            return
        self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
