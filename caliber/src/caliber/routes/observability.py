"""Observability — list MLflow traces + fetch a trace's span tree.

Backs the in-app, CALIBER-styled trace viewer (a mirror of MLflow's trace UI):
every agent/workflow run that CALIBER traces shows up here with its spans, tool
calls, timings, and redacted IO. The span tree reuses
:func:`caliber.trace_client.fetch_trace_spans`; the list wraps
``mlflow.search_traces``.

Every MLflow call is guarded — when tracing is off, the fake provider is in use,
or MLflow is unavailable, the routes degrade to an empty list/tree rather than
failing. Reads require an authenticated user.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from caliber.auth import require_user
from caliber.routes._deps import parse_json_object
from caliber.trace_client import (
    fetch_trace_detail,
    trace_experiment_id,
    trace_metadata_cost,
    trace_metadata_tokens,
    trace_session_id,
    trace_user,
)

logger = logging.getLogger("caliber.routes.observability")

PREFIX = "/ajax-api/2.0/mlflow/caliber"
TRACES_PATH = PREFIX + "/observability/traces"
TRACE_DETAIL_PATH = PREFIX + "/observability/traces/{trace_id}"
FEEDBACK_PATH = PREFIX + "/observability/traces/{trace_id}/feedback"
EXPERIMENTS_PATH = PREFIX + "/observability/experiments"
METRICS_PATH = PREFIX + "/observability/metrics"
ALLURE_REPORT_PATH = PREFIX + "/observability/allure-report"
ALLURE_REPORT_FILE_PATH = PREFIX + "/observability/allure-report/{path:path}"
_DEFAULT_ALLURE_DIR = "caliber/caliber-ui/allure-report"

_METRICS_MAX_TRACES = 1000
_METRICS_BUCKETS = 24

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_PREVIEW_CHARS = 2000
_MAX_EXPERIMENTS = 100


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if not raw:
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, value))


def _truncate(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text[:_PREVIEW_CHARS]


def _state_str(info: Any) -> str:
    """MLflow 3 ``TraceInfo.state`` is a ``TraceState`` enum — return its name."""
    state = getattr(info, "state", None)
    if state is None:
        return ""
    return str(getattr(state, "name", None) or getattr(state, "value", None) or state)


def _experiment_ids(mlflow_mod: Any, configured: str) -> list[str]:
    """Resolve which experiments to search.

    A configured ``CALIBER_TRACING_EXPERIMENT`` (numeric id or name) scopes the
    search to that experiment; otherwise we search every experiment so the view
    shows all traces.
    """
    value = str(configured or "").strip()
    if value:
        if value.isdigit():
            return [value]
        getter = getattr(mlflow_mod, "get_experiment_by_name", None)
        if callable(getter):
            try:
                experiment = getter(value)
            except Exception:
                experiment = None
            experiment_id = getattr(experiment, "experiment_id", None) if experiment else None
            if experiment_id is not None:
                return [str(experiment_id)]
        return []
    search = getattr(mlflow_mod, "search_experiments", None)
    if not callable(search):
        return []
    try:
        experiments = search(max_results=_MAX_EXPERIMENTS) or []
    except Exception as exc:
        logger.warning("search_experiments failed (%s)", exc)
        return []
    return [
        str(exp.experiment_id)
        for exp in experiments
        if getattr(exp, "experiment_id", None) is not None
    ]


def _experiments(mlflow_mod: Any) -> list[Any]:
    search = getattr(mlflow_mod, "search_experiments", None)
    if not callable(search):
        return []
    try:
        return list(search(max_results=_MAX_EXPERIMENTS) or [])
    except Exception as exc:
        logger.warning("search_experiments failed (%s)", exc)
        return []


def _experiment_name_map(mlflow_mod: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for exp in _experiments(mlflow_mod):
        experiment_id = getattr(exp, "experiment_id", None)
        if experiment_id is not None:
            names[str(experiment_id)] = str(getattr(exp, "name", "") or "")
    return names


def _int_param(request: Request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _raw_span_num(span: Any, key: str) -> float | None:
    """Read a numeric span attribute from a raw MLflow span (JSON-encoded values)."""
    attributes = getattr(span, "attributes", None)
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get(key)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return None


def _trace_summary(trace: Any, experiment_names: dict[str, str]) -> dict[str, Any]:
    """Compact, JSON-able summary of one MLflow trace for the list view."""
    info = getattr(trace, "info", None)
    data = getattr(trace, "data", None)
    spans = list(getattr(data, "spans", None) or [])
    tool_calls = sum(
        1 for span in spans if str(getattr(span, "span_type", "") or "").upper() == "TOOL"
    )
    trace_id = getattr(info, "trace_id", None)
    tags = getattr(info, "tags", None)
    name = ""
    if isinstance(tags, dict):
        name = tags.get("mlflow.traceName") or tags.get("mlflow.trace.name") or ""

    tokens = 0.0
    cost = 0.0
    have_tokens = have_cost = False
    for span in spans:
        token_value = _raw_span_num(span, "caliber.tokens")
        if token_value is not None:
            tokens += token_value
            have_tokens = True
        cost_value = _raw_span_num(span, "caliber.cost_usd")
        if cost_value is not None:
            cost += cost_value
            have_cost = True

    # Prefer CALIBER's per-span rollup; fall back to MLflow-native trace metadata
    # so token/cost show for any traced run (e.g. raw OpenAI autolog).
    total_tokens = int(tokens) if have_tokens else trace_metadata_tokens(info)
    total_cost = round(cost, 6) if have_cost else trace_metadata_cost(info)
    experiment_id = trace_experiment_id(trace)

    return {
        "trace_id": str(trace_id) if trace_id else None,
        "name": str(name) or (str(trace_id) if trace_id else "trace"),
        "status": _state_str(info),
        "experiment_id": experiment_id,
        "experiment_name": experiment_names.get(experiment_id or "", "") or None,
        "session_id": trace_session_id(info),
        "user": trace_user(info),
        "request_preview": _truncate(getattr(info, "request_preview", "") or ""),
        "response_preview": _truncate(getattr(info, "response_preview", "") or ""),
        "timestamp_ms": getattr(info, "request_time", None),
        "execution_time_ms": getattr(info, "execution_duration", None),
        "span_count": len(spans),
        "tool_call_count": tool_calls,
        "total_tokens": total_tokens,
        "cost_usd": total_cost,
    }


async def list_traces(request: Request) -> JSONResponse:
    """Return recent traces (most recent first) across the configured experiment(s)."""
    require_user(request)
    config = getattr(request.app.state, "config", None)
    configured = str(getattr(config, "tracing_experiment", "") or "") if config else ""
    limit = _limit(request)
    status_filter = request.query_params.get("status", "").strip().upper()
    session_filter = request.query_params.get("session", "").strip()
    experiment_filter = request.query_params.get("experiment_id", "").strip()
    since_ms = _int_param(request, "since_ms")
    try:
        import mlflow  # noqa: PLC0415

        names = _experiment_name_map(mlflow)
        # An explicit experiment_id scopes the search; otherwise honour the
        # configured CALIBER_TRACING_EXPERIMENT, else search every experiment.
        experiment_ids = (
            [experiment_filter] if experiment_filter else _experiment_ids(mlflow, configured)
        )
        if not experiment_ids:
            return JSONResponse({"data": {"traces": []}})
        traces = (
            mlflow.search_traces(
                experiment_ids=experiment_ids,
                max_results=limit,
                order_by=["timestamp_ms DESC"],
                return_type="list",
            )
            or []
        )
    except Exception as exc:
        logger.warning("trace search failed (%s); returning empty list", exc)
        return JSONResponse({"data": {"traces": []}})

    summaries: list[dict[str, Any]] = []
    for trace in traces:
        try:
            summary = _trace_summary(trace, names)
        except Exception:  # one malformed trace shouldn't drop the whole list
            logger.debug("skipping malformed trace in list", exc_info=True)
            continue
        if status_filter and summary["status"].upper() != status_filter:
            continue
        if session_filter and summary["session_id"] != session_filter:
            continue
        if since_ms is not None and (summary["timestamp_ms"] or 0) < since_ms:
            continue
        summaries.append(summary)
    return JSONResponse({"data": {"traces": summaries}})


async def list_experiments(request: Request) -> JSONResponse:
    """List MLflow experiments (id + name) for the Observability experiment filter."""
    require_user(request)
    try:
        import mlflow  # noqa: PLC0415

        experiments = _experiments(mlflow)
    except Exception as exc:
        logger.warning("experiment list failed (%s); returning empty list", exc)
        return JSONResponse({"data": {"experiments": []}})
    out = [
        {
            "experiment_id": str(getattr(exp, "experiment_id", "")),
            "name": str(getattr(exp, "name", "") or ""),
        }
        for exp in experiments
        if getattr(exp, "experiment_id", None) is not None
    ]
    return JSONResponse({"data": {"experiments": out}})


async def get_trace(request: Request) -> JSONResponse:
    """Return one trace's full detail: span tree, status, request/response, tags,
    token/cost rollup, and feedback assessments (the in-app MLflow mirror)."""
    require_user(request)
    trace_id = str(request.path_params["trace_id"])
    detail = fetch_trace_detail(trace_id)
    return JSONResponse(
        {
            "data": {
                "trace_id": detail.trace_id,
                "name": detail.name,
                "status": detail.status,
                "experiment_id": detail.experiment_id,
                "session_id": detail.session_id,
                "user": detail.user,
                "spans": detail.spans,
                "mlflow_url": detail.mlflow_url,
                "request": detail.request,
                "response": detail.response,
                "request_time_ms": detail.request_time_ms,
                "execution_time_ms": detail.execution_time_ms,
                "total_tokens": detail.total_tokens,
                "prompt_tokens": detail.prompt_tokens,
                "completion_tokens": detail.completion_tokens,
                "cost_usd": detail.cost_usd,
                "tags": detail.tags,
                "assessments": detail.assessments,
            }
        }
    )


async def post_feedback(request: Request) -> JSONResponse:
    """Attach a human feedback assessment to a trace (mlflow.log_feedback).

    Lets reviewers score/annotate a trace from CALIBER — the assessment lands on
    the MLflow trace and feeds CALIBER's assessment-polling / refinement loop, so
    the human-in-the-loop stays in-app. Returns the trace's refreshed assessments.
    """
    user = require_user(request)
    trace_id = str(request.path_params["trace_id"])
    data = await parse_json_object(request)
    name = (str(data.get("name") or "feedback").strip()) or "feedback"
    value = data.get("value")
    if value is None or not isinstance(value, (bool, int, float, str)):
        raise HTTPException(status_code=400, detail="'value' is required (bool, number, or string)")
    rationale_raw = data.get("rationale")
    rationale = str(rationale_raw).strip() if rationale_raw else None
    try:
        import mlflow  # noqa: PLC0415
        from mlflow.entities import AssessmentSource  # noqa: PLC0415

        mlflow.log_feedback(
            trace_id=trace_id,
            name=name,
            value=value,
            rationale=rationale,
            source=AssessmentSource(source_type="HUMAN", source_id=user),
        )
    except Exception as exc:
        logger.warning("log_feedback failed for %s (%s)", trace_id, exc)
        raise HTTPException(status_code=502, detail=f"failed to log feedback: {exc}") from exc

    detail = fetch_trace_detail(trace_id)
    return JSONResponse({"data": {"assessments": detail.assessments}})


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _bucketize(summaries: list[dict[str, Any]], since_ms: int | None) -> dict[str, Any]:
    """Bucket trace summaries into a fixed number of time windows for charting."""
    timestamps = [s["timestamp_ms"] for s in summaries if s.get("timestamp_ms")]
    if not timestamps:
        return {"buckets": [], "bucket_ms": 0, "totals": _totals([])}
    start = since_ms if since_ms is not None else min(timestamps)
    end = max(timestamps)
    span = max(1, end - start)
    bucket_ms = max(1, span // _METRICS_BUCKETS)
    buckets: dict[int, list[dict[str, Any]]] = {}
    for summary in summaries:
        ts = summary.get("timestamp_ms")
        if not ts or ts < start:
            continue
        idx = min(_METRICS_BUCKETS - 1, int((ts - start) // bucket_ms))
        buckets.setdefault(idx, []).append(summary)

    series: list[dict[str, Any]] = []
    for idx in range(_METRICS_BUCKETS):
        rows = buckets.get(idx, [])
        if not rows:
            continue
        durations = [float(r["execution_time_ms"]) for r in rows if r.get("execution_time_ms")]
        errors = sum(1 for r in rows if str(r.get("status", "")).upper() == "ERROR")
        series.append(
            {
                "ts": start + idx * bucket_ms,
                "count": len(rows),
                "error_count": errors,
                "error_rate": round(errors / len(rows), 4),
                "p50_ms": _percentile(durations, 0.50),
                "p95_ms": _percentile(durations, 0.95),
                "tokens": sum(int(r["total_tokens"]) for r in rows if r.get("total_tokens")),
                "cost_usd": round(sum(float(r["cost_usd"]) for r in rows if r.get("cost_usd")), 6),
            }
        )
    return {"buckets": series, "bucket_ms": bucket_ms, "totals": _totals(summaries)}


def _totals(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(summaries)
    errors = sum(1 for s in summaries if str(s.get("status", "")).upper() == "ERROR")
    durations = [float(s["execution_time_ms"]) for s in summaries if s.get("execution_time_ms")]
    return {
        "count": count,
        "error_rate": round(errors / count, 4) if count else 0.0,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "tokens": sum(int(s["total_tokens"]) for s in summaries if s.get("total_tokens")),
        "cost_usd": round(sum(float(s["cost_usd"]) for s in summaries if s.get("cost_usd")), 6),
    }


def _raw_span_str(span: Any, key: str) -> str | None:
    """Read a string span attribute from a raw MLflow span (JSON-encoded values)."""
    attributes = getattr(span, "attributes", None)
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get(key)
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            raw = decoded
    except (ValueError, TypeError):
        pass
    return raw.strip() or None


def gateway_usage_payload(
    *, experiment_filter: str, configured: str, since_ms: int | None
) -> dict[str, Any]:
    """Trace-derived usage for the Gateway page's Usage tab.

    Reuses the same trace search + :func:`_bucketize` as the observability
    monitor (volume / error rate / latency / tokens / cost over time) and adds a
    per-model token+cost rollup by walking span attributes (``caliber.model`` /
    ``caliber.tokens`` / ``caliber.cost_usd``). Degrades to empty on any error —
    this reads MLflow traces, independent of the gateway service's reachability.
    """
    empty: dict[str, Any] = {**_bucketize([], since_ms), "by_model": []}
    try:
        import mlflow  # noqa: PLC0415
    except Exception:  # MLflow not importable — no usage to show
        return empty
    try:
        names = _experiment_name_map(mlflow)
        experiment_ids = (
            [experiment_filter] if experiment_filter else _experiment_ids(mlflow, configured)
        )
        if not experiment_ids:
            return empty
        traces = (
            mlflow.search_traces(
                experiment_ids=experiment_ids,
                max_results=_METRICS_MAX_TRACES,
                order_by=["timestamp_ms DESC"],
                return_type="list",
            )
            or []
        )
    except Exception as exc:  # external store — degrade, never fail the page
        logger.warning("gateway usage search failed (%s); returning empty series", exc)
        return empty

    summaries: list[dict[str, Any]] = []
    by_model: dict[str, dict[str, float]] = {}
    for trace in traces:
        try:
            summaries.append(_trace_summary(trace, names))
        except Exception:
            logger.debug("skipping malformed trace in gateway usage", exc_info=True)
            continue
        data = getattr(trace, "data", None)
        for span in list(getattr(data, "spans", None) or []):
            tokens = _raw_span_num(span, "caliber.tokens")
            cost = _raw_span_num(span, "caliber.cost_usd")
            if tokens is None and cost is None:
                continue
            model = _raw_span_str(span, "caliber.model") or "unknown"
            agg = by_model.setdefault(model, {"calls": 0.0, "tokens": 0.0, "cost_usd": 0.0})
            agg["calls"] += 1.0
            if tokens is not None:
                agg["tokens"] += tokens
            if cost is not None:
                agg["cost_usd"] += cost

    payload = _bucketize(summaries, since_ms)
    payload["by_model"] = sorted(
        (
            {
                "model": model,
                "calls": int(v["calls"]),
                "tokens": int(v["tokens"]),
                "cost_usd": round(v["cost_usd"], 6),
            }
            for model, v in by_model.items()
        ),
        key=lambda r: (r["cost_usd"], r["tokens"]),
        reverse=True,
    )
    return payload


async def get_metrics(request: Request) -> JSONResponse:
    """Time-bucketed trace metrics for the monitoring dashboard: volume, error
    rate, latency p50/p95, tokens, and cost over time."""
    require_user(request)
    config = getattr(request.app.state, "config", None)
    configured = str(getattr(config, "tracing_experiment", "") or "") if config else ""
    experiment_filter = request.query_params.get("experiment_id", "").strip()
    since_ms = _int_param(request, "since_ms")
    try:
        import mlflow  # noqa: PLC0415

        names = _experiment_name_map(mlflow)
        experiment_ids = (
            [experiment_filter] if experiment_filter else _experiment_ids(mlflow, configured)
        )
        if not experiment_ids:
            return JSONResponse({"data": _bucketize([], since_ms)})
        traces = (
            mlflow.search_traces(
                experiment_ids=experiment_ids,
                max_results=_METRICS_MAX_TRACES,
                order_by=["timestamp_ms DESC"],
                return_type="list",
            )
            or []
        )
    except Exception as exc:
        logger.warning("metrics search failed (%s); returning empty series", exc)
        return JSONResponse({"data": _bucketize([], since_ms)})

    summaries: list[dict[str, Any]] = []
    for trace in traces:
        try:
            summary = _trace_summary(trace, names)
        except Exception:
            logger.debug("skipping malformed trace in metrics", exc_info=True)
            continue
        if since_ms is not None and (summary["timestamp_ms"] or 0) < since_ms:
            continue
        summaries.append(summary)
    return JSONResponse({"data": _bucketize(summaries, since_ms)})


def _allure_source(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    raw = str(getattr(config, "allure_report_dir", "") or "") if config else ""
    return raw or _DEFAULT_ALLURE_DIR


def _safe_rel(request: Request) -> str:
    """Sanitize the requested report-relative path (reject traversal)."""
    rel = (request.path_params.get("path") or "").strip("/")
    if ".." in rel.split("/"):
        raise HTTPException(status_code=403, detail="invalid path")
    return rel


def _is_missing_object(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        if code in ("NoSuchKey", "404", "NotFound"):
            return True
        http = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        return http == HTTPStatus.NOT_FOUND
    return "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc)


_ALLURE_NOT_READY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Allure report</title><style>body{font-family:system-ui,sans-serif;max-width:42rem;
margin:4rem auto;padding:0 1.5rem;color:#334155}code{background:#f1f5f9;padding:.1rem .35rem;
border-radius:.25rem}h1{font-size:1.4rem}</style></head><body>
<h1>No Allure report generated yet</h1>
<p>Generate the combined report (backend + frontend unit + e2e), then refresh:</p>
<pre><code>make allure-report</code></pre>
<p>It is written to the directory CALIBER serves
(<code>CALIBER_ALLURE_REPORT_DIR</code>, default
<code>caliber/caliber-ui/allure-report</code>).</p></body></html>"""


