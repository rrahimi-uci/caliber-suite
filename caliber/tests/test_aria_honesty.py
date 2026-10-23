"""Aria says whether it can answer, names the missing key, and refuses empty plans (alpha gap P1-11).

On a default install ``assistant_engine=auto`` resolves to OpenAI with no key. Every turn
returned 201 with ``An error occurred: OpenAIError``, "Ask Aria" was offered on every page,
and a goal the planner could not map produced a zero-step plan that approved and executed
to ``completed``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant.capabilities import registered_capabilities
from caliber.assistant.models import AssistantTurnResult
from caliber.assistant.plans import HeuristicPlanner
from caliber.assistant.service import (
    AssistantService,
    AssistantUnavailableError,
    assistant_failure_message,
)
from caliber.db.models import CaliberAriaPlan
from caliber.routes.aria_plans import APPROVE_PATH, EXECUTE_PATH, LIST_PATH

CONFIG = "/ajax-api/2.0/mlflow/caliber/assistant/config"


def _use_engine(client: TestClient, engine: str) -> None:
    client.app.state._assistant_overrides = {"engine": engine}  # type: ignore[attr-defined]


# --- configured / reason -----------------------------------------------------------------


def test_openai_without_a_key_is_not_configured_and_names_the_variable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _use_engine(client, "openai")

    data = client.get(CONFIG).json()["data"]

    assert data["configured"] is False
    assert "OPENAI_API_KEY" in data["reason"]
    assert "restart" in data["reason"]


def test_auto_without_any_key_resolves_to_openai_and_is_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _use_engine(client, "auto")

    data = client.get(CONFIG).json()["data"]

    assert data["engine"] == "openai"
    assert data["configured"] is False


def test_a_configured_key_makes_aria_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _use_engine(client, "openai")

    data = client.get(CONFIG).json()["data"]

    assert data["configured"] is True
    assert data["reason"] is None


def test_anthropic_needs_its_own_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _use_engine(client, "anthropic")

    data = client.get(CONFIG).json()["data"]

    assert data["configured"] is False
    assert "ANTHROPIC_API_KEY" in data["reason"]


@pytest.mark.parametrize("engine", ["fake", "ollama"])
def test_keyless_engines_report_configured(client: TestClient, engine: str) -> None:
    _use_engine(client, engine)

    data = client.get(CONFIG).json()["data"]

    assert data["configured"] is True
    assert data["reason"] is None


def test_a_disabled_assistant_says_so(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"assistant_enabled": False}
    )

    data = client.get(CONFIG).json()["data"]

    assert data["configured"] is False
    assert "CALIBER_ASSISTANT_ENABLED" in data["reason"]


def test_a_custom_key_variable_counts_for_openai(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEAM_OPENAI_KEY", "sk-team")
    client.app.state.config = client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"llm_api_key_env": "TEAM_OPENAI_KEY"}
    )
    _use_engine(client, "openai")

    assert client.get(CONFIG).json()["data"]["configured"] is True


# --- failure message ---------------------------------------------------------------------


class OpenAIError(Exception):
    """Stand-in with the SDK's class name; the real one needs the openai package."""


class AuthenticationError(Exception):
    pass


def test_a_missing_openai_key_message_names_the_variable() -> None:
    exc = OpenAIError(
        "The api_key client option must be set either by passing api_key to the client or by "
        "setting the OPENAI_API_KEY environment variable"
    )
    message = assistant_failure_message(exc)
    assert "OPENAI_API_KEY is not set" in message
    assert "restart" in message
    assert "OpenAIError" not in message


def test_a_missing_anthropic_key_message_names_its_variable() -> None:
    message = assistant_failure_message(RuntimeError("ANTHROPIC_API_KEY is required"))
    assert "ANTHROPIC_API_KEY is not set" in message


def test_a_generic_key_complaint_points_at_both_variables() -> None:
    message = assistant_failure_message(ValueError("Missing API key for provider"))
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in message


def test_a_rejected_key_says_so() -> None:
    message = assistant_failure_message(AuthenticationError("401"))
    assert "rejected the API key" in message


