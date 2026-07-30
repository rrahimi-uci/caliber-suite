"""End-to-end coverage for the Webhook workflow node.

Exercises every layer the node touches: manifest parse, validation, the
component catalog, the compiler (manifest -> IR -> generated Python), and the
runtime interpreter. The outbound HTTP call is injected via
``RuntimePlan.webhook_sender`` so no network is hit.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber.egress import EgressPolicy
from caliber.workflows import component_catalog as cc
from caliber.workflows.compiler import build_ir, compile_workflow
from caliber.workflows.manifest import WebhookNode, parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from caliber.workflows.validation import validate_manifest

#: The egress pre-check now fails closed on a host this process cannot resolve, because a
#: policy-time DNS failure followed by a successful connect-time lookup would reach an
#: address nothing vetted. These tests use RFC 2606 reserved names (``*.example``), which
#: never resolve, and inject a fake sender — so no request leaves the process and DNS
#: vetting is not the property under test. Opting in locally keeps the fail-closed default
#: in force everywhere else; it is pinned by tests/test_egress_policy.py and
#: tests/test_egress_rebinding.py.
_ALLOW_UNRESOLVABLE = EgressPolicy(allow_unresolvable_hosts=True)


def _manifest(url: str = "https://example.test/notify") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": "wf_webhook",
        "name": "Webhook demo",
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
            "hook": {
                "id": "hook",
                "type": "webhook",
                "url": url,
                "method": "POST",
                "headers": {"X-Source": "caliber"},
                "timeout_seconds": 12,
                "inputs": {
                    "payload": {"type": "structured"},
                    "input": {"type": "string"},
                },
                "outputs": {
                    "text": {"type": "string"},
                    "response": {"type": "structured"},
                    "metadata": {"type": "structured"},
                },
            },
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


def test_webhook_parses_typed_fields() -> None:
    manifest = parse_manifest(_manifest())
    node = manifest.nodes["hook"]
    assert isinstance(node, WebhookNode)
    assert node.url == "https://example.test/notify"
    assert node.method == "POST"
    assert node.headers == {"X-Source": "caliber"}
    assert node.timeout_seconds == 12


def test_webhook_validates_clean() -> None:
    report = validate_manifest(parse_manifest(_manifest()))
    assert report.errors == []


def test_webhook_in_component_catalog() -> None:
    catalog = cc.build_workflow_component_catalog()
    webhook = next(c for c in catalog["components"] if c["type"] == "webhook")
    assert webhook["label"] == "Webhook"
    assert webhook["category"] == "Integrations"
    field_keys = {field["key"] for field in webhook["fields"]}
    assert {"url", "method", "headers", "timeout_seconds"} <= field_keys
    # setup check nudges the operator to set a URL before compile
    assert any(check["field"] == "url" for check in webhook["setup_checks"])
    # starter node payload is materializable in the designer
    assert webhook["starter_node"]["type"] == "webhook"


def test_webhook_compiles_to_python() -> None:
    manifest = parse_manifest(_manifest())
    result = compile_workflow(manifest, resolver=InMemoryToolResolver({}), version="1")
    assert "webhook" in result.generated_python.lower()
    assert result.report["validation"]["errors"] == []


def test_webhook_runs_with_injected_sender() -> None:
    manifest = parse_manifest(_manifest())
    resolver = InMemoryToolResolver({})
    captured: dict[str, Any] = {}

    def fake_sender(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {
            "status_code": 202,
            "text": '{"ok": true}',
            "json": {"ok": True},
            "headers": {"content-type": "application/json"},
        }

    plan = RuntimePlan(
        egress_policy=_ALLOW_UNRESOLVABLE,
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        webhook_sender=fake_sender,
    )
    result = execute(plan, "hello world", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    # the sender received the configured request + the mapped upstream body
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.test/notify"
    assert captured["headers"] == {"X-Source": "caliber"}
    assert captured["body"] == "hello world"

    hook_step = next(step for step in result.steps if step.node_id == "hook")
    assert hook_step.status == "ok"
    assert hook_step.output == '{"ok": true}'
    assert "202" in (hook_step.detail or "")


def test_webhook_runtime_error_surfaces_in_result() -> None:
    manifest = parse_manifest(_manifest())
    resolver = InMemoryToolResolver({})

    def boom_sender(_request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    plan = RuntimePlan(
        egress_policy=_ALLOW_UNRESOLVABLE,
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        webhook_sender=boom_sender,
    )
    result = execute(plan, "hi", executor=FakeWorkflowExecutor())
    assert result.status == "error"
    assert "webhook node 'hook'" in (result.error or "")
    assert "connection refused" in (result.error or "")


def test_webhook_empty_url_fails_the_run() -> None:
    manifest = parse_manifest(_manifest(url=""))
    resolver = InMemoryToolResolver({})
    plan = RuntimePlan(
        egress_policy=_ALLOW_UNRESOLVABLE,
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        webhook_sender=lambda _r: {"status_code": 200, "text": "", "json": None},
    )
    result = execute(plan, "hi", executor=FakeWorkflowExecutor())
    assert result.status == "error"
    assert "url" in (result.error or "").lower()


def test_webhook_node_is_a_valid_orchestration_target() -> None:
    """A for_each can iterate items through a webhook node (executable target)."""
    raw = _manifest()
    raw["nodes"]["each"] = {
        "id": "each",
        "type": "for_each",
        "target_node_id": "hook",
        "item_input_port": "items",
        "max_items": 10,
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"results": {"type": "structured"}},
    }
    report = validate_manifest(parse_manifest(raw))
    target_errors = [e for e in report.errors if "executable" in e.message.lower()]
    assert target_errors == []


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_webhook_accepts_all_methods(method: str) -> None:
    raw = _manifest()
    raw["nodes"]["hook"]["method"] = method
    node = parse_manifest(raw).nodes["hook"]
    assert isinstance(node, WebhookNode)
    assert node.method == method
