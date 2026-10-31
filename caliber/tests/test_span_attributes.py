"""B11.3a (#737): the causal-link span attributes are an allow-list of trusted, bounded identifiers.

These tests pin the helper's own contract (what is emitted, what is dropped, what never reaches a span) and
how ``run_tags`` and the trace-access allow-list consume it.  The behavior of the governed effect spans is in
``test_workflow_run_trace_linkage.py`` and the reference-profile scenario.
"""

from __future__ import annotations

import pytest

from caliber.audit import configure_redactor
from caliber.effects.dispatcher import EffectRefused
from caliber.effects.records import ProposalError
from caliber.observability.mlflow_tracing import Tracer, set_tracer
from caliber.observability.span_attributes import (
    CAUSAL_ATTRIBUTE_KEYS,
    MAX_ID_LENGTH,
    REFUSAL_CODE_KEY,
    RESERVED_SPAN_KEYS,
    causal_attributes,
    governed_span,
    without_reserved_keys,
)
from caliber.observability.trace_access import redact_trace_detail
from caliber.profile import SAFE_SPAN_ATTRIBUTES
from caliber.redaction import build_redactor
from caliber.workflows.approval_policy import ApprovalCommitRefused
from caliber.workflows.runtime import CaliberRunContext, run_tags
from tests.test_mlflow_tracing import FakeMlflow

PROPOSAL = "prop-0a1b2c3d4e5f60718293a4b5c6d7e8f9"
DECISION = "pd_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
APPROVAL = "RAP-9b1d4e0a"
EFFECT = "rcpt-00112233445566778899aabbccddeeff"
ALL_FOUR = {
    "caliber.proposal_id": PROPOSAL,
    "caliber.decision_id": DECISION,
    "caliber.approval_id": APPROVAL,
    "caliber.effect_id": EFFECT,
}


@pytest.fixture(autouse=True)
def _redactor_and_tracer():
    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))
    set_tracer(None)


def _ctx(**overrides: object) -> CaliberRunContext:
    fields: dict[str, object] = {
        "workflow_id": "wf",
        "workflow_version": "1",
        "entry_node_id": "start",
    }
    fields.update(overrides)
    return CaliberRunContext(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- causal_attributes


def test_exactly_the_four_causal_keys_are_emitted_in_the_documented_namespace() -> None:
    assert dict(CAUSAL_ATTRIBUTE_KEYS) == {
        "proposal_id": "caliber.proposal_id",
        "decision_id": "caliber.decision_id",
        "approval_id": "caliber.approval_id",
        "effect_id": "caliber.effect_id",
    }
    assert (
        causal_attributes(
            proposal_id=PROPOSAL, decision_id=DECISION, approval_id=APPROVAL, effect_id=EFFECT
        )
        == ALL_FOUR
    )


def test_an_unknown_id_is_omitted_not_blanked() -> None:
    assert causal_attributes() == {}
    assert causal_attributes(proposal_id=PROPOSAL) == {"caliber.proposal_id": PROPOSAL}


def test_the_longest_allowed_identifier_is_kept_and_one_more_is_dropped() -> None:
    longest = "p" * MAX_ID_LENGTH
    assert causal_attributes(proposal_id=longest) == {"caliber.proposal_id": longest}
    # Dropped whole: a truncated id would name a different row.
    assert causal_attributes(proposal_id=longest + "p") == {}


class _StrSubclass(str):
    pass


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "prop 1",
        "prop-1\n",
        "\nprop-1",
        "prop-1\x00",
        "9prop",
        "123456789",
        "123-45-6789",
        "-prop",
        "jane@example.com",
        "prop/../etc",
        "prop-é",
        "prop-١٢",
        "https://example.test/x",
        "{'a': 1}",
        7,
        b"prop-1",
        ["prop-1"],
        _StrSubclass("prop-1"),
    ],
)
def test_a_value_that_is_not_an_opaque_identifier_is_dropped(value: object) -> None:
    assert causal_attributes(proposal_id=value, decision_id=value) == {}


def test_an_unpaired_surrogate_is_dropped() -> None:
    # Not a parametrize id: a lone surrogate in a string parameter aborts the Allure report.
    assert causal_attributes(proposal_id="pr" + chr(0xD800) + "op") == {}


def test_one_bad_value_does_not_hide_the_good_ones() -> None:
    assert causal_attributes(proposal_id=PROPOSAL, approval_id="bad value", effect_id=EFFECT) == {
        "caliber.proposal_id": PROPOSAL,
        "caliber.effect_id": EFFECT,
    }


