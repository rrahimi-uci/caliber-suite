#!/usr/bin/env python3
"""Offline OpenAI-compatible model for Cookbook 22's governed agent turn.

This is a deterministic model stand-in, not a production provider.  It emits
one valid ``request_missing_document`` tool call whenever CALIBER presents that
protected tool, allowing the lab to exercise the real OpenAI chat executor,
proposal binder, approval gate, effect dispatcher, and evidence path without a
paid model or network access.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ.get("LAB22_MODEL_KEY", "lab22-model-key")
CASE_ID = "MORT-2026-000184"
DOCUMENT_TYPE = "pay_statement"
MODEL = "lab22-scripted"


def _response(body: dict) -> dict:
    tools = body.get("tools") or []
    names = {
        str(tool.get("function", {}).get("name"))
        for tool in tools
        if isinstance(tool, dict)
    }
    messages = body.get("messages") or []
    user_text = next(
        (
            message.get("content", "")
            for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        "",
    )
    case_id = CASE_ID
    match = re.search(r"for ([A-Za-z0-9-]+)", str(user_text))
    if match:
        case_id = match.group(1)

    if "request_missing_document" in names:
        arguments = {
            "args": {"document_type": DOCUMENT_TYPE},
            "resource": f"cases/{case_id}/document-requests",
            "expected_resource_version": "1",
        }
        return {
            "id": "chatcmpl-lab22",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(body.get("model") or MODEL),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-lab22-document-request",
                                "type": "function",
                                "function": {
                                    "name": "request_missing_document",
                                    "arguments": json.dumps(arguments, separators=(",", ":")),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 32, "completion_tokens": 12, "total_tokens": 44},
        }
    return {
        "id": "chatcmpl-lab22",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "No governed tool was requested."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self._send(401, {"error": {"message": "missing or wrong model key"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) if length else b"{}")
        self._send(200, _response(body))

    def log_message(self, format: str, *args: object) -> None:
        del format, args


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
