"""query_service_health — Caliber workflow `python_code` node body.

This is NOT a registered tool / shipped callable. There is no
`query_service_health` in any Caliber module, so paste this file's body into a
workflow **Python Code** node (``Compose → Workflows`` → drag a *Python Code*
node). It returns a small **synthetic runtime-health fixture** for the given
service/environment so the incident commander has live signals to reason over.
It versions with the workflow and uses **stdlib only**.

Node inputs (read from the upstream ``ingest`` node):
    service       str  -- e.g. "gateway" | "workflow-runner" | "worker" |
                          "checkout" | "billing". Case-insensitive.
    environment   str  -- e.g. "prod" | "staging". Case-insensitive.

Node outputs (returned on the node's ``result`` port; also as JSON on ``text``):
    error_rate       float -- fraction of failing requests in [0.0, 1.0], or
                              None when metrics are unavailable.
    latency_p99_ms   int   -- p99 latency in ms, or None when unavailable.
    saturation       float -- resource saturation in [0.0, 1.0] (cpu/mem/queue),
                              or None when unavailable.
    status           str   -- "healthy" | "degraded" | "unknown". "unknown"
                              means metrics are missing/partial — the commander
                              must treat that as an OPEN QUESTION, never a fact.

The fixtures are deliberately self-consistent with lookup_recent_deployments.py
and dataset/incident-cases.jsonl (same service/environment names):
    gateway/prod        -> DEGRADED (error-rate + latency spike) right after a
                           HIGH-risk deploy => clean post-deploy regression.
    workflow-runner/prod-> HEALTHY with a mild p99 blip => monitor, no approval.
    worker/prod         -> UNKNOWN (metrics missing) => can't confirm => gather
                           more evidence (do not present a guess as fact).
    checkout/prod       -> DEGRADED but with NO recent deploy => investigate.
    billing/staging     -> HEALTHY => monitor.
"""

import json

# --- Synthetic health fixtures (the only data; keep them at the top) ---------
# Keyed by (service, environment), both lower-cased.
_HEALTH = {
    ("gateway", "prod"): {
        "error_rate": 0.18,        # 18% of requests failing -- well above baseline
        "latency_p99_ms": 4200,    # ~4.2s p99 (baseline ~350ms)
        "saturation": 0.91,        # connection pool nearly exhausted
        "status": "degraded",
    },
    ("workflow-runner", "prod"): {
        "error_rate": 0.004,       # 0.4% -- within normal noise
        "latency_p99_ms": 1300,    # a mild blip, not an outage
        "saturation": 0.42,
        "status": "healthy",
    },
    ("worker", "prod"): {
        # Metrics pipeline is down for this service: signals are missing.
        "error_rate": None,
        "latency_p99_ms": None,
        "saturation": None,
        "status": "unknown",
    },
    ("checkout", "prod"): {
        "error_rate": 0.12,        # degraded, but NO recent deploy explains it
        "latency_p99_ms": 2600,
        "saturation": 0.77,
        "status": "degraded",
    },
    ("billing", "staging"): {
        "error_rate": 0.002,
        "latency_p99_ms": 410,
        "saturation": 0.31,
        "status": "healthy",
    },
}

# Returned for any service/environment not in the fixture: honestly unknown.
_UNKNOWN = {
    "error_rate": None,
    "latency_p99_ms": None,
    "saturation": None,
    "status": "unknown",
}


def query_service_health(service, environment) -> dict:
    """Pure, deterministic health lookup. Returns the node-output dict."""
    svc = (service or "").strip().lower()
    env = (environment or "").strip().lower()
    return dict(_HEALTH.get((svc, env), _UNKNOWN))


# --- python_code node entrypoint --------------------------------------------
# A CALIBER Python Code node calls ``run_python_node(...)`` and uses its RETURN
# value as the node output; a module-level ``result = ...`` would be DISCARDED
# (the runtime wraps a body lacking this def in a function with no return).
# Expose both ports: structured ``result`` and JSON ``text`` for downstream
# nodes / the agent prompt's {{ health }} variable.
def run_python_node(input=None, context=None, inputs=None, run_input=""):
    payload = inputs or {}
    data = query_service_health(
        service=payload.get("service", ""),
        environment=payload.get("environment", ""),
    )
    return {"text": json.dumps(data), "result": data}
