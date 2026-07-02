"""Coverage-focused tests for ``caliber.routes.observability``.

Targets the error/edge branches the happy-path suite (``test_routes_observability.py``)
doesn't reach: the ``limit``/``since_ms`` query-param parsing edge cases, the
named-experiment resolution path in ``_experiment_ids``, MLflow-unavailable /
MLflow-raises degradation across ``list_traces``/``list_experiments``/``get_metrics``,
malformed-trace skipping, the raw-span-attribute decoders (``_raw_span_num`` /
``_raw_span_str``), the ``gateway_usage_payload`` helper (no HTTP route of its own),
and the Allure report file server's traversal / S3-error branches.

MLflow is stubbed the same way the happy-path suite does it: monkeypatching
attributes on the real (already-imported) ``mlflow`` module, which the route's
function-local ``import mlflow`` resolves to the same object.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import caliber.routes.observability as obs_routes

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OBS = PREFIX + "/observability"


def _trace(
    trace_id: str,
    *,
    ts: int | None = 100,
    state: str = "OK",
) -> SimpleNamespace:
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            state=SimpleNamespace(name=state),
            request_time=ts,
            execution_duration=1,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(spans=[]),
    )


class _RaisingInfoTrace:
    """A trace whose ``.info`` access raises — simulates a malformed trace.

    ``getattr(trace, "info", None)`` only swallows ``AttributeError``; any other
    exception raised by a property propagates, letting us exercise the
    "one bad trace shouldn't drop the whole list" guards.
    """

    @property
    def info(self) -> SimpleNamespace:
        raise RuntimeError("malformed trace")

    data = SimpleNamespace(spans=[])


def _set_allure_dir(client: TestClient, path: object) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"allure_report_dir": str(path)}
    )


# ---------------------------------------------------------------------------
# _limit
# ---------------------------------------------------------------------------


def test_limit_invalid_value_falls_back_to_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    captured: dict[str, object] = {}

    def fake_search(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", fake_search)

    resp = client.get(f"{OBS}/traces", params={"limit": "not-a-number"})
    assert resp.status_code == 200, resp.text
    assert captured["max_results"] == 50  # _DEFAULT_LIMIT


def test_limit_clamped_to_bounds(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    captured: dict[str, object] = {}

    def fake_search(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", fake_search)

    # Above the max clamps down to _MAX_LIMIT.
    resp = client.get(f"{OBS}/traces", params={"limit": "5000"})
    assert resp.status_code == 200, resp.text
    assert captured["max_results"] == 200

    # Below 1 clamps up to 1.
    resp = client.get(f"{OBS}/traces", params={"limit": "0"})
    assert resp.status_code == 200, resp.text
    assert captured["max_results"] == 1


# ---------------------------------------------------------------------------
# _state_str
# ---------------------------------------------------------------------------


def test_state_str_handles_missing_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="no-state",
            state=None,
            request_time=1,
            execution_duration=1,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(spans=[]),
    )
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_k: [trace])

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]["traces"][0]
    assert summary["status"] == ""


# ---------------------------------------------------------------------------
# _experiment_ids — named-experiment resolution + degrade-to-empty branches
# ---------------------------------------------------------------------------


def test_numeric_tracing_experiment_scopes_directly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purely numeric ``CALIBER_TRACING_EXPERIMENT`` is used as-is, without a
    ``get_experiment_by_name`` round-trip."""
    import mlflow

    client.app.state.config = client.app.state.config.model_copy(update={"tracing_experiment": "7"})
    monkeypatch.setattr(mlflow, "search_experiments", lambda max_results=100: [])

    captured: dict[str, object] = {}

    def fake_search(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mlflow, "search_traces", fake_search)

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert captured["experiment_ids"] == ["7"]


def test_named_tracing_experiment_resolves_via_get_experiment_by_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    client.app.state.config = client.app.state.config.model_copy(
        update={"tracing_experiment": "my-exp"}
    )
    monkeypatch.setattr(
        mlflow, "get_experiment_by_name", lambda name: SimpleNamespace(experiment_id="42")
    )
    monkeypatch.setattr(mlflow, "search_experiments", lambda max_results=100: [])

    captured: dict[str, object] = {}

    def fake_search(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mlflow, "search_traces", fake_search)

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert captured["experiment_ids"] == ["42"]


def test_named_tracing_experiment_lookup_raises_returns_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    client.app.state.config = client.app.state.config.model_copy(
        update={"tracing_experiment": "missing-exp"}
    )

    def boom(_name: str) -> object:
        raise RuntimeError("no mlflow connection")

    monkeypatch.setattr(mlflow, "get_experiment_by_name", boom)
    monkeypatch.setattr(mlflow, "search_experiments", lambda max_results=100: [])

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["traces"] == []


def test_search_experiments_not_callable_degrades_to_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "search_experiments", None)

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["traces"] == []


def test_search_experiments_raises_degrades_to_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    def boom(max_results: int = 100) -> object:
        raise RuntimeError("mlflow tracking server down")

    monkeypatch.setattr(mlflow, "search_experiments", boom)

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["traces"] == []


