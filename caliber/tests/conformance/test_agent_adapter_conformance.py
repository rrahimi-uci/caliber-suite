"""The provider-adapter conformance suite, run against every shipped adapter (B08.5, #769).

The four shipped adapters (``OpenAIChatWorkflowExecutor``, ``OpenAIResponsesWorkflowExecutor``, ``OpenAIAgentsWorkflowExecutor`` and
``AnthropicChatWorkflowExecutor``) are the real classes over canned provider transports: no provider is called. Each is run against
every rule of :mod:`caliber.testing.adapter_conformance`. The rules answer the acceptance criteria of the task:

* the disposition (``stop_reason``) is one of the contract's, and the output is schema-validated: every run checks that the result
  is a valid ``agent_step_result`` that answers the asked step (``completes``, ``deterministic``, ``fresh_state_per_step``);
* bounds are enforced (``limits_before_work``, ``turn_limit``, ``tool_call_limit``, ``token_limit``, ``deadline_limit``,
  ``adapter_cannot_raise_limits``);
* a ``committed`` claim, and every other field the contract does not have, is rejected (``claims_commit``,
  ``extra_fields_rejected``);
* a protected tool call is proposed and never run (``proposes_protected_tool``, ``read_tools_authoritative``,
  ``one_proposed_action``);
* and what the model and the provider say is untrusted (``hostile_model_text``, the argument-size rules, ``errors_are_payload_free``).

``test_suite_fails_against_broken_adapters.py`` shows that the suite fails when an adapter is broken, and
``test_agent_adapter_governed_plan.py`` runs the same adapters under the interpreter with the reservation ledger.
"""

from __future__ import annotations

import asyncio
import io
import logging
import warnings
from collections.abc import Callable
from typing import Any

import pytest

from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    RULES,
    RULES_BY_ID,
    AdapterUnderTest,
    Call,
    ConformanceFailure,
    ConformanceReport,
    Fault,
    ProviderUsage,
    Reply,
    Rule,
    Status,
    Unsupported,
    _describe,
    _guard,
    _rule,
    check_rule,
    conformance_report,
    fault_exception,
    observe,
    run_rule,
    scenario_script,
)
from caliber.testing.agent_step_conformance import (
    PROTECTED_CLASS,
    PROTECTED_TOOL,
    Scenario,
    assert_claim_mismatch,
    canary_protected_tool,
    canary_read_tool,
    capture_logs,
    make_context,
    running,
)
from tests.conformance.anthropic_stub import anthropic_subject
from tests.conformance.test_agent_adapter_openai_agents import (
    AGENTS_UNBOUNDED_IN_LOOP,
    agents_subject,
)
from tests.conformance.test_agent_adapter_openai_chat import chat_subject
from tests.conformance.test_agent_adapter_openai_responses import responses_subject
from tests.conformance.toy_adapter import ToyAdapter, toy_provider, toy_subject

SUBJECTS: tuple[AdapterUnderTest, ...] = (
    chat_subject(),
    responses_subject(),
    agents_subject(),
    anthropic_subject(),
)


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.id)
@pytest.mark.parametrize("subject", SUBJECTS, ids=lambda subject: subject.name)
def test_the_adapter_conforms_to_the_rule(subject: AdapterUnderTest, rule: Rule) -> None:
    result = check_rule(subject, rule.id)
    assert result.status is Status.PASS, f"{subject.name} did not run {rule.id}: {result.detail}"


def test_the_registered_adapters_are_the_four_shipped_ones_and_every_rule_runs_for_each() -> None:
    assert [subject.name for subject in SUBJECTS] == [
        "openai_chat",
        "openai_responses",
        "openai_agents",
        "anthropic",
    ]
    for subject in SUBJECTS:
        report = conformance_report(subject)
        assert report.passed, report.render()
        assert report.unsupported == () and len(report.results) == len(RULES), report.render()


