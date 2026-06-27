"""Guardrail ``on_failure: redact`` (gap-analysis R4.4).

Before this slice the runtime treated ``redact`` as a no-op passthrough: a
``pii_detection`` guardrail flagged the match but the unscrubbed text flowed
downstream anyway. These tests pin the fix at two levels:

* **Unit** — :func:`caliber.workflows.guardrails.redact_guardrail` scrubs the
  matched spans of failing, redactable checks and leaves everything else alone.
* **End-to-end** — a Start → Agent → Guardrail(redact) → Output workflow run
  produces output with the PII removed, while the same workflow with
  ``on_failure: block`` halts instead (proving redact ≠ block ≠ passthrough).
"""

from __future__ import annotations

from caliber.workflows.compiler import build_ir
from caliber.workflows.guardrails import (
    REDACTION_PLACEHOLDER,
    GuardrailContext,
    redact_guardrail,
    redactable_check_kinds,
)
from caliber.workflows.ir import IRGuardrail, IRGuardrailCheck, NodeType
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from tests.workflow_helpers import fake_resolver, make_manifest

# ---------------------------------------------------------------------------
# Unit — redact_guardrail
# ---------------------------------------------------------------------------


def _guard(on_failure: str, checks: list[tuple[str, dict[str, object]]]) -> IRGuardrail:
    return IRGuardrail(
        node_id="g",
        node_type=NodeType.GUARDRAIL,
        mode="post_agent",
        checks=[IRGuardrailCheck(kind, params) for kind, params in checks],
        on_failure=on_failure,
    )


def test_redactable_kinds_are_the_content_checks() -> None:
    assert redactable_check_kinds() == frozenset(
        {"pii_detection", "forbid_substring", "toxicity_check"}
    )


def test_redact_scrubs_pii() -> None:
    node = _guard("redact", [("pii_detection", {"entities": ["email", "ssn"]})])
    ctx = GuardrailContext(response_text="reach jane@example.com or ssn 123-45-6789 today")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "jane@example.com" not in scrubbed
    assert "123-45-6789" not in scrubbed
    assert scrubbed.count(REDACTION_PLACEHOLDER) == 2
    assert kinds == ["pii_detection"]


def test_redact_scrubs_forbidden_substring_case_insensitively() -> None:
    node = _guard("redact", [("forbid_substring", {"substring": "internal-only"})])
    ctx = GuardrailContext(response_text="This is INTERNAL-ONLY material.")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "INTERNAL-ONLY" not in scrubbed
    assert REDACTION_PLACEHOLDER in scrubbed
    assert kinds == ["forbid_substring"]


def test_redact_scrubs_toxic_terms() -> None:
    node = _guard("redact", [("toxicity_check", {})])
    ctx = GuardrailContext(response_text="you are an idiot")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "idiot" not in scrubbed
    assert kinds == ["toxicity_check"]


def test_redact_leaves_passing_text_unchanged() -> None:
    node = _guard("redact", [("pii_detection", {"entities": ["email"]})])
    ctx = GuardrailContext(response_text="no personal data here")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert scrubbed == "no personal data here"
    assert kinds == []


def test_redact_ignores_non_redactable_failures() -> None:
    # max_length fails but has no span to scrub → text untouched, nothing redacted.
    node = _guard("redact", [("max_length", {"max_chars": 5})])
    ctx = GuardrailContext(response_text="way too long for the limit")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert scrubbed == "way too long for the limit"
    assert kinds == []


def test_redact_runs_pii_first_so_forbid_cannot_strand_card_digits() -> None:
    # A forbid_substring clipping the card's prefix is declared BEFORE pii.
    # PII-first ordering must still remove the whole card (no residual digits).
    node = _guard(
        "redact",
        [
            ("forbid_substring", {"substring": "4111 1111"}),
            ("pii_detection", {"entities": ["credit_card"]}),
        ],
    )
    ctx = GuardrailContext(response_text="card 4111 1111 1111 1111 end")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "4111" not in scrubbed
    assert REDACTION_PLACEHOLDER in scrubbed
    assert kinds[0] == "pii_detection"  # applied first regardless of declaration order


def test_redact_runs_pii_first_so_forbid_cannot_strand_email() -> None:
    node = _guard(
        "redact",
        [
            ("forbid_substring", {"substring": "jane@example"}),
            ("pii_detection", {"entities": ["email"]}),
        ],
    )
    ctx = GuardrailContext(response_text="reach jane@example.com now")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "jane@example" not in scrubbed
    assert "@example" not in scrubbed
    assert kinds[0] == "pii_detection"


def test_redact_handles_mixed_redactable_and_structural_failures() -> None:
    node = _guard(
        "redact",
        [
            ("pii_detection", {"entities": ["email"]}),
            ("max_length", {"max_chars": 5}),  # fails, but not redactable
        ],
    )
    ctx = GuardrailContext(response_text="mail a@b.com now")

    scrubbed, kinds = redact_guardrail(node, ctx)

    assert "a@b.com" not in scrubbed
    assert kinds == ["pii_detection"]


# ---------------------------------------------------------------------------
# End-to-end — a workflow run that redacts the agent output
# ---------------------------------------------------------------------------


def _redact_workflow(on_failure: str) -> dict[str, object]:
    """Start → Agent → Guardrail(pii_detection) → Output, with the given on_failure."""
    data = make_manifest()
    data["nodes"]["pii_guard"] = {
        "id": "pii_guard",
        "type": "guardrail",
        "mode": "post_agent",
        "on_failure": on_failure,
        "inputs": {"response": {"type": "string"}},
        "outputs": {"passthrough": {"type": "string"}},
        "checks": [{"pii_detection": {"entities": ["email"]}}],
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "pii_guard", "map": {"final_output": "response"}},
        {"id": "e3", "from": "pii_guard", "to": "final", "map": {"passthrough": "response"}},
    ]
    return data


def _plan(manifest: dict[str, object]) -> RuntimePlan:
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest), resolver, version="1")
    return RuntimePlan(ir=ir, resolver=resolver)


def test_redact_workflow_scrubs_output_end_to_end() -> None:
    result = execute(
        _plan(_redact_workflow("redact")),
        "contact jane@example.com please",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert "jane@example.com" not in result.output
    assert REDACTION_PLACEHOLDER in result.output
    guard_step = next(s for s in result.steps if s.node_id == "pii_guard")
    assert guard_step.status == "ok"
    assert "redacted: pii_detection" in guard_step.detail


def test_block_halts_where_redact_continues() -> None:
    # Same workflow, on_failure=block → the run is blocked, not passed through.
    result = execute(
        _plan(_redact_workflow("block")),
        "contact jane@example.com please",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "blocked"


def test_escalate_fails_closed_does_not_leak() -> None:
    # escalate is not silent passthrough — it blocks (fail-closed) until true
    # escalation routing lands. The PII must not flow to the output.
    result = execute(
        _plan(_redact_workflow("escalate")),
        "contact jane@example.com please",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "blocked"
    assert "jane@example.com" not in (result.output or "")