def test_other_failures_keep_only_the_exception_class() -> None:
    secret_url = "https://user:hunter2@llm.internal/v1"
    message = assistant_failure_message(RuntimeError(f"boom contacting {secret_url}"))
    assert message == "An error occurred: RuntimeError"
    assert "hunter2" not in message


# --- "Describe it" (prompt drafting) -----------------------------------------------------


class _NoKeyEngine:
    """An engine that cannot answer at all, the way OpenAI's client fails with no key."""

    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        raise OpenAIError(
            "The api_key client option must be set either by passing api_key to the client or by "
            "setting the OPENAI_API_KEY environment variable"
        )


class _AnswersOnceThenFails:
    """Answers the first turn (a clarifying question, no draft), then goes down."""

    def __init__(self) -> None:
        self.turns = 0

    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        self.turns += 1
        if self.turns > 1:
            raise RuntimeError("provider went away")
        return AssistantTurnResult(reply="Which tickets do you mean?")


def test_drafting_without_a_key_raises_instead_of_returning_an_empty_draft() -> None:
    """The builder used to seed the user's own text as if Aria had drafted it."""
    svc = AssistantService(engine=_NoKeyEngine())  # type: ignore[arg-type]

    with pytest.raises(AssistantUnavailableError) as raised:
        svc.draft_prompt_from_description("Summarize support tickets.")

    assert "OPENAI_API_KEY is not set" in str(raised.value)
    assert "restart" in str(raised.value)
    assert "OpenAIError" not in str(raised.value)


def test_drafting_stops_after_the_first_failed_turn() -> None:
    """No five-turn retry loop against an engine that cannot answer."""

    class _CountingNoKey(_NoKeyEngine):
        turns = 0

        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            type(self).turns += 1
            return super().run_turn(request, toolset=toolset)

    svc = AssistantService(engine=_CountingNoKey())  # type: ignore[arg-type]

    with pytest.raises(AssistantUnavailableError):
        svc.draft_prompt_from_description("Summarize support tickets.", max_turns=5)

    assert _CountingNoKey.turns == 1


def test_an_engine_that_answered_then_failed_keeps_the_builders_fallback() -> None:
    """Only "never answered" is an outage; a partial conversation still seeds the manual flow."""
    svc = AssistantService(engine=_AnswersOnceThenFails())  # type: ignore[arg-type]

    result = svc.draft_prompt_from_description("Summarize support tickets.")

    assert result["template"] == ""
    assert result["reply"] == "Which tickets do you mean?"


def test_prompt_draft_route_answers_503_with_the_actionable_reason(client: TestClient) -> None:
    client.app.state.assistant_service = AssistantService(engine=_NoKeyEngine())  # type: ignore[arg-type,attr-defined]

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/assistant/prompt-draft",
        json={"description": "Summarize support tickets."},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "OPENAI_API_KEY is not set" in detail
    assert "restart" in detail


# --- plans -------------------------------------------------------------------------------


def test_the_planner_maps_test_set_to_the_dataset_capability() -> None:
    steps = HeuristicPlanner().plan(
        "Create a test set for the support prompt", capabilities=registered_capabilities()
    )
    assert [s.capability_key for s in steps] == ["eval_dataset.create"]


@pytest.mark.parametrize("phrase", ["test set", "test-set", "testset", "eval dataset"])
def test_every_test_set_spelling_plans_a_step(phrase: str) -> None:
    steps = HeuristicPlanner().plan(
        f"please create a {phrase}", capabilities=registered_capabilities()
    )
    assert "eval_dataset.create" in {s.capability_key for s in steps}


def test_an_unmapped_goal_is_flagged_at_creation(client: TestClient) -> None:
    created = client.post(LIST_PATH, json={"goal": "write me a poem"})

    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert body["plan"]["step_count"] == 0
    assert any("No platform steps matched" in w for w in body["warnings"])


def test_a_mapped_goal_has_no_warning(client: TestClient) -> None:
    body = client.post(LIST_PATH, json={"goal": "create a judge"}).json()["data"]
    assert body["plan"]["step_count"] >= 1
    assert "warnings" not in body


