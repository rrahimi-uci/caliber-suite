"""Deliberately broken adapters: at least one per rule of the conformance suite, each a ``ToyAdapter`` with exactly one defect (B08.5, #769).

A suite that cannot fail proves nothing. ``test_suite_fails_against_broken_adapters.py`` runs every rule against the adapter
listed for it here and requires that the rule fails (and that the intact ``ToyAdapter`` passes every rule), so a rule that is
emptied, loosened or never reaches its branch is found by a test, not by a reviewer. The defects are the ones the rules exist for:
an adapter that runs a protected tool, trusts the model's claims, forgets usage, fabricates it, retries a provider call, swallows a
cancellation, logs a payload, ignores a limit or carries state between steps.

Rules the platform's boundary enforces whatever an adapter does (``limits_before_work``, ``adapter_cannot_raise_limits``,
``invalid_request_refused`` and the copy half of ``input_isolation``) are shown to fail against a broken *boundary* instead: see the monkeypatched cases in the test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import itertools
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
)
from caliber.effects.records import MAX_ARGS_DEPTH, MAX_INTENT_BYTES
from caliber.testing.adapter_conformance import AdapterUnderTest
from caliber.workflows.agent_step import (
    ADAPTER_TIMEOUT,
    PROVIDER_ERROR,
    AgentStepInput,
    StepDeadlineReached,
    StepToolBudgetExceeded,
    failure,
    proposal_from_call,
)
from tests.conformance.toy_adapter import ToyAdapter, _Tally, _Unreadable, toy_provider

logger = logging.getLogger("tests.conformance.broken_adapters")


# ----------------------------------------------------------------------------------------------- result transformers


class _Rewrites(ToyAdapter):
    """A toy whose finished result is rewritten before it is returned: a defect in what the adapter reports."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        raise NotImplementedError

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        return self.rewrite(super().run_step(request, step_input))


class ReportsZeroUsage(_Rewrites):
    """completes: a finished step reports no usage, so nothing is charged for what was spent."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        return dataclasses.replace(result, usage=StepUsage(0, 0, 0, 0, 0))


class ForgetsUsageOnStop(_Rewrites):
    """budget_exhausted: a step the provider cut off reports nothing, so the spend is forgotten."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        if result.stop_reason == "max_tokens":
            return dataclasses.replace(result, usage=StepUsage(0, 0, 0, 0, 0))
        return result


class TimeoutIsAGenericError(_Rewrites):
    """provider_timeout: a timeout is reported as an ordinary provider error, so the retry logic cannot tell."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        if result.error_code == ADAPTER_TIMEOUT:
            return dataclasses.replace(result, error_code=PROVIDER_ERROR)
        return result


class FabricatesUsage(_Rewrites):
    """missing_usage_not_fabricated: figures the adapter made up are reported as the provider's."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        if result.usage_source == "estimated":
            return dataclasses.replace(result, usage_source="provider")
        return result


class ReportsProviderUsageAsEstimated(_Rewrites):
    """honest_usage_is_charged_as_reported: genuine usage is labelled an estimate, so it is charged as a guess."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        if result.usage_source == "provider":
            return dataclasses.replace(result, usage_source="estimated")
        return result


# ------------------------------------------------------------------------------------------ what the adapter does


class SkipsReadTools(ToyAdapter):
    """read_tool_call: the model's read call is acknowledged but the tool is never run."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        idle = {name: (lambda **_: "skipped") for name in step_input.read_tools}
        return super().run_step(
            request,
            AgentStepInput(step_input.agent, step_input.input_text, step_input.history, idle),
        )


class ExecutesProtectedTool(ToyAdapter):
    """proposes_protected_tool: the adapter resolves the protected tool from its binding and runs it, then proposes."""

    def _propose(
        self,
        request: AgentStepRequest,
        call: dict[str, Any],
        tally: _Tally,
        step_input: AgentStepInput,
    ) -> AgentStepResult:
        for binding in step_input.agent.tools:
            if binding.local_name == call["name"]:
                module = importlib.import_module(binding.module_path)
                getattr(module, binding.callable_name)(**call["arguments"]["args"])
        return super()._propose(request, call, tally, step_input)


