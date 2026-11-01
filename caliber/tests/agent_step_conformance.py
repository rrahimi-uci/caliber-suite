"""The per-scenario conformance harness, re-exported for the tests that import it from here (B08.1b, #720; B08.5, #769).

The harness itself is :mod:`caliber.testing.agent_step_conformance` (it ships with the package so that an adapter outside this
repository can run the same checks; see :mod:`caliber.testing.adapter_conformance`). This module keeps the import path
the adapters' own tests use, and holds the one helper that builds the repository's real executors.
"""

from __future__ import annotations

from typing import Any

from caliber.testing.agent_step_conformance import (  # noqa: F401 - re-exported
    FIXED_NOW,
    MANDATORY,
    PLATFORM_CODES,
    PROTECTED_CLASS,
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    AdapterDriver,
    FakeStepClock,
    Scenario,
    StepContext,
    assert_claim_mismatch,
    canary_protected_tool,
    canary_read_tool,
    fixed_clock,
    limits_changes,
    make_context,
    make_request,
    run_scenario,
    stop_before_work,
)

EXECUTOR_NAMES = ("fake", "openai_chat", "openai_responses", "openai_agents", "anthropic")


def every_executor() -> dict[str, object]:
    """The five ``WorkflowExecutor`` implementations, each built so that constructing it touches no provider.

    The real ones get a tripwire client that fails the test if anything calls it, so a governed step that is not integrated
    can be shown to make no provider call. The conformance suite (``tests/conformance/test_agent_adapter_conformance.py``)
    builds each of the four provider executors over its canned transport instead.
    """
    from caliber.workflows.runtime import (
        AnthropicChatWorkflowExecutor,
        FakeWorkflowExecutor,
        OpenAIAgentsWorkflowExecutor,
        OpenAIChatWorkflowExecutor,
        OpenAIResponsesWorkflowExecutor,
    )

    class _Tripwire:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the provider client was used ({name})")

    tripwire = _Tripwire()
    return {
        "fake": FakeWorkflowExecutor(),
        "openai_chat": OpenAIChatWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
        "openai_responses": OpenAIResponsesWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
        "openai_agents": OpenAIAgentsWorkflowExecutor(api_key="unused", default_model="m"),
        "anthropic": AnthropicChatWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
    }