# ---------------------------------------------------------------------------
# list_experiments — degrade branches
# ---------------------------------------------------------------------------


def test_list_experiments_search_not_callable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "search_experiments", None)

    resp = client.get(f"{OBS}/experiments")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["experiments"] == []


def test_list_experiments_outer_exception_returns_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_mlflow_mod: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(obs_routes, "_experiments", boom)

    resp = client.get(f"{OBS}/experiments")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["experiments"] == []


# ---------------------------------------------------------------------------
# _int_param (since_ms)
# ---------------------------------------------------------------------------


def test_since_ms_invalid_value_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_k: [_trace("t1", ts=100)])

    resp = client.get(f"{OBS}/traces", params={"since_ms": "not-a-number"})
    assert resp.status_code == 200, resp.text
    assert [t["trace_id"] for t in resp.json()["data"]["traces"]] == ["t1"]


# ---------------------------------------------------------------------------
# _raw_span_num / _raw_span_str — direct unit tests for the value-decoding
# branches (bool guard, direct numeric, malformed JSON, unsupported type).
# ---------------------------------------------------------------------------


def test_raw_span_num_edge_cases() -> None:
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": True}), "k") is None
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": False}), "k") is None
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": 42}), "k") == 42
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": 4.5}), "k") == 4.5
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": "not-json"}), "k") is None
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": None}), "k") is None
    assert obs_routes._raw_span_num(SimpleNamespace(attributes={"k": [1, 2]}), "k") is None


def test_raw_span_str_edge_cases() -> None:
    assert obs_routes._raw_span_str(SimpleNamespace(), "caliber.model") is None
    assert (
        obs_routes._raw_span_str(SimpleNamespace(attributes={"other": "x"}), "caliber.model")
        is None
    )
    # Plain (non-JSON-encoded) string value: json.loads fails, falls back to the raw string.
    assert (
        obs_routes._raw_span_str(
            SimpleNamespace(attributes={"caliber.model": "gpt-4"}), "caliber.model"
        )
        == "gpt-4"
    )
    # JSON-encoded string value: decoded to the inner string.
    assert (
        obs_routes._raw_span_str(
            SimpleNamespace(attributes={"caliber.model": '"gpt-4-json"'}), "caliber.model"
        )
        == "gpt-4-json"
    )