def test_no_shipped_adapter_declares_a_stop_it_cannot_make_between_provider_calls() -> None:
    """The Agents SDK offers ``tool_use_behavior`` (stop at a tool, or a function), so every stop is available to every adapter."""
    declared = {subject.name: subject.unbounded_in_loop for subject in SUBJECTS}
    assert declared == dict.fromkeys(
        ("openai_chat", "openai_responses", "openai_agents", "anthropic"), frozenset()
    )
    assert frozenset() == AGENTS_UNBOUNDED_IN_LOOP
    assert "cannot stop asking" not in conformance_report(SUBJECTS[2]).render()


def test_a_subject_that_declares_stops_it_cannot_make_has_them_printed_first_in_the_report() -> (
    None
):
    subject = AdapterUnderTest(
        name="declares",
        build=toy_subject().build,
        unbounded_in_loop=frozenset({"deadline", "tokens"}),
    )
    lines = conformance_report(subject).render().splitlines()
    assert lines[0] == "adapter conformance: declares"
    assert lines[1] == "  cannot stop asking the provider at: deadline, tokens"
    # and the rules then do not demand those stops of it
    assert run_rule(subject, "deadline_limit").status is Status.PASS


def test_the_rules_are_registered_once_with_a_title_and_a_stable_order() -> None:
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids)) == len(RULES_BY_ID) >= 35
    assert all(rule.title and rule.title == rule.title.strip() for rule in RULES)
    assert [rule.id for rule in RULES[:9]] == [scenario.value for scenario in Scenario][:9], (
        "the harness' scenarios come first"
    )
    with pytest.raises(ValueError, match="duplicate conformance rule completes"):
        _rule("completes", "again")(lambda subject: None)
    assert RULES_BY_ID["completes"].optional is False


# ----------------------------------------------------------------------------- how a subject is judged and reported


def _subject_that_cannot_stage(reason: str) -> AdapterUnderTest:
    def build(script: Any, probe: Any) -> object:
        raise Unsupported(reason)

    return AdapterUnderTest(name="cannot-stage", build=build)


def test_an_optional_rule_may_be_unsupported_and_a_mandatory_one_may_not() -> None:
    report = conformance_report(_subject_that_cannot_stage("no such transport"))
    by_rule = {result.rule: result for result in report.results}
    optional = {rule.id for rule in RULES if rule.optional}
    assert optional == {
        "invalid_argument_text",
        "read_tool_failure_is_class_only",
        "offered_tools_are_the_agents_own",
    }
    assert {r.rule for r in report.unsupported} == optional
    assert by_rule["invalid_argument_text"].detail == "no such transport"
    mandatory = [r for r in report.results if r.rule not in optional]
    assert all(r.status is Status.FAIL and "mandatory" in r.detail for r in mandatory)
    assert not report.passed
    with pytest.raises(ConformanceFailure, match="mandatory"):
        check_rule(_subject_that_cannot_stage("x"), "completes")
    assert check_rule(_subject_that_cannot_stage("x"), "invalid_argument_text").status is (
        Status.UNSUPPORTED
    )


def test_a_build_that_raises_is_a_failure_that_names_the_class() -> None:
    def build(script: Any, probe: Any) -> object:
        raise ZeroDivisionError("a bug in the subject")

    result = run_rule(AdapterUnderTest(name="buggy", build=build), "completes")
    assert result.status is Status.FAIL and "ZeroDivisionError" in result.detail


def test_an_exception_in_a_rule_is_a_failure_and_not_a_crash_of_the_report() -> None:
    def build(script: Any, probe: Any) -> object:
        return object()  # not an adapter: ``run_agent_step`` refuses it, the rule reports it

    report = conformance_report(AdapterUnderTest(name="not-an-adapter", build=build))
    assert not report.passed
    # Every rule that reaches the adapter fails; the one that is the boundary's alone never touches it.
    assert {r.rule for r in report.results} - {r.rule for r in report.failed} == {
        "invalid_request_refused"
    }
    assert "DOES NOT CONFORM" in report.render()


def test_a_subject_may_only_name_in_loop_stops_that_exist() -> None:
    with pytest.raises(ValueError, match="unknown in-loop stops"):
        AdapterUnderTest(
            name="x", build=lambda s, p: object(), unbounded_in_loop=frozenset({"nap"})
        )
    subject = AdapterUnderTest(
        name="x", build=lambda s, p: object(), unbounded_in_loop=frozenset({"tokens"})
    )
    assert subject.stops_in_loop("deadline") and not subject.stops_in_loop("tokens")


