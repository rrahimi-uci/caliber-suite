"""lookup_recent_deployments: body of a CALIBER ``python_code`` node.

Not a registered tool: paste this file into a Python Code node (or ship it in a workflow manifest,
see ``workflows/evidence-lookup-drill.workflow.json``). It returns a small synthetic deployment
fixture for one service and environment so a workflow has evidence to reason over.

The node runs in the tool sandbox: there are no ``import`` statements (``json``, ``re``, ``math``,
``datetime`` and ``statistics`` are already in scope) and only a restricted set of builtins.

Input (the node's ``input`` text): a JSON object ``{"service": "gateway", "environment": "prod"}``.
Anything that is not JSON raises, which is what the lab's Error Boundary drill relies on.

Output (``text`` is JSON, ``result`` the same data): ``{"deployments": [...]}``, newest first, each
``{sha, minutes_ago, risk, change_summary}``; ``[]`` is an honest "no recent deploy", never invented.
"""

# Synthetic fixtures, keyed "service/environment" (lower case). Newest deploy first.
_DEPLOYMENTS = {
    "gateway/prod": [
        {"sha": "a1b9f3c", "minutes_ago": 12, "risk": "high",
         "change_summary": "Refactor upstream connection pool + raise keep-alive limits"},
        {"sha": "7d2e0a4", "minutes_ago": 4300, "risk": "low",
         "change_summary": "Bump request-logging dependency (patch)"},
    ],
    "workflow-runner/prod": [
        {"sha": "c4f8821", "minutes_ago": 90, "risk": "low",
         "change_summary": "Add p99 latency histogram metric (instrumentation only)"},
    ],
    "worker/prod": [
        {"sha": "e90ab12", "minutes_ago": 40, "risk": "low",
         "change_summary": "Tune retry backoff for transient queue errors"},
    ],
    # checkout/prod and billing/staging intentionally have no recent deploy.
}


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    request = json.loads(input or run_input)  # raises on anything that is not JSON
    key = (str(request.get("service", "")).strip().lower() + "/"
           + str(request.get("environment", "")).strip().lower())
    data = {"deployments": [dict(d) for d in _DEPLOYMENTS.get(key, [])]}
    return {"text": json.dumps(data), "result": data}
