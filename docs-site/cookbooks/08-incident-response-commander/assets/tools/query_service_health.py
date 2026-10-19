"""query_service_health: body of a CALIBER ``python_code`` node.

Not a registered tool: paste this file into a Python Code node. It returns a small synthetic runtime
health fixture for one service and environment. Same sandbox rules as ``lookup_recent_deployments``:
no ``import`` statements (``json`` is already in scope), a JSON object as the node input.

Output: ``{error_rate, latency_p99_ms, saturation, status}`` with ``status`` one of ``healthy``,
``degraded`` or ``unknown``. ``unknown`` means metrics are missing; a caller must treat that as an
open question, never as a fact. Any service that is not in the fixture is honestly ``unknown``.
"""

_HEALTH = {
    "gateway/prod": {"error_rate": 0.18, "latency_p99_ms": 4200, "saturation": 0.91, "status": "degraded"},
    "workflow-runner/prod": {"error_rate": 0.004, "latency_p99_ms": 1300, "saturation": 0.42, "status": "healthy"},
    "worker/prod": {"error_rate": None, "latency_p99_ms": None, "saturation": None, "status": "unknown"},
    "checkout/prod": {"error_rate": 0.12, "latency_p99_ms": 2600, "saturation": 0.77, "status": "degraded"},
    "billing/staging": {"error_rate": 0.002, "latency_p99_ms": 410, "saturation": 0.31, "status": "healthy"},
}
_UNKNOWN = {"error_rate": None, "latency_p99_ms": None, "saturation": None, "status": "unknown"}


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    request = json.loads(input or run_input)
    key = (str(request.get("service", "")).strip().lower() + "/"
           + str(request.get("environment", "")).strip().lower())
    data = dict(_HEALTH.get(key, _UNKNOWN))
    return {"text": json.dumps(data), "result": data}
