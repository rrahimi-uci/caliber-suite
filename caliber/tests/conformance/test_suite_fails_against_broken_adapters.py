"""The conformance suite fails when an adapter, or the platform's boundary, is broken (B08.5, #769).

Every rule is run against the intact reference adapter (it must pass all of them) and against the one broken adapter written for it
(it must fail that rule, for a reason the rule states and not because something else went wrong). The rules the boundary enforces
whatever an adapter does are run against a boundary with that enforcement removed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from caliber.contracts.agent_step import AgentStepRequest, StepLimits
from caliber.testing.adapter_conformance import (
    RULES,
    RULES_BY_ID,
    ConformanceFailure,
    Status,
    check_rule,
    conformance_report,
    run_rule,
)
from caliber.workflows import agent_step
from tests.conformance.broken_adapters import BOUNDARY_RULES, MUTANTS, Mutant
from tests.conformance.toy_adapter import toy_subject

# What an unexpected crash looks like in a result (``TypeError: ...``): a mutant must fail on purpose, not by accident.
_ACCIDENT = re.compile(r"^(\w+Error|\w+Exception|KeyboardInterrupt|CancelledError)\b[:(]")


def test_the_intact_reference_adapter_passes_every_rule() -> None:
    report = conformance_report(toy_subject())
    assert report.passed, report.render()
    assert report.unsupported == () and len(report.results) == len(RULES)


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: f"{m.rule}-{m.name}")
def test_each_rule_fails_against_its_broken_adapter(mutant: Mutant) -> None:
    result = run_rule(mutant.subject(), mutant.rule)
    assert result.status is Status.FAIL, f"{mutant.name} was not caught by {mutant.rule}"
    assert result.detail and not _ACCIDENT.match(result.detail), result.detail
    with pytest.raises(ConformanceFailure, match=mutant.rule):
        check_rule(mutant.subject(), mutant.rule)


def test_every_rule_has_a_broken_adapter_or_a_broken_boundary() -> None:
    covered = {mutant.rule for mutant in MUTANTS} | BOUNDARY_RULES
    assert covered == set(RULES_BY_ID), sorted(set(RULES_BY_ID) ^ covered)
    assert len(MUTANTS) == len({mutant.rule for mutant in MUTANTS}), "one broken adapter per rule"


def test_a_broken_adapter_makes_the_report_say_so() -> None:
    mutant = next(m for m in MUTANTS if m.rule == "provider_error")
    report = conformance_report(mutant.subject())
    assert not report.passed
    assert "provider_error" in {r.rule for r in report.failed}
    text = report.render()
    assert "DOES NOT CONFORM" in text and "FAIL" in text and "provider_error" in text


# ------------------------------------------------------------------------------------------------ a broken boundary


def _no_pre_work_stop(request: Any, *, clock: Any = None) -> None:
    del request, clock


def _limits_not_enforced(
    request: Any, result: Any, meter: Any, started_at: Any, elapsed: Any
) -> Any:
    del request, meter, started_at, elapsed
    return result


@pytest.mark.parametrize(
    ("rule", "attribute", "replacement"),
    [
        ("limits_before_work", "before_work_stop", _no_pre_work_stop),
        ("adapter_cannot_raise_limits", "_enforce_limits", _limits_not_enforced),
        ("input_isolation", "_metered_input", lambda step_input, meter: step_input),
    ],
)
def test_the_boundary_rules_fail_when_the_boundary_stops_enforcing(
    monkeypatch: pytest.MonkeyPatch, rule: str, attribute: str, replacement: Callable[..., Any]
) -> None:
    assert run_rule(toy_subject(), rule).status is Status.PASS
    monkeypatch.setattr(agent_step, attribute, replacement)
    result = run_rule(toy_subject(), rule)
    assert result.status is Status.FAIL and not _ACCIDENT.match(result.detail), result.detail


def test_invalid_request_refused_fails_when_the_boundary_stops_validating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_rule(toy_subject(), "invalid_request_refused").status is Status.PASS

    def unchecked(payload: Any) -> AgentStepRequest:
        return AgentStepRequest(
            **{name: payload[name] for name in _REQUEST_FIELDS},
            limits=StepLimits(**payload["limits"]),
        )

    def record(self: AgentStepRequest) -> dict[str, Any]:
        return {
            **{name: getattr(self, name) for name in _REQUEST_FIELDS},
            "limits": self.limits.record(),
        }

    monkeypatch.setattr(agent_step, "parse_step_request", unchecked)
    monkeypatch.setattr(AgentStepRequest, "record", record)
    result = run_rule(toy_subject(), "invalid_request_refused")
    assert result.status is Status.FAIL and "should have refused" in result.detail, result.detail


_REQUEST_FIELDS = (
    "step_request_id",
    "workflow_run_id",
    "node_id",
    "occurrence_id",
    "attempt",
    "run_manifest_digest",
    "context_manifest_digest",
    "grant_id",
)
