"""What a declaration accepts, and what it refuses at construction.

Refusals happen in the plugin author's own process, where they can be fixed,
rather than in a deployment's server log where they cannot.
"""

from __future__ import annotations

import pytest

from caliber_plugin_sdk import DeclarationError, PluginDeclaration, declare
from caliber_plugin_sdk.reference import RequirementAppender


def test_a_declaration_states_what_it_can_target() -> None:
    declaration = declare(
        "Acme",
        summary="Does a thing.",
        artifact_types=("prompt",),
        factory=RequirementAppender,
    )
    assert declaration.can_target("prompt")
    assert not declaration.can_target("skill")
    assert declaration.artifact_types == frozenset({"prompt"})


def test_extra_keywords_become_metadata_rather_than_being_dropped() -> None:
    declaration = declare(
        "Acme",
        summary="Does a thing.",
        artifact_types=("prompt",),
        factory=RequirementAppender,
        vendor="acme",
    )
    assert declaration.extra == {"vendor": "acme"}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"name": ""}, "needs a name"),
        ({"summary": ""}, "needs a summary"),
        ({"artifact_types": ()}, "could never be selected"),
        ({"artifact_types": ("prompts",)}, "unknown artifact types"),
        ({"factory": "not callable"}, "not callable"),
    ],
    ids=["no-name", "no-summary", "targets-nothing", "typo-in-kind", "bad-factory"],
)
def test_a_malformed_declaration_is_refused_at_construction(
    kwargs: dict[str, object], expected: str
) -> None:
    """Each of these would otherwise fail somewhere far from its cause.

    ``"prompts"`` is the one worth naming: it is a plausible typo that produces
    an optimizer no selection rule can ever pick, and nothing at run time would
    say why.
    """
    fields: dict[str, object] = {
        "name": "Acme",
        "summary": "Does a thing.",
        "artifact_types": frozenset({"prompt"}),
        "factory": RequirementAppender,
    }
    fields.update(kwargs)
    if isinstance(fields["artifact_types"], tuple):
        fields["artifact_types"] = frozenset(fields["artifact_types"])

    with pytest.raises(DeclarationError, match=expected):
        PluginDeclaration(**fields)  # type: ignore[arg-type]


def test_a_summary_is_required_because_an_operator_reads_it_to_decide() -> None:
    """Allowlisting is a trust decision, and an empty summary makes it blind."""
    with pytest.raises(DeclarationError, match="needs a summary"):
        declare("Acme", summary="   ", artifact_types=("prompt",), factory=RequirementAppender)
