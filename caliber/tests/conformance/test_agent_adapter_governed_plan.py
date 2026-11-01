"""Each shipped adapter inside a governed plan: the platform's side of the conformance rules, through the real interpreter (B08.5, #769).

``test_agent_adapter_conformance.py`` runs every rule against the adapter and the boundary. These tests run the same real adapters
(over their canned transports) *under the interpreter*, with the reservation ledger and the proposal store on a migrated SQLite
database, to show what no adapter-only rule can: the reservation is taken before the provider is reached and charged by what the
adapter reported (honest usage as reported; missing, malformed, failed or cancelled as the whole reservation), a replayed step never
reaches the provider again, a handoff is refused before any adapter runs, a protected call is one recorded proposal and never an
effect, and no provider text reaches the run's error. The PostgreSQL races of the ledger are in
``tests/egi_scenarios/test_b08_token_reservation.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from sqlalchemy import select

from caliber.budget.step_reservation import run_token_usage
from caliber.db.models import CaliberEffectProposal, CaliberStepTokenReservation
from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    AdapterUnderTest,
    Call,
    Fault,
    ProviderUsage,
    Reply,
    Script,
)
from caliber.testing.agent_step_conformance import SECRET_MARKER, make_context
from caliber.workflows.runtime import execute
from tests import test_governed_agent_step as governed
from tests.conformance.anthropic_stub import anthropic_subject
from tests.conformance.test_agent_adapter_openai_agents import agents_subject
from tests.conformance.test_agent_adapter_openai_chat import chat_subject
from tests.conformance.test_agent_adapter_openai_responses import responses_subject
from tests.test_governed_agent_step import (
    LIMITS,
    PROTECTED,
    RESOURCE,
    Canaries,
    _manifest,
    _plan,
)

# The governed-step fixtures: a migrated SQLite database with a run and the tool registry seeded (``world`` needs the template).
_migrated_template = governed._migrated_template
world = governed.world

SUBJECTS = [chat_subject, responses_subject, agents_subject, anthropic_subject]
#: The ceiling the platform reserves for the step: the producer's ``max_tokens``, after the platform's own ceilings.
CEILING = LIMITS.max_tokens
INPUT = "please ask for the pay statement"


def _proposal(**changes: Any) -> dict[str, Any]:
    """What a model sends the governed plan's protected tool: the tool's arguments and the destination it names (B04.6).

    No connector, version or class: the platform binds those from the called function's trusted identity.
    """
    envelope = {"resource": RESOURCE, "args": {"document_type": "pay_statement"}}
    envelope.update(changes)
    return envelope


def _run(
    world: Any,
    subject: AdapterUnderTest,
    script: Script,
    *,
    manifest: dict[str, Any] | None = None,
    **context: Any,
) -> tuple[Any, Any, Canaries]:
    probe = make_context()
    canaries = Canaries()
    result = execute(
        _plan(world, manifest=manifest, **context),
        INPUT,
        executor=subject.build(script, probe),
        extra_tools=canaries.tools(),
    )
    return result, probe, canaries


def _rows(world: Any) -> list[CaliberStepTokenReservation]:
    with world() as session:
        return list(session.execute(select(CaliberStepTokenReservation)).scalars())


def _proposals(world: Any) -> list[CaliberEffectProposal]:
    with world() as session:
        return list(session.execute(select(CaliberEffectProposal)).scalars())


@pytest.fixture(params=SUBJECTS, ids=lambda factory: factory().name)
def subject(request: pytest.FixtureRequest) -> AdapterUnderTest:
    return request.param()  # type: ignore[no-any-return]


# ------------------------------------------------------------------------------------- a protected call is a proposal


def test_a_protected_call_is_one_recorded_proposal_charged_as_reported_and_never_an_effect(
    world: Any, subject: AdapterUnderTest
) -> None:
    script = (
        Reply(
            text=None,
            calls=(Call(PROTECTED, _proposal()),),
            usage=ProviderUsage(100, 20),
        ),
    )
    result, probe, canaries = _run(world, subject, script)
    assert result.status == "blocked" and result.error == "waiting_approval:agent", result.error
    assert canaries.mutations == [], "the protected tool was executed"
    (proposal,) = _proposals(world)
    assert (proposal.state, proposal.effect_class) == ("proposed", "irreversible_write")
    assert probe.provider_calls == 1
    (row,) = _rows(world)
    assert (row.state, row.reserved_tokens, row.charged_tokens, row.charge_basis) == (
        "settled",
        CEILING,
        120,
        "reported",
    )


def test_a_model_that_lies_about_the_effect_class_records_nothing(
    world: Any, subject: AdapterUnderTest
) -> None:
    script = (
        Reply(
            text=None,
            calls=(Call(PROTECTED, _proposal(effect_class="read")),),
            usage=ProviderUsage(100, 20),
        ),
    )
    result, probe, canaries = _run(world, subject, script)
    # The model volunteered a class the tool's envelope has no field for: the call is unreadable and nothing is recorded.
    assert result.status == "error" and "malformed_result" in (result.error or ""), result.error
    assert canaries.mutations == [] and _proposals(world) == []
    assert probe.provider_calls == 1


# ------------------------------------------------------------------------------- reservations and what is charged


@pytest.mark.parametrize(
    "script_name",
    [
        "provider_error",
        "timeout",
        "unreadable",
        "missing_usage",
        "malformed_usage",
        "claimed_commit",
    ],
)
def test_a_step_the_provider_or_the_adapter_could_not_account_for_is_charged_its_whole_reservation(
    world: Any, subject: AdapterUnderTest, script_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    marker = SECRET_MARKER
    scripts: dict[str, Script] = {
        "provider_error": (Fault("error", marker),),
        "timeout": (Fault("timeout", marker),),
        "unreadable": (Reply(text=marker, usage=MALFORMED_USAGE),),
        "missing_usage": (Reply(text=marker, usage=None),),
        "malformed_usage": (
            Reply(text=None, calls=(Call(PROTECTED, _proposal()),), usage=MALFORMED_USAGE),
        ),
        "claimed_commit": (
            Reply(text=None, calls=(Call(PROTECTED, _proposal(state=marker, receipt_id=marker)),)),
        ),
    }
    result, probe, canaries = _run(world, subject, scripts[script_name])
    assert result.status == "error", result.error
    assert marker not in repr(result) and marker not in caplog.text, (
        "the provider's text reached the run"
    )
    assert canaries.mutations == [] and _proposals(world) == []
    assert probe.provider_calls == 1
    (row,) = _rows(world)
    assert (row.state, row.charged_tokens, row.charge_basis) == ("settled", CEILING, "conservative")


def test_a_step_that_answers_without_proposing_is_settled_as_reported_and_its_replay_never_reaches_the_provider(
    world: Any, subject: AdapterUnderTest
) -> None:
    script = (Reply(text="done", usage=ProviderUsage(100, 20)),)
    first, probe, _ = _run(world, subject, script)
    assert first.status == "error" and "without an output channel" in (first.error or "")
    (row,) = _rows(world)
    assert (row.charged_tokens, row.charge_basis) == (120, "reported")
    assert probe.provider_calls == 1

    again, replay_probe, canaries = _run(world, subject, script)
    assert again.status == "error"
    assert "reservation_replayed" in (again.error or ""), again.error
    assert replay_probe.provider_calls == 0, "a replayed step reached the provider a second time"
    assert canaries.mutations == []
    (kept,) = _rows(world)
    assert kept.charged_tokens == 120, "the replay changed the charge"


def test_a_step_that_would_pass_the_runs_budget_is_refused_before_the_provider(
    world: Any, subject: AdapterUnderTest
) -> None:
    script = (
        Reply(text=None, calls=(Call(PROTECTED, _proposal()),), usage=ProviderUsage(100, 20)),
    )
    result, probe, canaries = _run(world, subject, script, run_token_budget=CEILING - 1)
    assert result.status == "error" and "token_budget_exhausted" in (result.error or ""), (
        result.error
    )
    assert probe.provider_calls == 0, "the provider was reached for a step the budget refused"
    assert _rows(world) == [] and _proposals(world) == [] and canaries.mutations == []


def test_a_timeout_keeps_its_reservation_and_the_retry_is_reserved_again_and_called_once(
    world: Any, subject: AdapterUnderTest
) -> None:
    manifest = _manifest()
    manifest["nodes"]["agent"]["execution_policy"] = {"max_retries": 1}
    script = (
        Fault("timeout", SECRET_MARKER),
        Reply(text=None, calls=(Call(PROTECTED, _proposal()),), usage=ProviderUsage(100, 20)),
    )
    result, probe, canaries = _run(world, subject, script, manifest=manifest)
    assert result.status == "blocked", result.error
    assert probe.provider_calls == 2, (
        "one provider call per attempt: the adapter retried nothing itself"
    )
    attempts = sorted(_rows(world), key=lambda row: row.attempt)
    assert [(r.attempt, r.state, r.charged_tokens, r.charge_basis) for r in attempts] == [
        (1, "settled", CEILING, "conservative"),
        (2, "settled", 120, "reported"),
    ]
    assert len(_proposals(world)) == 1 and canaries.mutations == []


def test_a_cancelled_step_keeps_its_whole_reservation_open(
    world: Any, subject: AdapterUnderTest
) -> None:
    probe = make_context()
    executor = subject.build((Fault("cancel"), Reply(text="done")), probe)
    with pytest.raises(asyncio.CancelledError):
        execute(_plan(world), INPUT, executor=executor, extra_tools=Canaries().tools())
    assert probe.provider_calls == 1
    (row,) = _rows(world)
    assert (row.state, row.charged_tokens) == ("reserved", None)
    usage = run_token_usage(world, row.workflow_run_id)
    assert (usage.counted_tokens, usage.open_reservations) == (CEILING, 1), (
        "an open reservation counts in full"
    )
    assert _proposals(world) == []


# ------------------------------------------------------------------------------------------------------- handoffs


def test_a_governed_agent_with_a_handoff_never_reaches_the_adapter(
    world: Any, subject: AdapterUnderTest
) -> None:
    manifest = _manifest()
    manifest["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    manifest["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "tools": [],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    script = (Reply(text=None, calls=(Call(PROTECTED, _proposal()),)),)
    result, probe, canaries = _run(world, subject, script, manifest=manifest)
    assert result.status == "error" and "do not follow handoffs" in (result.error or ""), (
        result.error
    )
    assert probe.provider_calls == 0 and result.steps == []
    assert _rows(world) == [] and _proposals(world) == [] and canaries.mutations == []