def test_the_helper_cannot_carry_a_payload_key() -> None:
    with pytest.raises(TypeError):
        causal_attributes(tool_arguments="x")  # type: ignore[call-arg]


# ----------------------------------------------------------------------------- run_tags


def test_run_tags_emit_the_four_ids_when_given_and_nothing_when_not() -> None:
    ctx = _ctx()
    plain = run_tags(ctx, node_id="hook")
    assert not (set(plain) & RESERVED_SPAN_KEYS)
    tagged = run_tags(
        ctx,
        node_id="hook",
        proposal_id=PROPOSAL,
        decision_id=DECISION,
        approval_id=APPROVAL,
        effect_id=EFFECT,
    )
    assert tagged == {**plain, **ALL_FOUR}


def test_run_tags_drop_a_non_identifier_instead_of_emitting_it() -> None:
    tags = run_tags(_ctx(), proposal_id="ssn 123-45-6789", effect_id=EFFECT)
    assert "caliber.proposal_id" not in tags
    assert tags["caliber.effect_id"] == EFFECT


def test_a_manifest_cannot_forge_a_causal_link_through_trace_group_tags() -> None:
    forged = dict.fromkeys(RESERVED_SPAN_KEYS, "prop-forged")
    ctx = _ctx(trace_group_tags={**forged, "team": "ops"})

    without_ids = run_tags(ctx)
    assert not (set(without_ids) & RESERVED_SPAN_KEYS)
    assert without_ids["team"] == "ops"

    with_id = run_tags(ctx, proposal_id=PROPOSAL)
    assert with_id["caliber.proposal_id"] == PROPOSAL
    assert not ((set(with_id) & RESERVED_SPAN_KEYS) - {"caliber.proposal_id"})


def test_without_reserved_keys_keeps_every_other_tag() -> None:
    tags = {"team": "ops", "caliber.node_id": "n", REFUSAL_CODE_KEY: "x", **ALL_FOUR}
    assert without_reserved_keys(tags) == {"team": "ops", "caliber.node_id": "n"}


# ----------------------------------------------------------------------------- governed_span


def test_governed_span_is_inert_without_a_tracer_and_still_returns_the_body_value() -> None:
    with governed_span("effect.x", proposal_id=PROPOSAL) as span:
        span.bind(effect_id=EFFECT)
        assert span.attributes["caliber.effect_id"] == EFFECT
    assert span.attributes["caliber.proposal_id"] == PROPOSAL


def test_governed_span_sends_only_validated_ids_to_the_tracer() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    with governed_span(
        "effect.x", span_type="TOOL", proposal_id=PROPOSAL, approval_id="no way!"
    ) as s:
        s.bind(decision_id=DECISION, effect_id="1-bad")
    (recorded,) = fake.spans
    assert recorded.name == "effect.x"
    assert recorded.span_type == "TOOL"
    assert recorded.attributes["caliber.proposal_id"] == PROPOSAL
    assert recorded.attributes["caliber.decision_id"] == DECISION
    assert "caliber.approval_id" not in recorded.attributes
    assert "caliber.effect_id" not in recorded.attributes
    assert recorded.attributes["caliber.status"] == "completed"
    assert fake.span_exits == [("effect.x", None)]


class _ForeignError(RuntimeError):
    """What a driver or client library raises: it can carry a ``code`` that is not ours."""

    def __init__(self, code: object, message: str) -> None:
        super().__init__(message)
        self.code = code


def _platform_refusals(secret: str) -> list[BaseException]:
    return [
        ProposalError("stale_version", secret),
        EffectRefused("stale_version", secret),
        ApprovalCommitRefused("stale_version", secret),
    ]


@pytest.mark.parametrize("index", [0, 1, 2], ids=["proposal-error", "effect-refused", "approval"])
def test_a_platform_refusal_is_recorded_by_code_and_type_only_and_reraised_unchanged(
    index: int,
) -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    secret = "ssn 123-45-6789 token=abc123 jane@example.com"
    raised = _platform_refusals(secret)[index]

    with pytest.raises(type(raised)) as caught:
        with governed_span("effect.x", proposal_id=PROPOSAL):
            raise raised

    assert caught.value is raised
    (recorded,) = fake.spans
    assert recorded.attributes[REFUSAL_CODE_KEY] == "stale_version"
    assert recorded.attributes["caliber.error_type"] == type(raised).__name__
    assert recorded.attributes["caliber.status"] == "failed"
    assert recorded.attributes["caliber.error"] == "stale_version"
    assert secret not in repr(recorded.attributes)
    assert "abc123" not in repr(recorded.attributes)
    # The tracer never saw an exception, so MLflow cannot record its message on the span's exception event.
    assert fake.span_exits == [("effect.x", None)]