async def serve_allure_report(request: Request) -> FileResponse | HTMLResponse | Response:
    """Serve the generated Allure HTML report straight from CALIBER, so the
    in-app link works with no separate Allure server.

    The report source (``CALIBER_ALLURE_REPORT_DIR``) is either a local directory
    or an ``s3://bucket/prefix`` URI — the latter lets every node serve a report
    published to shared object storage (no shared filesystem needed). Falls back
    to a friendly 'not generated yet' page when the report is absent.
    """
    require_user(request)
    rel = _safe_rel(request)
    source = _allure_source(request)
    if source.startswith("s3://"):
        return _serve_allure_s3(request, source, rel)
    return _serve_allure_local(Path(source).resolve(), rel)


def _serve_allure_local(base: Path, rel: str) -> FileResponse | HTMLResponse:
    index = base / "index.html"
    if not index.is_file():
        return HTMLResponse(_ALLURE_NOT_READY_HTML, status_code=200)
    target = (base / rel).resolve() if rel else index
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="invalid path") from exc
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found in report")
    return FileResponse(target)


def _serve_allure_s3(request: Request, uri: str, rel: str) -> HTMLResponse | Response:
    from caliber.routes.object_store import _client, _inline_media_type  # noqa: PLC0415

    without_scheme = uri[len("s3://") :]
    bucket, _, prefix = without_scheme.partition("/")
    prefix = prefix.strip("/")
    name = rel or "index.html"
    key = f"{prefix}/{name}" if prefix else name
    try:
        obj = _client(request).get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
    except Exception as exc:
        if _is_missing_object(exc):
            if name == "index.html":
                return HTMLResponse(_ALLURE_NOT_READY_HTML, status_code=200)
            raise HTTPException(status_code=404, detail="not found in report") from exc
        logger.warning("allure s3 fetch failed for %s (%s)", key, exc)
        raise HTTPException(status_code=502, detail="failed to read report") from exc
    media_type = _inline_media_type(name, obj.get("ContentType"))
    return Response(content=body, media_type=media_type)


async def redirect_allure_report(request: Request) -> RedirectResponse:
    """``/allure-report`` → ``/allure-report/`` so the report's relative asset
    links resolve correctly."""
    require_user(request)
    return RedirectResponse(url=request.url.path + "/")


def register(app: Starlette) -> None:
    app.routes.append(Route(EXPERIMENTS_PATH, list_experiments, methods=["GET"]))
    app.routes.append(Route(METRICS_PATH, get_metrics, methods=["GET"]))
    app.routes.append(Route(ALLURE_REPORT_PATH, redirect_allure_report, methods=["GET"]))
    app.routes.append(Route(ALLURE_REPORT_FILE_PATH, serve_allure_report, methods=["GET"]))
    app.routes.append(Route(TRACES_PATH, list_traces, methods=["GET"]))
    app.routes.append(Route(FEEDBACK_PATH, post_feedback, methods=["POST"]))
    app.routes.append(Route(TRACE_DETAIL_PATH, get_trace, methods=["GET"]))
