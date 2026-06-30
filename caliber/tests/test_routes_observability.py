"""Observability routes — trace list + span-tree fetch.

The list wraps ``mlflow.search_traces`` and the detail wraps
``mlflow.get_trace`` (via ``fetch_trace_spans``); both are guarded so they
degrade to an empty list/tree when MLflow has no traces. The mapping test
monkeypatches the MLflow search surface with a fake trace to assert the summary
shape without a live tracking server.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient


class _NoSuchKeyError(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    """Minimal object-store client double for the S3-served Allure report."""

    def __init__(self, files: dict[str, tuple[bytes, str]]) -> None:
        self._files = files

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if Key not in self._files:
            raise _NoSuchKeyError()
        body, content_type = self._files[Key]
        return {"Body": io.BytesIO(body), "ContentType": content_type}


PREFIX = "/ajax-api/2.0/mlflow/caliber"
OBS = PREFIX + "/observability"


def _trace(
    trace_id: str,
    *,
    session: str | None = None,
    user: str | None = None,
    experiment_id: str = "0",
    tokens_meta: int | None = None,
    ts: int = 100,
    state: str = "OK",
) -> SimpleNamespace:
    """A fake MLflow trace with the metadata/location the routes read."""
    metadata: dict[str, str] = {}
    if session:
        metadata["mlflow.trace.session"] = session
    if user:
        metadata["mlflow.trace.user"] = user
    if tokens_meta is not None:
        metadata["mlflow.trace.tokenUsage"] = json.dumps({"total_tokens": tokens_meta})
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            state=SimpleNamespace(name=state),
            request_time=ts,
            execution_duration=1,
            request_preview="",
            response_preview="",
            tags={},
            trace_metadata=metadata,
            trace_location=SimpleNamespace(
                mlflow_experiment=SimpleNamespace(experiment_id=experiment_id)
            ),
        ),
        data=SimpleNamespace(spans=[]),
    )


def test_list_traces_returns_a_list(client: TestClient) -> None:
    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["data"]["traces"], list)


def test_get_trace_is_empty_for_unknown_id(client: TestClient) -> None:
    resp = client.get(f"{OBS}/traces/does-not-exist")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["spans"] == []


def test_list_traces_maps_summary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    fake_trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-1",
            state=SimpleNamespace(name="OK"),
            request_time=1_717_000_000_000,
            execution_duration=1234,
            request_preview="hello?",
            response_preview="hi!",
            tags={"mlflow.traceName": "agent.greeter"},
        ),
        data=SimpleNamespace(
            spans=[
                SimpleNamespace(span_type="AGENT", name="greeter"),
                SimpleNamespace(span_type="TOOL", name="lookup"),
            ]
        ),
    )
    monkeypatch.setattr(
        mlflow,
        "search_experiments",
        lambda max_results=100: [SimpleNamespace(experiment_id="0")],
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_kwargs: [fake_trace])

    resp = client.get(f"{OBS}/traces")
    assert resp.status_code == 200, resp.text
    traces = resp.json()["data"]["traces"]
    assert len(traces) == 1
    summary = traces[0]
    assert summary["trace_id"] == "tr-1"
    assert summary["name"] == "agent.greeter"
    assert summary["status"] == "OK"
    assert summary["span_count"] == 2
    assert summary["tool_call_count"] == 1
    assert summary["execution_time_ms"] == 1234
    assert summary["timestamp_ms"] == 1_717_000_000_000


def test_list_traces_status_filter(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    def _trace(trace_id: str, state: str) -> SimpleNamespace:
        return SimpleNamespace(
            info=SimpleNamespace(
                trace_id=trace_id,
                state=SimpleNamespace(name=state),
                request_time=1,
                execution_duration=1,
                request_preview="",
                response_preview="",
                tags={},
            ),
            data=SimpleNamespace(spans=[]),
        )

    monkeypatch.setattr(
        mlflow,
        "search_experiments",
        lambda max_results=100: [SimpleNamespace(experiment_id="0")],
    )
    monkeypatch.setattr(
        mlflow,
        "search_traces",
        lambda **_kwargs: [_trace("ok-1", "OK"), _trace("err-1", "ERROR")],
    )

    resp = client.get(f"{OBS}/traces", params={"status": "error"})
    assert resp.status_code == 200, resp.text
    traces = resp.json()["data"]["traces"]
    assert [t["trace_id"] for t in traces] == ["err-1"]


def test_list_traces_rolls_up_tokens_and_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    # MLflow stores span attributes JSON-encoded; CALIBER writes caliber.tokens
    # / caliber.cost_usd per span — the summary sums them across the trace.
    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-usage",
            state=SimpleNamespace(name="OK"),
            request_time=10,
            execution_duration=20,
            request_preview="",
            response_preview="",
            tags={},
        ),
        data=SimpleNamespace(
            spans=[
                SimpleNamespace(
                    span_type="AGENT",
                    name="a",
                    attributes={"caliber.tokens": "100", "caliber.cost_usd": "0.0025"},
                ),
                SimpleNamespace(
                    span_type="TOOL",
                    name="b",
                    attributes={"caliber.tokens": "40", "caliber.cost_usd": "0.001"},
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_kwargs: [trace])

    resp = client.get(f"{OBS}/traces")
    summary = resp.json()["data"]["traces"][0]
    assert summary["total_tokens"] == 140
    assert summary["cost_usd"] == 0.0035


def test_get_trace_detail_full(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    span = SimpleNamespace(
        span_id="s1",
        parent_id=None,
        name="agent.run",
        span_type="AGENT",
        start_time_ns=0,
        end_time_ns=2_000_000,
        status=SimpleNamespace(status_code="OK"),
        inputs={"q": "hi"},
        outputs="done",
        attributes={
            "caliber.tokens": "120",
            "caliber.prompt_tokens": "80",
            "caliber.completion_tokens": "40",
            "caliber.cost_usd": "0.003",
        },
    )
    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-detail",
            state=SimpleNamespace(name="OK"),
            request_time=1700,
            execution_duration=42,
            request_preview="rp",
            response_preview="resp",
            tags={"mlflow.traceName": "agent.run", "env": "dev"},
            assessments=[
                SimpleNamespace(
                    name="relevance",
                    value=0.9,
                    rationale="looks good",
                    source=SimpleNamespace(source_id="judge"),
                )
            ],
        ),
        data=SimpleNamespace(spans=[span], request="REQ", response="RESP"),
    )
    monkeypatch.setattr(mlflow, "get_trace", lambda _tid, silent=False: trace)

    resp = client.get(f"{OBS}/traces/tr-detail")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "OK"
    assert data["request_time_ms"] == 1700
    assert data["execution_time_ms"] == 42
    assert data["total_tokens"] == 120
    assert data["prompt_tokens"] == 80
    assert data["completion_tokens"] == 40
    assert data["cost_usd"] == 0.003
    assert data["tags"]["env"] == "dev"
    assert data["request"] == "REQ"
    assert data["response"] == "RESP"
    assert len(data["spans"]) == 1
    assert data["spans"][0]["span_type"] == "AGENT"
    assert data["assessments"][0]["name"] == "relevance"
    assert data["assessments"][0]["value"] == 0.9
    assert data["assessments"][0]["source"] == "judge"


def test_summary_session_user_experiment_and_metadata_tokens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    trace = _trace("tr-x", session="sess-1", user="@me", experiment_id="1", tokens_meta=512)
    monkeypatch.setattr(
        mlflow,
        "search_experiments",
        lambda max_results=100: [SimpleNamespace(experiment_id="1", name="agents")],
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_kwargs: [trace])

    summary = client.get(f"{OBS}/traces").json()["data"]["traces"][0]
    assert summary["session_id"] == "sess-1"
    assert summary["user"] == "@me"
    assert summary["experiment_id"] == "1"
    assert summary["experiment_name"] == "agents"
    # No CALIBER span attributes → falls back to MLflow-native token metadata.
    assert summary["total_tokens"] == 512


def test_list_filters_by_session(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow,
        "search_traces",
        lambda **_kwargs: [_trace("a", session="s1"), _trace("b", session="s2")],
    )
    resp = client.get(f"{OBS}/traces", params={"session": "s2"})
    assert [t["trace_id"] for t in resp.json()["data"]["traces"]] == ["b"]


def test_list_filters_by_since_ms(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(
        mlflow,
        "search_traces",
        lambda **_kwargs: [_trace("recent", ts=1000), _trace("old", ts=10)],
    )
    resp = client.get(f"{OBS}/traces", params={"since_ms": "500"})
    assert [t["trace_id"] for t in resp.json()["data"]["traces"]] == ["recent"]


def test_list_scopes_to_requested_experiment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    captured: dict[str, object] = {}

    def fake_search(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return [_trace("a", experiment_id="7")]

    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="7")]
    )
    monkeypatch.setattr(mlflow, "search_traces", fake_search)

    resp = client.get(f"{OBS}/traces", params={"experiment_id": "7"})
    assert resp.status_code == 200, resp.text
    assert captured["experiment_ids"] == ["7"]


def test_list_experiments_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(
        mlflow,
        "search_experiments",
        lambda max_results=100: [
            SimpleNamespace(experiment_id="0", name="Default"),
            SimpleNamespace(experiment_id="1", name="agents"),
        ],
    )
    resp = client.get(f"{OBS}/experiments")
    assert resp.status_code == 200, resp.text
    experiments = resp.json()["data"]["experiments"]
    assert {e["experiment_id"] for e in experiments} == {"0", "1"}
    assert {e["name"] for e in experiments} == {"Default", "agents"}


def test_post_feedback(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    captured: dict[str, object] = {}

    def fake_log_feedback(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(mlflow, "log_feedback", fake_log_feedback)

    resp = client.post(
        f"{OBS}/traces/tr-1/feedback",
        json={"value": True, "rationale": "great answer"},
    )
    assert resp.status_code == 200, resp.text
    assert "assessments" in resp.json()["data"]
    assert captured["trace_id"] == "tr-1"
    assert captured["name"] == "feedback"
    assert captured["value"] is True
    assert captured["rationale"] == "great answer"
    assert getattr(captured["source"], "source_type", None) == "HUMAN"


def test_post_feedback_requires_value(client: TestClient) -> None:
    resp = client.post(f"{OBS}/traces/tr-1/feedback", json={"rationale": "x"})
    assert resp.status_code == 400


def _set_allure_dir(client: TestClient, path: object) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"allure_report_dir": str(path)}
    )


def test_allure_report_not_generated(client: TestClient, tmp_path: object) -> None:
    _set_allure_dir(client, tmp_path)  # empty dir → no index.html
    resp = client.get(f"{OBS}/allure-report/")
    assert resp.status_code == 200, resp.text
    assert "No Allure report generated yet" in resp.text


def test_allure_report_serves_files(client: TestClient, tmp_path: object) -> None:
    import pathlib

    base = pathlib.Path(str(tmp_path))
    (base / "index.html").write_text("<html>ALLURE INDEX</html>", encoding="utf-8")
    (base / "app.js").write_text("console.log('allure')", encoding="utf-8")
    _set_allure_dir(client, base)

    index = client.get(f"{OBS}/allure-report/")
    assert index.status_code == 200, index.text
    assert "ALLURE INDEX" in index.text

    asset = client.get(f"{OBS}/allure-report/app.js")
    assert asset.status_code == 200, asset.text
    assert "console.log" in asset.text

    missing = client.get(f"{OBS}/allure-report/nope.js")
    assert missing.status_code == 404


def test_allure_report_redirects_without_trailing_slash(
    client: TestClient, tmp_path: object
) -> None:
    _set_allure_dir(client, tmp_path)
    resp = client.get(f"{OBS}/allure-report", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith("/allure-report/")


def test_allure_report_blocks_traversal(client: TestClient, tmp_path: object) -> None:
    import pathlib

    base = pathlib.Path(str(tmp_path)) / "report"
    base.mkdir()
    (base / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (pathlib.Path(str(tmp_path)) / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    _set_allure_dir(client, base)

    resp = client.get(f"{OBS}/allure-report/%2e%2e%2fsecret.txt")
    assert resp.status_code in (403, 404)
    assert "TOPSECRET" not in resp.text


def test_allure_report_serves_from_object_store(client: TestClient) -> None:
    client.app.state.object_store_client = _FakeS3(
        {
            "allure/index.html": (b"<html>S3 INDEX</html>", "text/html"),
            "allure/app.js": (b"console.log('s3')", "application/javascript"),
        }
    )
    _set_allure_dir(client, "s3://reports/allure")

    index = client.get(f"{OBS}/allure-report/")
    assert index.status_code == 200, index.text
    assert "S3 INDEX" in index.text

    asset = client.get(f"{OBS}/allure-report/app.js")
    assert asset.status_code == 200, asset.text
    assert "console.log" in asset.text

    missing = client.get(f"{OBS}/allure-report/nope.js")
    assert missing.status_code == 404


def test_allure_report_s3_not_generated(client: TestClient) -> None:
    client.app.state.object_store_client = _FakeS3({})
    _set_allure_dir(client, "s3://reports/allure")
    resp = client.get(f"{OBS}/allure-report/")
    assert resp.status_code == 200, resp.text
    assert "No Allure report generated yet" in resp.text


def test_allure_report_s3_blocks_traversal(client: TestClient) -> None:
    client.app.state.object_store_client = _FakeS3(
        {"allure/index.html": (b"<html>ok</html>", "text/html")}
    )
    _set_allure_dir(client, "s3://reports/allure")
    resp = client.get(f"{OBS}/allure-report/%2e%2e%2fsecret")
    assert resp.status_code in (403, 404)


def test_metrics_buckets_and_totals(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    traces = [
        _trace("a", ts=1000, state="OK", tokens_meta=100),
        _trace("b", ts=2000, state="ERROR", tokens_meta=50),
        _trace("c", ts=3000, state="OK", tokens_meta=25),
    ]
    monkeypatch.setattr(
        mlflow, "search_experiments", lambda max_results=100: [SimpleNamespace(experiment_id="0")]
    )
    monkeypatch.setattr(mlflow, "search_traces", lambda **_kwargs: traces)

    resp = client.get(f"{OBS}/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["totals"]["count"] == 3
    assert data["totals"]["error_rate"] == round(1 / 3, 4)
    assert data["totals"]["tokens"] == 175
    assert data["totals"]["p50_ms"] == 1  # every fake trace has execution_duration=1
    assert isinstance(data["buckets"], list)
    assert sum(b["count"] for b in data["buckets"]) == 3