def test_a_foreign_exception_with_a_matching_code_is_recorded_by_type_only() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    with pytest.raises(_ForeignError):
        with governed_span("effect.x"):
            raise _ForeignError("stale_version", "driver says: secret")
    (recorded,) = fake.spans
    assert REFUSAL_CODE_KEY not in recorded.attributes
    assert recorded.attributes["caliber.error_type"] == "_ForeignError"
    assert recorded.attributes["caliber.error"] == "_ForeignError"
    assert "stale_version" not in repr(recorded.attributes)
    assert "secret" not in repr(recorded.attributes)


@pytest.mark.parametrize(
    "code",
    ["Stale", "has space", "x" * 65, "9lives", "", "ssn-123-45-6789", "a@b.co"],
)
def test_an_unstable_platform_code_is_not_copied_to_the_span(code: str) -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    with pytest.raises(ProposalError):
        with governed_span("effect.x"):
            raise ProposalError(code, "boom")
    (recorded,) = fake.spans
    assert REFUSAL_CODE_KEY not in recorded.attributes
    assert recorded.attributes["caliber.error"] == "ProposalError"


class _RaisingRedactor:
    def redact_value(self, _value: object) -> object:
        raise RuntimeError("redactor down")


class _RaisingSpan:
    name = "raising"

    def set_attribute(self, _key: str, _value: object) -> None:
        raise RuntimeError("tracer down")

    def mark_error(self, _message: object = None) -> None:
        raise RuntimeError("tracer down")


def test_a_failing_redactor_drops_the_attribute_and_never_fails_the_body() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    configure_redactor(_RaisingRedactor())  # type: ignore[arg-type]
    with governed_span("effect.x", proposal_id=PROPOSAL) as span:
        span.bind(effect_id=EFFECT)
    (recorded,) = fake.spans
    assert not [key for key in recorded.attributes if key.startswith("caliber.")]
    assert span.attributes == {}


def test_a_failing_span_cannot_replace_the_exception_it_records() -> None:
    from caliber.observability.span_attributes import GovernedSpan

    handle = GovernedSpan(_RaisingSpan())  # type: ignore[arg-type]
    handle.bind(proposal_id=PROPOSAL)  # swallowed
    handle.record_failure(ProposalError("stale_version", "x"))  # swallowed


def test_an_exception_without_a_code_is_recorded_by_type_only() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    with pytest.raises(KeyError):
        with governed_span("effect.x"):
            raise KeyError("prop-secret-lookup")
    (recorded,) = fake.spans
    assert recorded.attributes["caliber.error_type"] == "KeyError"
    assert "prop-secret-lookup" not in repr(recorded.attributes)


def test_a_non_exception_base_error_passes_through_without_a_recorded_code() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    with pytest.raises(KeyboardInterrupt):
        with governed_span("effect.x"):
            raise KeyboardInterrupt
    (recorded,) = fake.spans
    assert REFUSAL_CODE_KEY not in recorded.attributes
    assert fake.span_exits == [("effect.x", KeyboardInterrupt)]


# ------------------------------------------------------------ the trace-access allow-list


def test_the_ids_are_payload_free_so_a_caller_without_payload_read_still_sees_them() -> None:
    for key in (*ALL_FOUR, REFUSAL_CODE_KEY):
        assert key in SAFE_SPAN_ATTRIBUTES
    detail = {
        "trace_id": "tr-1",
        "spans": [
            {
                "name": "effect.dispatch",
                "attributes": {
                    **ALL_FOUR,
                    REFUSAL_CODE_KEY: "stale_version",
                    "caliber.error": "stale_version",
                    "caliber.tool.output": "payload",
                },
            }
        ],
    }
    seen = redact_trace_detail(detail, allowed=False)["spans"][0]["attributes"]
    for key, value in {**ALL_FOUR, REFUSAL_CODE_KEY: "stale_version"}.items():
        assert seen[key] == value
    # Fail-closed for everything not on the list, including a message-like field.
    assert seen["caliber.error"] == "[redacted:attributes.caliber.error]"
    assert seen["caliber.tool.output"] == "[redacted:attributes.caliber.tool.output]"
