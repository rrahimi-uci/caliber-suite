"""End-to-end coverage for the API Request workflow node (URL + cURL modes).

Covers manifest parse, validation, the component catalog, compile, the cURL
parser, and the runtime interpreter. The outbound call is injected via
``RuntimePlan.webhook_sender`` so no network is hit.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber.egress import EgressPolicy
from caliber.workflows import component_catalog as cc
from caliber.workflows.compiler import build_ir, compile_workflow
from caliber.workflows.manifest import ApiRequestNode, parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    _parse_curl,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from caliber.workflows.validation import validate_manifest

_BASE_IN = {"payload": {"type": "structured"}, "input": {"type": "string"}}
_BASE_OUT = {
    "text": {"type": "string"},
    "response": {"type": "structured"},
    "metadata": {"type": "structured"},
}


#: The egress pre-check now fails closed on a host this process cannot resolve, because a
#: policy-time DNS failure followed by a successful connect-time lookup would reach an
#: address nothing vetted. These tests use RFC 2606 reserved names (``*.example``), which
#: never resolve, and inject a fake sender — so no request leaves the process and DNS
#: vetting is not the property under test. Opting in locally keeps the fail-closed default
#: in force everywhere else; it is pinned by tests/test_egress_policy.py and
#: tests/test_egress_rebinding.py.
_ALLOW_UNRESOLVABLE = EgressPolicy(allow_unresolvable_hosts=True)


def _manifest(hook: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": "wf_api",
        "name": "API demo",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "none"},
        },
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "hook": hook,
            "out": {
                "id": "out",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {"id": "e1", "from": "start", "to": "hook", "map": {"user_message": "input"}},
            {"id": "e2", "from": "hook", "to": "out", "map": {"text": "response"}},
        ],
        "tools": {},
    }


def _url_node(**over: Any) -> dict[str, Any]:
    return {
        "id": "hook",
        "type": "api_request",
        "mode": "url",
        "url": "https://api.test/v1/resource",
        "method": "POST",
        "headers": {"Authorization": "Bearer T"},
        "body": "",
        "inputs": _BASE_IN,
        "outputs": _BASE_OUT,
        **over,
    }


def _curl_node(curl: str) -> dict[str, Any]:
    return {
        "id": "hook",
        "type": "api_request",
        "mode": "curl",
        "curl": curl,
        "inputs": _BASE_IN,
        "outputs": _BASE_OUT,
    }


# --- cURL parser -------------------------------------------------------------


def test_parse_curl_extracts_method_url_headers_body() -> None:
    parsed = _parse_curl(
        "curl -X PUT 'https://api.test/x' -H 'Content-Type: application/json' "
        "-H 'X-Key: abc' -d '{\"a\": 1}'"
    )
    assert parsed["method"] == "PUT"
    assert parsed["url"] == "https://api.test/x"
    assert parsed["headers"] == {"Content-Type": "application/json", "X-Key": "abc"}
    assert parsed["body"] == '{"a": 1}'


def test_parse_curl_defaults_post_when_body_present() -> None:
    parsed = _parse_curl("curl https://api.test/y -d 'hello=world'")
    assert parsed["method"] == "POST"
    assert parsed["url"] == "https://api.test/y"


def test_parse_curl_defaults_get_without_body() -> None:
    parsed = _parse_curl("curl https://api.test/z")
    assert parsed["method"] == "GET"
    assert parsed["body"] is None


def test_parse_curl_without_url_raises() -> None:
    from caliber.workflows.runtime import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        _parse_curl("curl -X POST -H 'A: b'")


# --- node lifecycle ----------------------------------------------------------


def test_api_request_parses_typed_fields() -> None:
    node = parse_manifest(_manifest(_url_node())).nodes["hook"]
    assert isinstance(node, ApiRequestNode)
    assert node.mode == "url"
    assert node.method == "POST"
    assert node.headers == {"Authorization": "Bearer T"}


def test_api_request_validates_clean() -> None:
    assert validate_manifest(parse_manifest(_manifest(_url_node()))).errors == []
    curl = _curl_node("curl -X GET https://api.test/c")
    assert validate_manifest(parse_manifest(_manifest(curl))).errors == []


def test_api_request_in_component_catalog() -> None:
    component = next(
        c for c in cc.build_workflow_component_catalog()["components"] if c["type"] == "api_request"
    )
    assert component["label"] == "API Request"
    assert component["category"] == "Integrations"
    field_keys = {f["key"] for f in component["fields"]}
    assert {"mode", "url", "method", "curl", "headers", "body", "timeout_seconds"} <= field_keys
    assert component["starter_node"]["type"] == "api_request"
    # every field is self-describing (the catalog contract)
    assert all(f["description"] for f in component["fields"])


def test_api_request_compiles() -> None:
    result = compile_workflow(
        parse_manifest(_manifest(_url_node())), resolver=InMemoryToolResolver({}), version="1"
    )
    assert "api_request" in result.generated_python.lower()
    assert result.report["validation"]["errors"] == []


def _run(hook: dict[str, Any], sender: Any, run_input: str = "hello") -> Any:
    manifest = parse_manifest(_manifest(hook))
    resolver = InMemoryToolResolver({})
    plan = RuntimePlan(
        egress_policy=_ALLOW_UNRESOLVABLE,
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        webhook_sender=sender,
    )
    return execute(plan, run_input, executor=FakeWorkflowExecutor())


def test_api_request_url_mode_runs_and_sends_upstream_body() -> None:
    captured: dict[str, Any] = {}

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {"status_code": 201, "text": "created", "json": None, "headers": {}}

    result = _run(_url_node(), sender)
    assert result.status == "completed"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/v1/resource"
    assert captured["headers"] == {"Authorization": "Bearer T"}
    assert captured["body"] == "hello"  # body empty -> upstream input
    hook = next(s for s in result.steps if s.node_id == "hook")
    assert hook.status == "ok" and "201" in (hook.detail or "")


def test_api_request_url_mode_uses_explicit_json_body() -> None:
    captured: dict[str, Any] = {}

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {"status_code": 200, "text": "ok", "json": None, "headers": {}}

    _run(_url_node(body='{"hello": "world"}'), sender)
    assert captured["body"] == {"hello": "world"}  # JSON string coerced to object


def test_api_request_curl_mode_runs() -> None:
    captured: dict[str, Any] = {}

    def sender(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {"status_code": 200, "text": "ok", "json": None, "headers": {}}

    curl = _curl_node("curl -X PATCH 'https://api.test/curl' -H 'X-A: 1' -d '{\"k\": 2}'")
    result = _run(curl, sender)
    assert result.status == "completed"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://api.test/curl"
    assert captured["headers"] == {"X-A": "1"}
    assert captured["body"] == {"k": 2}


def test_api_request_error_surfaces_in_result() -> None:
    def boom(_request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("dns failure")

    result = _run(_url_node(), boom)
    assert result.status == "error"
    assert "api_request node 'hook'" in (result.error or "")


def test_api_request_url_mode_empty_url_fails() -> None:
    result = _run(_url_node(url=""), lambda _r: {"status_code": 200, "text": "", "json": None})
    assert result.status == "error"
    assert "url" in (result.error or "").lower()


def test_api_request_is_a_valid_orchestration_target() -> None:
    raw = _manifest(_url_node())
    raw["nodes"]["each"] = {
        "id": "each",
        "type": "for_each",
        "target_node_id": "hook",
        "item_input_port": "items",
        "max_items": 5,
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"results": {"type": "structured"}},
    }
    report = validate_manifest(parse_manifest(raw))
    assert [e for e in report.errors if "executable" in e.message.lower()] == []