class RaisesOnProviderError(ToyAdapter):
    """provider_error: the adapter lets the provider's exception escape instead of returning a typed result."""

    @staticmethod
    def _provider_failed(request: AgentStepRequest, exc: Exception) -> AgentStepResult:
        del request
        raise exc


class TrustsUnreadableOutput(ToyAdapter):
    """unreadable_output: output that cannot be read is taken for an empty answer."""

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        try:
            return super()._read_reply(reply, tally, step_input)
        except _Unreadable:
            tally.estimated = True
            return []


_STATE_FIELDS = ("state", "receipt_id")
_OTHER_CLAIMS = (
    "receipt",
    "approval",
    "approval_id",
    "committed",
    "platform_bound",
    "status",
    "effect_receipt",
)


def _stripping(fields: tuple[str, ...]) -> type[ToyAdapter]:
    class Strips(ToyAdapter):
        def _propose(
            self,
            request: AgentStepRequest,
            call: dict[str, Any],
            tally: _Tally,
            step_input: AgentStepInput,
        ) -> AgentStepResult:
            kept = {k: v for k, v in call["arguments"].items() if k not in fields}
            return super()._propose(request, {**call, "arguments": kept}, tally, step_input)

    return Strips


class StripsClaimedState(_stripping(_STATE_FIELDS)):  # type: ignore[misc]
    """claims_commit: a model's claim of ``state`` and a receipt id is dropped and the rest is proposed."""


class StripsOtherClaimedFields(_stripping(_OTHER_CLAIMS)):  # type: ignore[misc]
    """extra_fields_rejected: approval and status claims are dropped and the rest is proposed."""


class StripsAVolunteeredClass(_stripping(("effect_class",))):  # type: ignore[misc]
    """model_lies_about_effect_class: a class the model volunteers is silently dropped and the call is proposed anyway."""


class StripsVolunteeredClaims(_stripping(("connector", "connector_version", "effect_class"))):  # type: ignore[misc]
    """connector_version_claim: a connector, version or class the model volunteers is dropped and the call is proposed."""


class DropsTheCalledToolName(_Rewrites):
    """proposal_names_the_called_tool: the proposal no longer carries the name of the function the model called."""

    def rewrite(self, result: AgentStepResult) -> AgentStepResult:
        fixed = tuple(dataclasses.replace(a, tool=None) for a in result.proposed_actions)
        return dataclasses.replace(result, proposed_actions=fixed)


class LogsModelText(ToyAdapter):
    """hostile_model_text: the model's text is written to a log record."""

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        if isinstance(reply, Mapping):
            logger.info("the model said: %s", reply.get("text"))
        return super()._read_reply(reply, tally, step_input)


class LogsProviderErrorText(ToyAdapter):
    """errors_are_payload_free: the provider's exception message, which may quote the prompt, is logged."""

    @staticmethod
    def _provider_failed(request: AgentStepRequest, exc: Exception) -> AgentStepResult:
        logger.warning("provider call failed: %s", exc)
        return failure(request, PROVIDER_ERROR)


def _unbuildable(
    predicate: Callable[[ProposedAction | None, dict[str, Any]], bool],
) -> type[ToyAdapter]:
    """An adapter that does not handle a proposal the contract refuses, for the arguments ``predicate`` picks out."""

    class Crashes(ToyAdapter):
        def _propose(
            self,
            request: AgentStepRequest,
            call: dict[str, Any],
            tally: _Tally,
            step_input: AgentStepInput,
        ) -> AgentStepResult:
            envelope = call["arguments"]
            if predicate(None, envelope):
                ProposedAction(
                    envelope["connector"],
                    envelope["connector_version"],
                    envelope["effect_class"],
                    envelope["resource"],
                    envelope["args"],
                )  # the contract's refusal is not caught: it escapes
            return super()._propose(request, call, tally, step_input)

    return Crashes


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    return 0


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


class CrashesOnOversizedArguments(
    _unbuildable(lambda _a, e: len(json.dumps(e["args"])) > MAX_INTENT_BYTES)  # type: ignore[misc]
):
    """oversized_arguments_refused: arguments over the intent's limit raise out of the adapter."""


