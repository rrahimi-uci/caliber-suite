#!/usr/bin/env python3
"""A scripted stand-in for an OpenAI-compatible model, for running Cookbook 10 without a key.

CALIBER's Evaluations and the judge alignment check call a real model, and by default CALIBER runs
simulated providers, so on a default install both refuse (Evaluations answers 400, alignment reports
every row as an error). To exercise the real code paths end to end without a provider key, point
CALIBER at this server (OPENAI_BASE_URL) and give it any OPENAI_API_KEY. Everything CALIBER does is
real: it builds the judge through MLflow ``make_judge``, renders the prompt, sends the request, parses
the structured reply and computes the metrics. Only the *model* is scripted.

It is NOT a model and its verdicts say nothing about how a real judge would score this dataset:

* A judge request (the MLflow judge system prompt) is answered by a mechanical numeric-grounding rule
  on the ``outputs`` and ``expectations`` it carries. The ``FaithfulnessJudge`` rubric gets the strict
  rule; any other rubric gets the lenient rule (passes anything that is not empty or truncated).
* Any other request is answered by replaying the recorded output for the row whose input it carries:
  the "candidate" prompt (system text containing CANDIDATE_MARKER) gets the recorded answer, any
  other system text (the "baseline") gets a vague non-answer.

Standard library only. Usage: ``python3 stand_in_model_server.py PORT [DATASET.jsonl]``.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: A system prompt containing this phrase is the candidate; anything else is the baseline.
CANDIDATE_MARKER = "ONLY the evidence"
BASELINE_REPLY = "Thanks for reaching out. We are looking into this and will get back to you shortly."
DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1] / "dataset" / "aggregated-outputs.jsonl"
)

# A "fact token" is a number or identifier: $49, 5-7, 14:32, 6.1%, v2.9.1, acct_1432, sev2, 1,200.
_FACT = re.compile(r"[a-z_]*\$?\d+(?:[.,:\-]\d+)*[a-z%]*")


def fact_tokens(text: str) -> set[str]:
    return set(_FACT.findall(text.lower()))


def is_malformed(output: str) -> bool:
    """Empty, unparsable JSON, or cut off mid-sentence."""
    text = output.strip()
    if not text:
        return True
    if text[0] in "{[":
        try:
            json.loads(text)
        except ValueError:
            return True
        return False
    return text[-1] not in ".!?)\"'"


def strict_faithfulness(output: str, expectations: Any) -> tuple[bool, str]:
    """True only if the output is well formed and every fact token in it appears in the evidence."""
    if is_malformed(output):
        return (
            False,
            "stand-in: the output is empty or truncated, so its claims cannot be verified.",
        )
    evidence = fact_tokens(json.dumps(expectations, ensure_ascii=False))
    unsupported = sorted(fact_tokens(output) - evidence)
    if unsupported:
        return (
            False,
            f"stand-in: figures/identifiers not found in the evidence: {unsupported}.",
        )
    return (
        True,
        "stand-in: every figure and identifier in the output appears in the evidence.",
    )


def lenient_faithfulness(output: str, expectations: Any) -> tuple[bool, str]:
    """Passes anything that is not empty or truncated (a deliberately weak judge)."""
    if is_malformed(output):
        return False, "stand-in: the output is empty or truncated."
    return True, "stand-in: the output is well formed."


def _decode_after(text: str, label: str) -> Any:
    """Decode the JSON value that follows ``label`` (e.g. ``outputs: ``) in a judge user message."""
    index = text.find(label)
    if index < 0:
        return None
    value, _ = json.JSONDecoder().raw_decode(text[index + len(label) :])
    return value


def answer_judge(system: str, user: str) -> dict[str, Any]:
    instructions = system.split("Your task:", 1)[-1].split("You *must* format", 1)[0]
    outputs = _decode_after(user, "outputs: ")
    expectations = _decode_after(user, "expectations: ") or {}
    rule = (
        strict_faithfulness
        if "faithful" in instructions.lower()
        else lenient_faithfulness
    )
    verdict, rationale = rule(
        outputs if isinstance(outputs, str) else json.dumps(outputs), expectations
    )
    return {"result": verdict, "rationale": rationale}


def load_recorded_outputs(dataset: Path) -> list[tuple[dict[str, Any], str]]:
    rows = []
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            text = row["outputs"].get("reply") or row["outputs"].get("summary") or ""
            rows.append((row["input"], text))
    return rows


def answer_predict(
    system: str, user: str, recorded: list[tuple[dict[str, Any], str]]
) -> str:
    if CANDIDATE_MARKER not in system:
        return BASELINE_REPLY
    try:
        inputs = json.loads(user)
    except ValueError:
        inputs = None
    for known_inputs, text in recorded:
        if known_inputs == inputs or user in known_inputs.values():
            return text
    return "I do not have enough information to answer."


def make_handler(
    recorded: list[tuple[dict[str, Any], str]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._send(
                200, {"object": "list", "data": [{"id": "stand-in", "object": "model"}]}
            )

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            request = json.loads(self.rfile.read(length) or b"{}")
            messages = request.get("messages", [])
            system = "\n".join(
                m["content"]
                for m in messages
                if m.get("role") in ("system", "developer")
            )
            user = "\n".join(m["content"] for m in messages if m.get("role") == "user")
            if "expert judge" in system:
                content = json.dumps(answer_judge(system, user))
            else:
                content = answer_predict(system, user, recorded)
            self._send(
                200,
                {
                    "id": "stand-in-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.get("model", "stand-in"),
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("stand-in-model: " + fmt % args + "\n")

    return Handler


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    dataset = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DATASET
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(load_recorded_outputs(dataset))
    )
    print(
        f"stand-in model listening on http://127.0.0.1:{port}/v1 (dataset {dataset.name})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