def test_the_report_is_one_table_with_a_verdict() -> None:
    report = conformance_report(toy_subject())
    assert isinstance(report, ConformanceReport) and report.passed
    lines = report.render().splitlines()
    assert lines[0] == "adapter conformance: toy" and lines[-1] == "CONFORMS"
    assert len(lines) == len(RULES) + 2
    assert all(line.startswith("  PASS") for line in lines[1:-1])


def test_the_suite_refuses_to_run_without_assertions() -> None:
    _guard(True)
    with pytest.raises(RuntimeError, match="python -O"):
        _guard(False)


def test_an_assertion_without_a_message_is_reported_by_the_statement_that_failed() -> None:
    try:
        raise AssertionError
    except AssertionError as exc:
        assert _describe(exc) == "raise AssertionError"
    assert _describe(AssertionError("said so")) == "said so"
    assert _describe(AssertionError()) == "an assertion failed"


def test_the_provider_script_vocabulary() -> None:
    assert ProviderUsage(3, 4).total_tokens == 7 and ProviderUsage(3, 4, 9).total_tokens == 9
    assert isinstance(fault_exception(Fault("cancel")), asyncio.CancelledError)
    assert isinstance(fault_exception(Fault("interrupt")), KeyboardInterrupt)
    assert str(fault_exception(Fault("error", "boom"))) == "upstream said boom"
    with pytest.raises(ValueError, match="subject's own transport"):
        fault_exception(Fault("timeout"))
    with pytest.raises(Unsupported, match="another step's identity"):
        scenario_script(Scenario.ANOTHER_STEPS_RESULT, make_context())
    assert repr(MALFORMED_USAGE) == "MALFORMED_USAGE"
    assert (str(Scenario.COMPLETES), str(Status.PASS)) == ("completes", "pass"), "StrEnum, on 3.10"
    for scenario in Scenario:
        if scenario is not Scenario.ANOTHER_STEPS_RESULT:
            assert scenario_script(scenario, make_context(scenario))


# --------------------------------------------------- what every run checks, whatever the rule: output the suite sees


def _leaky(how: Callable[[Any], None]) -> AdapterUnderTest:
    class Leaky(ToyAdapter):
        def _read_reply(self, reply: Any, tally: Any, step_input: Any) -> Any:
            how(reply)
            return super()._read_reply(reply, tally, step_input)

    return AdapterUnderTest(
        name="leaky",
        build=lambda script, probe: Leaky(toy_provider(script, probe), now=probe.clock.now),
    )


@pytest.mark.parametrize("sink", ["stdout", "stderr", "warnings"])
def test_provider_text_in_any_output_stream_fails_the_run(sink: str) -> None:
    import contextlib
    import sys

    def leak(reply: Any) -> None:
        text = str(reply["text"])[:2000]
        if sink == "warnings":
            warnings.warn(text, stacklevel=1)
        else:
            print(text, file=sys.stdout if sink == "stdout" else sys.stderr)

    with contextlib.redirect_stdout(io.StringIO()):
        result = run_rule(_leaky(leak), "hostile_model_text")
    assert result.status is Status.FAIL and "carries the provider's text" in result.detail


def test_observe_returns_both_runs_and_what_the_adapter_was_asked() -> None:
    obs = observe(toy_subject(), (Reply(text="done", usage=ProviderUsage(1, 1)),))
    assert obs.result == obs.direct and obs.result.stop_reason == "completed"
    assert obs.request.step_request_id == obs.ctx.request.step_request_id
    assert obs.ctx.provider_calls == obs.direct_ctx.provider_calls == 1
    assert PROTECTED_TOOL not in obs.ctx.step_input.read_tools


def test_a_run_that_executes_the_protected_canary_fails_whatever_the_rule() -> None:
    class Runs(ToyAdapter):
        def run_step(self, request: Any, step_input: Any) -> Any:
            for binding in step_input.agent.tools:
                if binding.side_effect_level != "read":
                    import importlib

                    module = importlib.import_module(binding.module_path)
                    getattr(module, binding.callable_name)()
            return super().run_step(request, step_input)

    subject = AdapterUnderTest(
        name="runs", build=lambda s, p: Runs(toy_provider(s, p), now=p.clock.now)
    )
    result = run_rule(subject, "completes")
    assert result.status is Status.FAIL and "executed a protected tool" in result.detail
    assert Call("x").arguments == {}


