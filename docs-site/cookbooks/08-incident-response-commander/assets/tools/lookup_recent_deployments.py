"""lookup_recent_deployments — Caliber workflow `python_code` node body.

This is NOT a registered tool / shipped callable. There is no
`lookup_recent_deployments` in any Caliber module, so paste this file's body
into a workflow **Python Code** node (``Compose → Workflows`` → drag a *Python
Code* node). It returns a small **synthetic deployment fixture** for the given
service/environment so the incident workflow has evidence to reason over. It
versions with the workflow and uses **stdlib only**.

Node inputs (read from the upstream ``ingest`` node):
    service       str  -- e.g. "gateway" | "workflow-runner" | "worker" |
                          "checkout" | "billing". Case-insensitive.
    environment   str  -- e.g. "prod" | "staging". Case-insensitive.

Node outputs (returned on the node's ``result`` port; also as JSON on ``text``):
    deployments   list -- recent deploys, newest first, each:
        {
          "sha":            str,   # short commit sha
          "deployed_at":    str,   # ISO-8601 UTC timestamp
          "change_summary": str,   # one-line human description
          "risk":           str,   # "low" | "medium" | "high"
        }
                          Empty list ([]) when there is no recent deploy for
                          that service/environment (an honest "no evidence"
                          signal — the commander must NOT invent one).

The fixtures are deliberately self-consistent with query_service_health.py and
dataset/incident-cases.jsonl (same service/environment names):
    gateway/prod        -> a recent HIGH-risk deploy   (pairs w/ degraded health
                           => clean post-deploy regression => rollback).
    workflow-runner/prod-> a recent LOW-risk deploy    (pairs w/ a healthy blip
                           => monitor, no approval).
    worker/prod         -> a recent LOW-risk deploy    (pairs w/ UNKNOWN health
                           => conflicting/incomplete => gather_more_evidence).
    checkout/prod       -> NO recent deploy ([])       (pairs w/ degraded health
                           => not post-deploy => investigate).
    billing/staging     -> NO recent deploy ([])       (pairs w/ healthy
                           => monitor).
"""

import json

# --- Synthetic deployment fixtures (the only data; keep them at the top) -----
# Keyed by (service, environment), both lower-cased. Newest deploy first.
_DEPLOYMENTS = {
    ("gateway", "prod"): [
        {
            "sha": "a1b9f3c",
            "deployed_at": "2026-06-24T08:12:00Z",
            "change_summary": "Refactor upstream connection pool + raise keep-alive limits",
            "risk": "high",
        },
        {
            "sha": "7d2e0a4",
            "deployed_at": "2026-06-21T15:40:00Z",
            "change_summary": "Bump request-logging dependency (patch)",
            "risk": "low",
        },
    ],
    ("workflow-runner", "prod"): [
        {
            "sha": "c4f8821",
            "deployed_at": "2026-06-23T19:05:00Z",
            "change_summary": "Add p99 latency histogram metric (instrumentation only)",
            "risk": "low",
        },
    ],
    ("worker", "prod"): [
        {
            "sha": "e90ab12",
            "deployed_at": "2026-06-24T06:55:00Z",
            "change_summary": "Tune retry backoff for transient queue errors",
            "risk": "low",
        },
    ],
    # checkout/prod and billing/staging intentionally have NO recent deploy.
}


def lookup_recent_deployments(service, environment) -> dict:
    """Pure, deterministic deployment lookup. Returns the node-output dict."""
    svc = (service or "").strip().lower()
    env = (environment or "").strip().lower()
    deployments = _DEPLOYMENTS.get((svc, env), [])
    # Return copies so a downstream node can't mutate the fixture in place.
    return {"deployments": [dict(d) for d in deployments]}


# --- python_code node entrypoint --------------------------------------------
# A CALIBER Python Code node calls ``run_python_node(...)`` and uses its RETURN
# value as the node output; a module-level ``result = ...`` would be DISCARDED
# (the runtime wraps a body lacking this def in a function with no return).
# Expose both ports: structured ``result`` and JSON ``text`` for downstream
# nodes / the agent prompt's {{ deployments }} variable.
def run_python_node(input=None, context=None, inputs=None, run_input=""):
    payload = inputs or {}
    data = lookup_recent_deployments(
        service=payload.get("service", ""),
        environment=payload.get("environment", ""),
    )
    return {"text": json.dumps(data), "result": data}