class CrashesOnDeepArguments(
    _unbuildable(lambda _a, e: _depth(e["args"]) > MAX_ARGS_DEPTH)  # type: ignore[misc]
):
    """deep_arguments_refused: arguments nested past the intent's limit raise out of the adapter."""


class CrashesOnLoneSurrogates(
    _unbuildable(  # type: ignore[misc]
        lambda _a, e: (
            _has_surrogate(json.dumps(e["args"], ensure_ascii=False))
            or _has_surrogate(e["resource"])
        )
    )
):
    """lone_surrogate_refused: a lone surrogate in a proposal raises out of the adapter."""


class CrashesOnBadArgumentText(ToyAdapter):
    """invalid_argument_text: arguments that are not JSON raise a ``JSONDecodeError`` out of the adapter."""

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        if isinstance(reply, Mapping):
            for raw in reply.get("tool_calls") or []:
                if isinstance(raw.get("arguments"), str):
                    json.loads(raw["arguments"])
        return super()._read_reply(reply, tally, step_input)


class IgnoresUnknownToolCalls(ToyAdapter):
    """unknown_tool_not_run: a call to a tool the agent does not have is dropped, and the step carries on to completion."""

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        names = {binding.local_name for binding in step_input.agent.tools}
        if isinstance(reply, Mapping):
            known = [c for c in reply.get("tool_calls") or [] if c.get("name") in names]
            reply = {**reply, "tool_calls": known}
        return super()._read_reply(reply, tally, step_input)


class TrustsBindingLevel(ToyAdapter):
    """read_tools_authoritative: a tool bound as ``read`` is run from its binding, whatever the platform classified."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        tools = dict(step_input.read_tools)
        for binding in step_input.agent.tools:
            if binding.side_effect_level == "read" and binding.local_name not in tools:
                module = importlib.import_module(binding.module_path)
                tools[binding.local_name] = getattr(module, binding.callable_name)
        widened = AgentStepInput(step_input.agent, step_input.input_text, step_input.history, tools)
        return super().run_step(request, widened)


class ProposesEveryCall(ToyAdapter):
    """one_proposed_action: every protected call in the response becomes a proposal."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        self._seen: list[dict[str, Any]] = []
        result = super().run_step(request, step_input)
        if result.stop_reason == "proposed_action" and len(self._seen) > 1:
            extra = tuple(self._action(call) for call in self._seen[1:])
            return dataclasses.replace(result, proposed_actions=(*result.proposed_actions, *extra))
        return result

    @staticmethod
    def _action(call: dict[str, Any]) -> ProposedAction:
        return proposal_from_call(call["name"], call["arguments"])

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        calls = super()._read_reply(reply, tally, step_input)
        read = set(step_input.read_tools)
        self._seen = [call for call in calls if call["name"] not in read]
        return calls


class KeepsGoingAfterAProposal(ToyAdapter):
    """proposal_ends_step: the provider is asked again after the model has proposed."""

    def _propose(
        self,
        request: AgentStepRequest,
        call: dict[str, Any],
        tally: _Tally,
        step_input: AgentStepInput,
    ) -> AgentStepResult:
        result = super()._propose(request, call, tally, step_input)
        self._call_provider([], [])
        return result