def test_a_cancellation_the_adapter_turns_into_another_exception_is_still_a_failure() -> None:
    class Rewraps(ToyAdapter):
        def _call_provider(self, messages: Any, tools: Any) -> Any:
            try:
                return super()._call_provider(messages, tools)
            except asyncio.CancelledError:
                raise SystemExit("cancelled") from None

    subject = AdapterUnderTest(
        name="rewraps", build=lambda s, p: Rewraps(toy_provider(s, p), now=p.clock.now)
    )
    result = run_rule(subject, "cancellation_propagates")
    assert result.status is Status.FAIL and "not CancelledError" in result.detail


def test_a_rule_that_crashes_is_reported_as_a_failure_naming_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(subject: AdapterUnderTest) -> None:
        raise KeyError("a bug in a rule")

    monkeypatch.setitem(RULES_BY_ID, "crashing", Rule("crashing", "a rule that crashes", crash))
    result = run_rule(toy_subject(), "crashing")
    assert result.status is Status.FAIL and result.detail.startswith("KeyError")


def test_binding_a_claim_that_differs_is_not_something_the_platform_does() -> None:
    ctx = make_context()
    action = ctx.protected_action(claimed_effect_class="read")
    assert assert_claim_mismatch(action, PROTECTED_CLASS).name == "effect_class"
    with pytest.raises(AssertionError, match="bound a claim that differs"):
        assert_claim_mismatch(ctx.protected_action(), PROTECTED_CLASS)


def test_a_canary_tool_called_outside_a_run_says_so_instead_of_an_index_error() -> None:
    with pytest.raises(RuntimeError, match="outside a conformance run"):
        canary_protected_tool()
    ctx = make_context()
    with running(ctx):
        canary_protected_tool(x=1)
        canary_read_tool(y=2)
    assert ctx.protected_calls == [{"x": 1}] and ctx.read_calls == [{"y": 2}]
    with pytest.raises(RuntimeError, match="outside a conformance run"):
        canary_read_tool()


def test_log_capture_sees_every_logger_every_level_and_the_extra_fields() -> None:
    quiet = logging.getLogger("conformance.test.quiet")
    private = logging.getLogger("conformance.test.private")
    quiet.setLevel(logging.ERROR)
    private.propagate = False
    capture, restore = capture_logs()
    try:
        quiet.info("dropped by its own level, seen by the capture")
        private.warning("a logger that does not propagate")
        private.info("with fields", extra={"provider_payload": "payload-in-an-extra"})
    finally:
        restore()
    text = "\n".join(capture.text)
    assert "dropped by its own level" in text and "does not propagate" in text
    assert "payload-in-an-extra" in text
    assert quiet.level == logging.ERROR and private.handlers == [], "the loggers are restored"


def test_the_probe_records_what_the_transport_says_it_offered_and_sent() -> None:
    ctx = make_context()
    ctx.provider_called()
    ctx.provider_called(tools=["a", "b"], sent="the request text")
    assert ctx.provider_calls == 2
    assert ctx.offered_tools == [("a", "b")] and ctx.sent_to_provider == ["the request text"]


def test_a_transport_that_reports_nothing_leaves_the_rules_that_need_it_unsupported() -> None:
    def build(script: Any, probe: Any) -> object:
        class Quiet(ToyAdapter):
            def _call_provider(self, messages: Any, tools: Any) -> Any:
                reply = super()._call_provider(messages, tools)
                probe.offered_tools.clear()
                probe.sent_to_provider.clear()
                return reply

        return Quiet(toy_provider(script, probe), now=probe.clock.now)

    subject = AdapterUnderTest(name="quiet", build=build)
    for rule in ("offered_tools_are_the_agents_own", "read_tool_failure_is_class_only"):
        assert run_rule(subject, rule).status is Status.UNSUPPORTED, rule
