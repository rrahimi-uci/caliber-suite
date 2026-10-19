#!/usr/bin/env python3
"""A stand-in "model" for Cookbook 01: an OpenAI-compatible endpoint with NO intelligence.

Why this exists. CALIBER's Evaluations scorecard fails closed when the language model is
simulated: with ``CALIBER_LLM_PROVIDER=fake`` it answers ``400 evaluation requires a real LLM
provider`` instead of inventing scores. So on a machine with no model key you cannot watch a
prompt being evaluated, and you cannot see a weaker prompt version being caught. This script
fills that gap for the lab only: it is a tiny, deterministic HTTP server that speaks just enough of
``POST /v1/chat/completions`` for CALIBER's OpenAI client to talk to it.

What it does. It reads the SYSTEM message CALIBER sends (the prompt version under evaluation,
already rendered with one dataset row), pulls the support message out of it, and classifies it with a
handful of keyword rules. Then it formats the record the way the prompt asks:

* if the prompt demands "JSON ONLY ... no prose" (the strong v1), it answers with the bare JSON record;
* if the prompt dropped that rule (the weak v2), it answers the way a chatty model does: a friendly
  sentence and a markdown code fence around the JSON.

What it proves. That the evaluation pipeline (dataset -> prompt version -> scorers -> stored
per-example predictions) and the regression comparison are wired correctly, because the only thing
that differs between the two evaluated runs is the prompt text. What it does NOT prove: anything
about how a real model scores on your tickets. Its keyword rules are deliberately crude (it does not
read other languages, for example), so its accuracy is meaningless. Never read its scores as quality.

Usage:  python3 server.py [--port 8765]
Then start CALIBER with CALIBER_LLM_PROVIDER=openai, OPENAI_API_KEY=stand-in and
OPENAI_BASE_URL=http://127.0.0.1:8765/v1 (see the lab README).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Keyword rules, checked in order; first match wins. Intentionally small and English-only.
_INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    # "How do I..." / "Where is..." questions are how_to whatever their topic.
    ("how_to", ("how do i", "how can i", "where can i", "where is", "is there a way")),
    ("account", ("delete all of my data", "close my account", "someone else accessed")),
    (
        "billing",
        (
            "charged",
            "invoice",
            "refund",
            "bill ",
            "billing",
            "credit card",
            "cancel my subscription",
            "downgrade",
            "seats but",
        ),
    ),
    (
        "account",
        (
            "log in",
            "login",
            "locked",
            "password",
            "two-factor",
            "sso",
            "admin on",
            "reset",
        ),
    ),
    (
        "bug",
        (
            "error",
            "crash",
            "stuck",
            "blank page",
            "nothing loads",
            "lost",
            "wrong",
            "traceback",
            "live api key",
        ),
    ),
    ("feature_request", ("would be great", "please support", "any plans", "add a ", "wish")),
]

_INJECTION_MARKERS = (
    "ignore all previous",
    "ignore previous",
    "disregard your rules",
    "new instructions",
    "system:",
    "system prompt",
)
_URGENT_MARKERS = ("whole team", "lost ", "finance is escalating", "someone else accessed", "live api key")
_HIGH_MARKERS = ("charged twice", "charged $", "jump", "locked", "two-factor", "redirects", "500", "traceback")
_LOW_MARKERS = ("invoice as a pdf", "how do i", "where can i", "where is", "would be great", "please support")
_MESSAGE_RE = re.compile(r'Message:\s*"""\n?(.*?)\n?"""', re.DOTALL)
_STRICT_RE = re.compile(r"json\s+only", re.IGNORECASE)


def extract_message(system_prompt: str, user_prompt: str) -> str:
    """The support message: the quoted block of the rendered prompt, else the user turn."""
    match = _MESSAGE_RE.search(system_prompt)
    if match:
        return match.group(1)
    return user_prompt


def classify(message: str) -> dict[str, Any]:
    """Crude keyword classification into the lab's record shape."""
    text = message.strip().lower()
    injected = any(marker in text for marker in _INJECTION_MARKERS)
    # Only plain-ASCII, non-trivial text is understood; everything else is "not confident".
    readable = len(text) >= 8 and sum(ch.isascii() for ch in text) / max(len(text), 1) > 0.95
    intent = "unknown"
    if readable:
        for name, words in _INTENT_RULES:
            if any(word in text for word in words):
                intent = name
                break
    if injected and not any(w in text for w in ("invoice", "password")):
        intent = "unknown"
    needs_review = intent == "unknown" or injected
    if "delete all of my data" in text or "live api key" in text:
        needs_review = True
    if intent == "unknown":
        priority = "low" if "system prompt" in text else "medium"
    elif any(marker in text for marker in _URGENT_MARKERS):
        priority = "urgent"
    elif any(marker in text for marker in _HIGH_MARKERS):
        priority = "high"
    elif any(marker in text for marker in _LOW_MARKERS):
        priority = "low"
    else:
        priority = "medium"
    confidence = 0.4 if intent == "unknown" else 0.9
    reason = (
        "Not enough to classify with confidence."
        if intent == "unknown"
        else f"Keyword match for {intent}."
    )
    if injected:
        reason += " The message contains instructions aimed at the classifier; not followed."
    return {
        "intent": intent,
        "priority": priority,
        "confidence": confidence,
        "needs_review": needs_review,
        "reason": reason,
    }


def render_answer(system_prompt: str, record: dict[str, Any]) -> str:
    """Strict prompts get the bare record; prompts without the JSON-only rule get a chatty wrapper."""
    body = json.dumps(record)
    if _STRICT_RE.search(system_prompt):
        return body
    return (
        "Happy to help! Here is how I would route this message:\n\n"
        f"```json\n{json.dumps(record, indent=2)}\n```\n\n"
        "Let me know if you would like anything changed."
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "intake-stand-in/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # one quiet line per request
        sys.stderr.write("stand-in-model: " + (fmt % args) + "\n")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") in ("/health", "/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": "stand-in", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "only /v1/chat/completions is implemented"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        system_prompt = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        user_prompt = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        record = classify(extract_message(system_prompt, user_prompt))
        answer = render_answer(system_prompt, record)
        self._send(
            200,
            {
                "id": f"chatcmpl-stand-in-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "stand-in"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"stand-in-model: listening on http://{args.host}:{args.port}/v1\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