class FollowsHandoffs(ToyAdapter):
    """handoff_not_followed: a model that names a handoff target gets the target's tools run."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        self._targets = step_input.handoff_agents or {}
        return super().run_step(request, step_input)

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        reply = super()._call_provider(messages, tools)
        if isinstance(reply, Mapping):
            for call in reply.get("tool_calls") or []:
                for target in self._targets.values():
                    tool = target.read_tools.get(call["name"])
                    if tool is not None:
                        tool(**json.loads(call["arguments"]))
        return reply


class IgnoresTheTurnLimit(ToyAdapter):
    """turn_limit: the adapter keeps asking the provider past ``max_turns``."""

    def _limit_reached(self, request: AgentStepRequest, tally: _Tally) -> str | None:
        reason = super()._limit_reached(request, tally)
        return None if reason == "max_turns" else reason


class IgnoresTheToolCallLimit(ToyAdapter):
    """tool_call_limit: refused tool calls are swallowed and the step carries on asking the provider."""

    @staticmethod
    def _tool_limit_reached(request: AgentStepRequest, tally: _Tally) -> bool:
        return False

    @staticmethod
    def _run_read_tool(tool: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        try:
            return tool(**arguments)
        except (StepToolBudgetExceeded, StepDeadlineReached):
            return "refused"


class IgnoresTheTokenLimit(ToyAdapter):
    """token_limit: the adapter keeps asking the provider after the budget is spent."""

    def _limit_reached(self, request: AgentStepRequest, tally: _Tally) -> str | None:
        reason = super()._limit_reached(request, tally)
        return None if reason == "max_tokens" else reason


class IgnoresTheDeadline(ToyAdapter):
    """deadline_limit: neither the clock nor the platform's refusal of a late tool stops the adapter."""

    def _limit_reached(self, request: AgentStepRequest, tally: _Tally) -> str | None:
        reason = super()._limit_reached(request, tally)
        return None if reason == "deadline" else reason

    @staticmethod
    def _run_read_tool(tool: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        try:
            return tool(**arguments)
        except StepDeadlineReached:
            return "refused"


class AcceptsOverBudgetResponses(ToyAdapter):
    """token_limit: a response that took the step past ``max_tokens`` is still acted on (the proposal is returned)."""

    @staticmethod
    def _over_budget(request: AgentStepRequest, tally: _Tally) -> bool:
        return False


class SendsToolErrorText(ToyAdapter):
    """read_tool_failure_is_class_only: a failing read tool's exception message is handed to the provider as its result."""

    @staticmethod
    def _run_read_tool(tool: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        try:
            return tool(**arguments)
        except (StepToolBudgetExceeded, StepDeadlineReached):
            raise
        except Exception as exc:
            return f"error: {exc}"


class OffersHandoffTargets(ToyAdapter):
    """offered_tools_are_the_agents_own: the model is offered a ``transfer_to_<agent>`` tool for each handoff target."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        self._targets = list(step_input.handoff_agents or {})
        return super().run_step(request, step_input)

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        return super()._call_provider(
            messages, [*tools, *(f"transfer_to_{t}" for t in self._targets)]
        )


class ReportsAsyncioTimeoutsAsErrors(ToyAdapter):
    """asyncio_timeout_is_adapter_timeout: an ``asyncio`` timeout is not recognised (the Python 3.10 miss), so it is an error."""

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        try:
            return super()._call_provider(messages, tools)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("the provider failed") from exc


class ClampsMalformedUsage(ToyAdapter):
    """malformed_usage_charged_whole: usage that cannot be read is taken as one token."""

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        if isinstance(reply, Mapping) and isinstance(reply.get("usage"), Mapping):
            usage = reply["usage"]
            if not all(type(usage.get(k)) is int for k in ("prompt", "completion")):
                reply = {**reply, "usage": {"prompt": 1, "completion": 0}}
        return super()._read_reply(reply, tally, step_input)


class RetriesTheProviderOnce(ToyAdapter):
    """no_hidden_retry: a failed provider call is quietly made again, a second charge nobody reserved."""

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        try:
            return super()._call_provider(messages, tools)
        except Exception:
            return super()._call_provider(messages, tools)


class SwallowsCancellation(ToyAdapter):
    """cancellation_propagates: ``BaseException`` from the provider call is caught and the step carries on as an empty answer."""

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        try:
            return super()._call_provider(messages, tools)
        except BaseException:
            return {"text": "", "tool_calls": [], "usage": {"prompt": 1, "completion": 1}}


class VariesBetweenRuns(ToyAdapter):
    """deterministic: every proposal carries a counter, so the same step is answered differently each time."""

    _counter = itertools.count(1)

    def _propose(
        self,
        request: AgentStepRequest,
        call: dict[str, Any],
        tally: _Tally,
        step_input: AgentStepInput,
    ) -> AgentStepResult:
        envelope = dict(call["arguments"])
        envelope["args"] = {**envelope["args"], "nonce": next(self._counter)}
        return super()._propose(request, {**call, "arguments": envelope}, tally, step_input)


class RemembersTheLastResult(ToyAdapter):
    """fresh_state_per_step: the adapter answers the second step with the first's result."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        if getattr(self, "_last", None) is not None:
            return self._last  # type: ignore[no-any-return]
        self._last = super().run_step(request, step_input)
        return self._last  # type: ignore[no-any-return]


class RaisesItsOwnLimitsOnItsCopy(ToyAdapter):
    """input_isolation: the adapter rewrites the limits of the request it was handed."""

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        object.__setattr__(request.limits, "max_tokens", 2_000_000_000)
        return super().run_step(request, step_input)


@dataclass(frozen=True)
class Mutant:
    """A broken adapter and the rule it must fail."""

    rule: str
    adapter: type[ToyAdapter]

    @property
    def name(self) -> str:
        return self.adapter.__name__

    def subject(self) -> AdapterUnderTest:
        adapter = self.adapter
        return AdapterUnderTest(
            name=self.name,
            build=lambda script, probe: adapter(toy_provider(script, probe), now=probe.clock.now),
        )


MUTANTS: tuple[Mutant, ...] = (
    Mutant("completes", ReportsZeroUsage),
    Mutant("read_tool_call", SkipsReadTools),
    Mutant("proposes_protected_tool", ExecutesProtectedTool),
    Mutant("model_lies_about_effect_class", StripsAVolunteeredClass),
    Mutant("budget_exhausted", ForgetsUsageOnStop),
    Mutant("provider_error", RaisesOnProviderError),
    Mutant("provider_timeout", TimeoutIsAGenericError),
    Mutant("unreadable_output", TrustsUnreadableOutput),
    Mutant("claims_commit", StripsClaimedState),
    Mutant("hostile_model_text", LogsModelText),
    Mutant("extra_fields_rejected", StripsOtherClaimedFields),
    Mutant("oversized_arguments_refused", CrashesOnOversizedArguments),
    Mutant("deep_arguments_refused", CrashesOnDeepArguments),
    Mutant("lone_surrogate_refused", CrashesOnLoneSurrogates),
    Mutant("invalid_argument_text", CrashesOnBadArgumentText),
    Mutant("unknown_tool_not_run", IgnoresUnknownToolCalls),
    Mutant("connector_version_claim", StripsVolunteeredClaims),
    Mutant("proposal_names_the_called_tool", DropsTheCalledToolName),
    Mutant("read_tools_authoritative", TrustsBindingLevel),
    Mutant("one_proposed_action", ProposesEveryCall),
    Mutant("proposal_ends_step", KeepsGoingAfterAProposal),
    Mutant("handoff_not_followed", FollowsHandoffs),
    Mutant("turn_limit", IgnoresTheTurnLimit),
    Mutant("tool_call_limit", IgnoresTheToolCallLimit),
    Mutant("token_limit", IgnoresTheTokenLimit),
    Mutant("token_limit", AcceptsOverBudgetResponses),
    Mutant("read_tool_failure_is_class_only", SendsToolErrorText),
    Mutant("offered_tools_are_the_agents_own", OffersHandoffTargets),
    Mutant("deadline_limit", IgnoresTheDeadline),
    Mutant("honest_usage_is_charged_as_reported", ReportsProviderUsageAsEstimated),
    Mutant("missing_usage_not_fabricated", FabricatesUsage),
    Mutant("malformed_usage_charged_whole", ClampsMalformedUsage),
    Mutant("asyncio_timeout_is_adapter_timeout", ReportsAsyncioTimeoutsAsErrors),
    Mutant("no_hidden_retry", RetriesTheProviderOnce),
    Mutant("cancellation_propagates", SwallowsCancellation),
    Mutant("errors_are_payload_free", LogsProviderErrorText),
    Mutant("deterministic", VariesBetweenRuns),
    Mutant("fresh_state_per_step", RemembersTheLastResult),
    Mutant("input_isolation", RaisesItsOwnLimitsOnItsCopy),
)

#: Rules the platform's boundary enforces for any adapter: shown to fail against a broken boundary, not a broken adapter.
BOUNDARY_RULES: frozenset[str] = frozenset(
    {"limits_before_work", "adapter_cannot_raise_limits", "invalid_request_refused"}
)
