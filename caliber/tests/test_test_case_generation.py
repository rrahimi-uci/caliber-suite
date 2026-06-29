"""Tests for engine-backed test-case generation (golden-path roadmap, Wave 5.3).

Generation uses the assistant engine and parses a JSON array from its reply,
falling back to a deterministic set when the engine is unavailable or its reply
isn't parseable (e.g. the fake engine) — so the fake/CI path stays deterministic.
"""

from __future__ import annotations

from caliber.assistant.models import AssistantTurnRequest, AssistantTurnResult
from caliber.assistant.service import AssistantService


class _ReplyEngine:
    """Engine stub returning a fixed reply (or raising)."""

    def __init__(self, reply: str = "", *, raises: bool = False) -> None:
        self._reply = reply
        self._raises = raises
        self.calls: list[AssistantTurnRequest] = []

    def run_turn(
        self, request: AssistantTurnRequest, *, toolset: object | None = None
    ) -> AssistantTurnResult:
        self.calls.append(request)
        if self._raises:
            raise RuntimeError("engine down")
        return AssistantTurnResult(reply=self._reply)


def _svc(engine) -> AssistantService:
    return AssistantService(engine=engine)


_VALID = (
    '[{"input": {"query": "What is the refund window?"}, '
    '"expected": {"behavior": "Cite the policy"}, "tags": ["policy"]}, '
    '{"input": "raw string input", "expected": "be concise", "tags": "notalist"}]'
)


# --------------------------------------------------------------------------- #
# _parse_test_cases
# --------------------------------------------------------------------------- #


def test_parse_valid_array_and_coercions() -> None:
    cases = AssistantService._parse_test_cases(_VALID)
    assert len(cases) == 2
    assert cases[0] == {
        "input": {"query": "What is the refund window?"},
        "expected": {"behavior": "Cite the policy"},
        "tags": ["policy"],
    }
    # string input → {"query": ...}; string expected → {"behavior": ...}; bad tags → default
    assert cases[1]["input"] == {"query": "raw string input"}
    assert cases[1]["expected"] == {"behavior": "be concise"}
    assert cases[1]["tags"] == ["generated"]


def test_parse_tolerates_code_fence_and_prose() -> None:
    reply = 'Here are the cases:\n```json\n[{"input": {"query": "q"}}]\n```\nDone.'
    cases = AssistantService._parse_test_cases(reply)
    assert cases == [{"input": {"query": "q"}, "expected": {}, "tags": ["generated"]}]


def test_parse_rejects_non_json_and_non_list() -> None:
    assert AssistantService._parse_test_cases("here is a draft, no JSON") == []
    assert AssistantService._parse_test_cases("") == []
    assert (
        AssistantService._parse_test_cases('{"input": {"query": "q"}}') == []
    )  # object, not array
    assert AssistantService._parse_test_cases("[not valid json}") == []


def test_parse_recovers_array_with_trailing_footnote() -> None:
    # Maximal first-'['/last-']' slice would include the footnote and fail.
    reply = '[{"input": {"query": "q"}}] (see item [3] for details)'
    assert AssistantService._parse_test_cases(reply) == [
        {"input": {"query": "q"}, "expected": {}, "tags": ["generated"]}
    ]


def test_parse_recovers_array_after_leading_prose_bracket() -> None:
    reply = 'see [the docs]: [{"input": {"query": "q"}}]'
    assert AssistantService._parse_test_cases(reply) == [
        {"input": {"query": "q"}, "expected": {}, "tags": ["generated"]}
    ]


def test_parse_recovers_first_valid_of_multiple_arrays() -> None:
    reply = '[{"input": {"query": "a"}}] and also [{"input": {"query": "b"}}]'
    cases = AssistantService._parse_test_cases(reply)
    assert cases == [{"input": {"query": "a"}, "expected": {}, "tags": ["generated"]}]


def test_parse_caps_at_five() -> None:
    items = ",".join('{"input": {"query": "q' + str(i) + '"}}' for i in range(10))
    cases = AssistantService._parse_test_cases(f"[{items}]")
    assert len(cases) == 5


# --------------------------------------------------------------------------- #
# _generate_test_cases (engine + fallback)
# --------------------------------------------------------------------------- #


def test_generate_uses_engine_output_when_parseable() -> None:
    engine = _ReplyEngine(reply=_VALID)
    cases = _svc(engine)._generate_test_cases("support-bot")
    assert len(cases) == 2
    assert cases[0]["input"] == {"query": "What is the refund window?"}
    # the engine was actually consulted with the prompt name
    assert engine.calls and "support-bot" in engine.calls[0].user_message


def test_generate_falls_back_on_non_json_reply() -> None:
    cases = _svc(_ReplyEngine(reply="Sure, here's a draft."))._generate_test_cases("p")
    assert cases == AssistantService._fallback_test_cases()


def test_generate_falls_back_on_engine_error() -> None:
    cases = _svc(_ReplyEngine(raises=True))._generate_test_cases("p")
    assert cases == AssistantService._fallback_test_cases()


def test_generate_falls_back_on_empty_array() -> None:
    cases = _svc(_ReplyEngine(reply="[]"))._generate_test_cases("p")
    assert cases == AssistantService._fallback_test_cases()


def test_generate_grounds_in_prompt_template() -> None:
    engine = _ReplyEngine(reply=_VALID)
    svc = AssistantService(engine=engine, prompt_fetcher=lambda _n: "SYSTEM: You are a refund bot.")
    svc._generate_test_cases("support-bot")
    msg = engine.calls[0].user_message
    assert "You are a refund bot" in msg
    assert "support-bot" in msg


def test_generate_without_fetcher_is_name_only() -> None:
    engine = _ReplyEngine(reply=_VALID)
    svc = AssistantService(engine=engine)  # no prompt_fetcher
    svc._generate_test_cases("support-bot")
    msg = engine.calls[0].user_message
    assert "prompt template under test" not in msg
    assert "support-bot" in msg


def test_generate_fetcher_error_falls_back_to_name_only() -> None:
    def _boom(_name: str) -> str:
        raise RuntimeError("registry down")

    engine = _ReplyEngine(reply=_VALID)
    svc = AssistantService(engine=engine, prompt_fetcher=_boom)
    svc._generate_test_cases("p")  # must not raise
    assert "prompt template under test" not in engine.calls[0].user_message