def test_a_zero_step_plan_cannot_be_approved_or_completed(
    client: TestClient, db_session: Session
) -> None:
    plan_id = client.post(LIST_PATH, json={"goal": "write me a poem"}).json()["data"]["plan"][
        "plan_id"
    ]

    approved = client.post(APPROVE_PATH.replace("{plan_id}", plan_id))
    assert approved.status_code == 409
    assert "no steps" in approved.json()["detail"]

    executed = client.post(EXECUTE_PATH.replace("{plan_id}", plan_id))
    assert executed.status_code == 409

    db_session.expire_all()
    plan = db_session.get(CaliberAriaPlan, plan_id)
    assert plan is not None
    assert plan.status == "draft"


def test_a_zero_step_plan_already_approved_still_cannot_execute(
    client: TestClient, db_session: Session
) -> None:
    """A plan stored before the approve check existed must not run to ``completed``."""
    plan_id = client.post(LIST_PATH, json={"goal": "write me a poem"}).json()["data"]["plan"][
        "plan_id"
    ]
    plan = db_session.get(CaliberAriaPlan, plan_id)
    assert plan is not None
    plan.status = "approved"
    db_session.commit()

    executed = client.post(EXECUTE_PATH.replace("{plan_id}", plan_id))

    assert executed.status_code == 409
    assert "no steps" in executed.json()["detail"]
    db_session.expire_all()
    refreshed = db_session.get(CaliberAriaPlan, plan_id)
    assert refreshed is not None
    assert refreshed.status == "approved"


def test_a_plan_with_steps_still_approves(client: TestClient) -> None:
    plan_id = client.post(LIST_PATH, json={"goal": "create a judge"}).json()["data"]["plan"][
        "plan_id"
    ]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200


# --- test-case generation -----------------------------------------------------------------
#
# A canned refund-policy test set silently substituted for the user's own prompt, and labelled
# "generated", is a fake that looks real. Only the explicit fake engine may return placeholders.


class _ErroringEngine:
    """A real-looking engine that reports a failure the way the OpenAI/Anthropic engines do."""

    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        return AssistantTurnResult(reply="", error="OPENAI_API_KEY is required to call the model")


class _ProseEngine:
    """A real engine that answers, but not with a JSON array."""

    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        return AssistantTurnResult(reply="Sure! Here are some thoughts on testing that prompt.")


class _CasesEngine:
    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        return AssistantTurnResult(
            reply='[{"input": {"query": "Where is my order?"}, '
            '"expected": {"behavior": "Look up the order."}, "tags": ["orders"]}]'
        )


def test_a_real_engine_that_cannot_answer_does_not_get_canned_cases() -> None:
    svc = AssistantService(engine=_NoKeyEngine())  # type: ignore[arg-type]
    with pytest.raises(AssistantUnavailableError) as raised:
        svc._generate_test_cases("support-agent")
    assert "OPENAI_API_KEY is not set" in str(raised.value)


def test_a_real_engine_that_reports_an_error_does_not_get_canned_cases() -> None:
    svc = AssistantService(engine=_ErroringEngine())  # type: ignore[arg-type]
    with pytest.raises(AssistantUnavailableError, match="OPENAI_API_KEY is not set"):
        svc._generate_test_cases("support-agent")


def test_a_real_engine_that_answers_in_prose_is_an_error_not_a_refund_policy_test_set() -> None:
    svc = AssistantService(engine=_ProseEngine())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="did not return a JSON array"):
        svc._generate_test_cases("support-agent")


def test_a_real_engine_that_returns_cases_has_them_passed_through() -> None:
    svc = AssistantService(engine=_CasesEngine())  # type: ignore[arg-type]
    cases = svc._generate_test_cases("support-agent")
    assert [c["input"]["query"] for c in cases] == ["Where is my order?"]
    assert cases[0]["tags"] == ["orders"]


def test_the_fake_engine_still_gets_deterministic_cases_and_says_they_are_simulated() -> None:
    from caliber.assistant.fake import FakeAssistantEngine

    svc = AssistantService(engine=FakeAssistantEngine())
    assert svc._engine_is_simulated() is True
    assert len(svc._generate_test_cases("support-agent")) >= 3  # the deterministic fallback
    assert AssistantService(engine=_CasesEngine())._engine_is_simulated() is False  # type: ignore[arg-type]