def test_list_traces_rolls_up_only_valid_numeric_attributes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration check: a trace mixing valid/invalid span attribute encodings
    still rolls up only the valid ones end-to-end through the route."""
    import mlflow

    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="mixed",
            state=SimpleNamespace(name="OK"),
            request_time=1,
            execution_duration=1,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(
            spans=[
                SimpleNamespace(span_type="TOOL", name="bool", attributes={"caliber.tokens": True}),
                SimpleNamespace(span_type="TOOL", name="num", attributes={"caliber.tokens": 42}),
                SimpleNamespace(
                    span_type="TOOL", name="badjson", attributes={"caliber.tokens": "nope"}
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_k: [trace])

    resp = client.get(f"{OBS}/traces")
    summary = resp.json()["data"]["traces"][0]
    assert summary["total_tokens"] == 42


# ---------------------------------------------------------------------------
# Malformed-trace skipping (list_traces / get_metrics)
# ---------------------------------------------------------------------------


def test_list_traces_skips_malformed_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow, "search_traces", lambda **_k: [_RaisingInfoTrace(), _trace("good-1")]
    )

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert [t["trace_id"] for t in resp.json()["data"]["traces"]] == ["good-1"]


def test_get_metrics_skips_malformed_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow, "search_traces", lambda **_k: [_RaisingInfoTrace(), _trace("good-1", ts=500)]
    )

    resp = client.get(f"{OBS}/metrics")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["totals"]["count"] == 1


# ---------------------------------------------------------------------------
# post_feedback — mlflow.log_feedback failure
# ---------------------------------------------------------------------------


def test_post_feedback_log_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    def boom(**_kwargs: object) -> object:
        raise RuntimeError("mlflow write failed")

    monkeypatch.setattr(mlflow, "log_feedback", boom)

    resp = client.post(f"{OBS}/traces/tr-1/feedback", json={"value": True})
    assert resp.status_code == 502, resp.text
    assert "failed to log feedback" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# _bucketize — falsy-timestamp rows are counted in totals but skipped from buckets
# ---------------------------------------------------------------------------


def test_metrics_bucketize_skips_falsy_timestamp_from_buckets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    no_ts = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="no-ts",
            state=SimpleNamespace(name="OK"),
            request_time=None,
            execution_duration=5,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(spans=[]),
    )
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow,
        "search_traces",
        lambda **_k: [no_ts, _trace("a", ts=1000), _trace("b", ts=2000)],
    )

    resp = client.get(f"{OBS}/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["totals"]["count"] == 3
    assert sum(b["count"] for b in data["buckets"]) == 2


def test_get_metrics_no_experiments_returns_empty_bucketize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "search_experiments", lambda max_results=100: [])

    resp = client.get(f"{OBS}/metrics")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["buckets"] == []


def test_get_metrics_search_traces_raises_returns_empty_bucketize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )

    def boom(**_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(mlflow, "search_traces", boom)

    resp = client.get(f"{OBS}/metrics")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["buckets"] == []


def test_get_metrics_since_ms_filters_out_old_traces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow, "search_traces", lambda **_k: [_trace("old", ts=10), _trace("new", ts=2000)]
    )

    resp = client.get(f"{OBS}/metrics", params={"since_ms": "500"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["totals"]["count"] == 1


# ---------------------------------------------------------------------------
# gateway_usage_payload — no HTTP route of its own; called directly (feeds the
# Gateway page's Usage tab from another route module).
# ---------------------------------------------------------------------------


def test_gateway_usage_payload_mlflow_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A module set to ``None`` in sys.modules makes ``import mlflow`` raise
    # ImportError — exercises the "MLflow not importable" degrade branch.
    monkeypatch.setitem(sys.modules, "mlflow", None)

    result = obs_routes.gateway_usage_payload(experiment_filter="", configured="", since_ms=None)
    assert result["by_model"] == []
    assert result["buckets"] == []


def test_gateway_usage_payload_no_experiments(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "search_experiments", lambda max_results=100: [])

    result = obs_routes.gateway_usage_payload(experiment_filter="", configured="", since_ms=None)
    assert result["by_model"] == []
    assert result["buckets"] == []


def test_gateway_usage_payload_search_traces_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )

    def boom(**_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(mlflow, "search_traces", boom)

    result = obs_routes.gateway_usage_payload(experiment_filter="", configured="", since_ms=None)
    assert result["by_model"] == []


def test_gateway_usage_payload_skips_malformed_trace_and_rolls_up_by_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mlflow

    good_span = SimpleNamespace(
        span_type="AGENT",
        name="a",
        attributes={"caliber.tokens": "100", "caliber.cost_usd": "0.01", "caliber.model": "gpt-4"},
    )
    skip_span = SimpleNamespace(span_type="TOOL", name="b", attributes={})
    good_trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="g1",
            state=SimpleNamespace(name="OK"),
            request_time=10,
            execution_duration=1,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(spans=[good_span, skip_span]),
    )
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_k: [_RaisingInfoTrace(), good_trace])

    result = obs_routes.gateway_usage_payload(experiment_filter="", configured="", since_ms=None)
    assert result["by_model"] == [{"model": "gpt-4", "calls": 1, "tokens": 100, "cost_usd": 0.01}]


# ---------------------------------------------------------------------------
# _is_missing_object — direct unit tests for the S3-error classification helper
# ---------------------------------------------------------------------------


class _DictRespError(Exception):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__("s3 error")
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        }


class _NoSuchKeyNamedError(Exception):
    """No ``.response`` attribute — exercises the non-dict fallback by class name."""


def test_is_missing_object_edge_cases() -> None:
    assert obs_routes._is_missing_object(_DictRespError("Other", 404)) is True
    assert obs_routes._is_missing_object(_DictRespError("Other", 500)) is False
    assert obs_routes._is_missing_object(_NoSuchKeyNamedError("whatever")) is True
    assert obs_routes._is_missing_object(RuntimeError("plain failure")) is False


class _FakeS3ServerError:
    """An object-store client double whose ``get_object`` raises a non-missing error."""

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        raise _DictRespError("InternalError", 500)


def test_allure_report_s3_fetch_failure_returns_502(client: TestClient) -> None:
    client.app.state.object_store_client = _FakeS3ServerError()
    _set_allure_dir(client, "s3://reports/allure")

    resp = client.get(f"{OBS}/allure-report/")
    assert resp.status_code == 502, resp.text
    assert "failed to read report" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# _serve_allure_local — symlink traversal + directory-index resolution
# ---------------------------------------------------------------------------


def test_allure_report_local_symlink_escape_blocked(client: TestClient, tmp_path: Path) -> None:
    base = tmp_path / "report"
    base.mkdir()
    (base / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOPSECRET", encoding="utf-8")
    (base / "escape").symlink_to(outside)
    _set_allure_dir(client, base)

    resp = client.get(f"{OBS}/allure-report/escape")
    assert resp.status_code == 403, resp.text
    assert "TOPSECRET" not in resp.text


def test_allure_report_local_serves_directory_index(client: TestClient, tmp_path: Path) -> None:
    base = tmp_path / "report2"
    base.mkdir()
    (base / "index.html").write_text("<html>ROOT</html>", encoding="utf-8")
    sub = base / "sub"
    sub.mkdir()
    (sub / "index.html").write_text("<html>SUB INDEX</html>", encoding="utf-8")
    _set_allure_dir(client, base)

    resp = client.get(f"{OBS}/allure-report/sub")
    assert resp.status_code == 200, resp.text
    assert "SUB INDEX" in resp.text
