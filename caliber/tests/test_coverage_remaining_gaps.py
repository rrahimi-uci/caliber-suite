"""Tests targeting uncovered lines across many small-gap modules.

Covers:
- events_stream.py (87%): SSE format, _anext, _format_event
- diff.py (91%): _deploy_gate_changes
- circuit_breaker.py (94%): half-open probe rejection
- manifest.py (94%): node key mismatch, duplicate edges, canonical_json, hash
- refinement.py (94%): _first_agent_id
- bundle.py (94%): _coerce_int
- secrets.py (95%): file-not-found, OS error, empty file
- trace.py (94%): _bind_for_test
- mlflow_client.py (93%): experiment name resolution, severity mapping
- promoter.py (96%): rollback errors
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from tests.workflow_helpers import make_support_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"


# ===================================================================
# events_stream.py — SSE helpers (lines 47-48, 76-77, 115-117)
# ===================================================================


def test_format_event_with_type() -> None:
    from caliber.routes.events_stream import _format_event

    payload = {"type": "job.updated", "id": "123"}
    raw = _format_event(payload)
    text = raw.decode("utf-8")
    assert "event: job.updated" in text
    assert "data:" in text
    assert text.endswith("\n\n")


def test_format_event_without_type() -> None:
    from caliber.routes.events_stream import _format_event

    payload = {"value": 42}
    raw = _format_event(payload)
    text = raw.decode("utf-8")
    assert "event:" not in text
    assert "data:" in text


def test_anext_delegates() -> None:
    from caliber.routes.events_stream import _anext

    async def _run() -> dict:
        async def gen():
            yield {"type": "hello"}

        it = gen()
        return await _anext(it)

    result = asyncio.run(_run())
    assert result == {"type": "hello"}


def test_sse_route_is_registered(client: TestClient) -> None:
    """The SSE route is registered in the application."""
    from starlette.routing import Route

    app = client.app
    paths = [r.path for r in app.routes if isinstance(r, Route)]
    assert "/ajax-api/2.0/mlflow/caliber/events/stream" in paths


# ===================================================================
# diff.py — _deploy_gate_changes (lines 119-122)
# ===================================================================


def test_deploy_gate_changes_added_and_removed() -> None:
    from caliber.workflows.diff import compute_graph_diff
    from caliber.workflows.manifest import WorkflowManifest

    base_dict = make_support_manifest("difftest")
    base_dict["deploy_gates"] = {
        "old_gate": {
            "type": "deploy_gate",
            "dataset_ref": "support_eval",
            "required_for_aliases": ["prod"],
            "thresholds": {"min_pass_rate": 0.9},
        }
    }
    cand_dict = make_support_manifest("difftest")
    cand_dict["deploy_gates"] = {
        "new_gate": {
            "type": "deploy_gate",
            "dataset_ref": "support_eval",
            "required_for_aliases": ["prod"],
            "thresholds": {"min_pass_rate": 0.95},
        }
    }
    base = WorkflowManifest.model_validate(base_dict)
    cand = WorkflowManifest.model_validate(cand_dict)
    diff = compute_graph_diff(base, cand)
    assert len(diff["deploy_gate_changes"]) == 2
    names = {c["name"] for c in diff["deploy_gate_changes"]}
    assert "old_gate" in names
    assert "new_gate" in names


# ===================================================================
# circuit_breaker.py — half-open probe-in-flight rejection (lines 243-251)
# ===================================================================


def test_circuit_breaker_half_open_rejects_concurrent_probe() -> None:
    from caliber.llm.circuit_breaker import (
        CircuitBreakerLLMProvider,
        CircuitState,
        LLMCircuitOpenError,
    )
    from caliber.llm.provider import CandidateContext, Diagnosis, LLMProvider

    ctx = CandidateContext(
        agent_id="a",
        job_id="j",
        artifact_type="prompt",
        optimizer_type="default",
        diagnosis=Diagnosis(
            summary="s",
            evidence=[],
            focus_area="f",
            root_cause="r",
            confidence=0.9,
        ),
    )

    fake = MagicMock(spec=LLMProvider)
    fake.generate_candidate.side_effect = RuntimeError("fail")

    cb = CircuitBreakerLLMProvider(
        inner=fake,
        failure_threshold=1,
        window_seconds=60.0,
        open_duration_seconds=0.01,
    )
    # Trip the breaker
    with pytest.raises(RuntimeError):
        cb.generate_candidate(ctx)
    # Manually set to half-open with probe in flight
    with cb._lock:
        cb._state = CircuitState.HALF_OPEN
        cb._probe_in_flight = True
    with pytest.raises(LLMCircuitOpenError, match="probe in flight"):
        cb.generate_candidate(ctx)


def test_circuit_breaker_half_open_no_probe_becomes_probe() -> None:
    """HALF_OPEN with no probe_in_flight treats the call as the probe."""
    from caliber.llm.circuit_breaker import (
        CircuitBreakerLLMProvider,
        CircuitState,
    )
    from caliber.llm.provider import (
        CandidateContext,
        Diagnosis,
        LLMProvider,
        LLMUsage,
        PromptCandidate,
    )

    ctx = CandidateContext(
        agent_id="a",
        job_id="j",
        artifact_type="prompt",
        optimizer_type="default",
        diagnosis=Diagnosis(
            summary="s",
            evidence=[],
            focus_area="f",
            root_cause="r",
            confidence=0.9,
        ),
    )

    fake = MagicMock(spec=LLMProvider)
    fake.generate_candidate.return_value = (
        PromptCandidate(content="ok", rationale="r", artifact_type="prompt"),
        LLMUsage(input_tokens=1, output_tokens=1),
    )

    cb = CircuitBreakerLLMProvider(
        inner=fake,
        failure_threshold=1,
        window_seconds=60.0,
        open_duration_seconds=0.01,
    )
    with cb._lock:
        cb._state = CircuitState.HALF_OPEN
        cb._probe_in_flight = False
    result = cb.generate_candidate(ctx)
    assert result[0].content == "ok"
    assert cb._state == CircuitState.CLOSED


# ===================================================================
# manifest.py — structural validators (lines 437, 439, 465, 495, 499-500, 516)
# ===================================================================


def test_manifest_node_key_mismatch() -> None:
    from caliber.workflows.manifest import WorkflowManifest

    d = make_support_manifest("keymismatch")
    # Mismatch: key is "start" but node.id is "wrong"
    d["nodes"]["start"]["id"] = "wrong"
    with pytest.raises(Exception, match="does not match"):
        WorkflowManifest.model_validate(d)


def test_manifest_duplicate_edge_ids() -> None:
    from caliber.workflows.manifest import WorkflowManifest

    d = make_support_manifest("dupedge")
    # Force two edges to have the same id
    if len(d["edges"]) >= 2:
        d["edges"][1]["id"] = d["edges"][0]["id"]
    with pytest.raises(Exception, match="duplicate edge"):
        WorkflowManifest.model_validate(d)


def test_manifest_canonical_json_and_hash() -> None:
    from caliber.workflows.manifest import WorkflowManifest

    d = make_support_manifest("canontest")
    m = WorkflowManifest.model_validate(d)
    cj = m.canonical_json()
    parsed = json.loads(cj)
    assert parsed["workflow_id"] == "canontest"
    h = m.manifest_hash()
    assert isinstance(h, str) and len(h) == 64


# ===================================================================
# refinement.py — _first_agent_id (lines 265-270)
# ===================================================================


def test_first_agent_id_returns_agent_node() -> None:
    from caliber.workflows.manifest import WorkflowManifest
    from caliber.workflows.refinement import _first_agent_id

    d = make_support_manifest("agentid")
    m = WorkflowManifest.model_validate(d)
    result = _first_agent_id(m)
    # should find the "support_agent" node which is type=agent
    assert result == "support_agent"


# ===================================================================
# bundle.py — _coerce_int (lines 280-282)
# ===================================================================


def test_coerce_int_from_string() -> None:
    from caliber.bundle import _coerce_int

    assert _coerce_int("42") == 42
    assert _coerce_int(7) == 7
    assert _coerce_int(None) is None
    assert _coerce_int("abc") is None


# ===================================================================
# secrets.py — _resolve_file edge cases (lines 138-143)
# ===================================================================


def test_secret_resolve_file_not_found(tmp_path: Path) -> None:
    from caliber.secrets import _resolve_file

    result = _resolve_file(str(tmp_path / "nope.txt"), source_label="test")
    assert result is None


def test_secret_resolve_file_empty(tmp_path: Path) -> None:
    from caliber.secrets import _resolve_file

    f = tmp_path / "empty.txt"
    f.write_text("   \n  ")
    result = _resolve_file(str(f), source_label="test")
    assert result is None


def test_secret_resolve_file_os_error(tmp_path: Path) -> None:
    from caliber.secrets import _resolve_file

    # A directory where a file is expected → IsADirectoryError (OSError subclass)
    d = tmp_path / "is_dir"
    d.mkdir()
    result = _resolve_file(str(d), source_label="test")
    assert result is None


def test_secret_resolve_file_empty_path() -> None:
    from caliber.secrets import _resolve_file

    result = _resolve_file("", source_label="test")
    assert result is None


# ===================================================================
# observability/trace.py — _bind_for_test (lines 151-155)
# ===================================================================


def test_bind_for_test_sets_and_resets_trace_id() -> None:
    from caliber.observability.trace import _bind_for_test, trace_id_var

    async def _run() -> None:
        assert trace_id_var.get("") == ""
        async with _bind_for_test("test-trace-123"):
            assert trace_id_var.get("") == "test-trace-123"
        assert trace_id_var.get("") == ""

    asyncio.run(_run())


# ===================================================================
# mlflow_client.py — experiment name resolution + severity
# ===================================================================


def test_resolve_experiment_ids_by_name() -> None:
    from caliber.mlflow_client import _resolve_search_experiment_ids

    class FakeExperiment:
        experiment_id = "42"

    mock_mlflow = MagicMock()
    mock_mlflow.get_experiment_by_name.return_value = FakeExperiment()
    ids, aliases = _resolve_search_experiment_ids(mock_mlflow, ["my-experiment"])
    assert ids == ["42"]
    assert aliases["42"] == "my-experiment"


def test_resolve_experiment_ids_name_not_found() -> None:
    from caliber.mlflow_client import _resolve_search_experiment_ids

    mock_mlflow = MagicMock()
    mock_mlflow.get_experiment_by_name.return_value = None
    ids, aliases = _resolve_search_experiment_ids(mock_mlflow, ["missing-exp"])
    assert ids == []


def test_resolve_experiment_ids_no_id_attr() -> None:
    from caliber.mlflow_client import _resolve_search_experiment_ids

    class BadExperiment:
        experiment_id = ""

    mock_mlflow = MagicMock()
    mock_mlflow.get_experiment_by_name.return_value = BadExperiment()
    ids, aliases = _resolve_search_experiment_ids(mock_mlflow, ["empty-id-exp"])
    assert ids == []


def test_severity_from_feedback_negative() -> None:
    from caliber.mlflow_client import _severity_from_feedback

    assert _severity_from_feedback(False) == "critical"
    assert _severity_from_feedback(0) == "critical"
    assert _severity_from_feedback("down") == "critical"
    assert _severity_from_feedback("negative") == "critical"
    assert _severity_from_feedback("bad") == "critical"


def test_severity_from_feedback_other() -> None:
    from caliber.mlflow_client import _severity_from_feedback

    assert _severity_from_feedback("something") == "standard"
    assert _severity_from_feedback(42) == "standard"


def test_experiment_id_from_trace_info_fallback() -> None:
    from caliber.mlflow_client import _experiment_id_from_trace

    class FakeInfo:
        experiment_id = "99"

    class FakeTrace:
        experiment_id = None
        info = FakeInfo()

    result = _experiment_id_from_trace(FakeTrace())
    assert result == "99"


def test_experiment_id_from_trace_direct() -> None:
    from caliber.mlflow_client import _experiment_id_from_trace

    class FakeTrace:
        experiment_id = "55"

    result = _experiment_id_from_trace(FakeTrace())
    assert result == "55"


# ===================================================================
# promoter.py — rollback errors (lines 221-222, 443-445)
# ===================================================================


def test_mlflow_promoter_rollback_wrong_artifact_type() -> None:
    from caliber.promoter import MLflowPromoter, PromoterError, RollbackRequest

    p = MLflowPromoter()
    with pytest.raises(PromoterError, match="only supports.*prompt"):
        p.rollback(
            RollbackRequest(
                agent_id="a",
                artifact_type="skill",
                version_before=1,
                checkpoint_id="cp1",
            )
        )


def test_mlflow_promoter_rollback_no_prior_version() -> None:
    from caliber.promoter import MLflowPromoter, PromoterError, RollbackRequest

    p = MLflowPromoter()
    with pytest.raises(PromoterError, match="no prior version"):
        p.rollback(
            RollbackRequest(
                agent_id="a",
                artifact_type="prompt",
                version_before=None,
                checkpoint_id="cp1",
            )
        )


# ===================================================================
# rate_limit.py — _read_user_header edge case
# ===================================================================


def test_rate_limit_read_user_header_non_list() -> None:
    from caliber.rate_limit import _read_user_header

    scope: dict = {"headers": "not-a-list"}
    result = _read_user_header(scope)
    assert result == "anonymous"
