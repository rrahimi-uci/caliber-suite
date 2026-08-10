"""The server accepts the plugin SDK's own declaration type.

The two packages ship separately and neither imports the other: a plugin must
not have to depend on the CALIBER server, and the server must not depend on the
plugin SDK. That independence is the design, and it is also exactly the seam
where a contract silently diverges -- each side stays internally consistent while
no longer agreeing.

So this drives the real ``caliber_plugin_sdk`` types through the real server
loader. It found the divergence it was written for: the loader originally
required its own ``OptimizerSpec``, so a plugin written against the plugin SDK's
documented API was rejected with a type error.

Skipped when ``caliber-plugin-sdk`` is not installed; CI installs it so this is a
real gate rather than a decorative one.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber.extensibility import OptimizerSpec, PluginError, optimizer_registry
from caliber.extensibility.entrypoints import _coerce

caliber_plugin_sdk = pytest.importorskip(
    "caliber_plugin_sdk", reason="caliber-plugin-sdk is not installed"
)


class _FakeEntryPoint:
    name = "acme"
    value = "acme:declaration"
    dist = None

    def load(self) -> Any:  # pragma: no cover - not exercised here
        raise AssertionError("not used")


def coerce(declaration: object, *, distribution: str | None = "acme-plugin") -> OptimizerSpec:
    return _coerce(declaration, entry=_FakeEntryPoint(), distribution=distribution)  # type: ignore[arg-type]


def test_the_server_accepts_a_plugin_sdk_declaration_unchanged() -> None:
    """The cross-package contract, driven end to end through both real types."""
    declaration = caliber_plugin_sdk.declare(
        "AcmeSpecificity",
        summary="Makes vague instructions specific.",
        artifact_types=("prompt", "skill"),
        factory=lambda: None,
        requires="acme-llm",
        explicit_only=True,
        vendor="acme",
    )

    spec = coerce(declaration)

    assert spec.name == "AcmeSpecificity"
    assert spec.summary == "Makes vague instructions specific."
    assert spec.artifact_types == frozenset({"prompt", "skill"})
    assert spec.requires == "acme-llm"
    assert spec.explicit_only is True
    assert spec.extra == {"vendor": "acme"}
    # Provenance is the server's to assign, never the plugin's to claim.
    assert spec.source == "plugin"
    assert spec.distribution == "acme-plugin"
    assert spec.experimental is True


def test_the_reference_plugin_registers_through_the_real_loader() -> None:
    """The plugin SDK's worked example must actually work against the server.

    A reference implementation that the server rejected would be worse than none:
    it is the thing authors copy.
    """
    from caliber_plugin_sdk.reference import declaration

    spec = coerce(declaration, distribution="caliber-plugin-sdk")
    assert spec.name == "ReferenceRequirementAppender"
    assert spec.can_target("prompt")
    assert spec.can_target("skill")
    # Declared explicit-only by the plugin, and carried through: a reference
    # implementation must not end up in an automatic production path.
    assert spec.explicit_only

    registry = optimizer_registry()
    registry.reset_for_tests()
    try:
        registry.register(spec)
        assert "ReferenceRequirementAppender" not in registry.selectable("prompt")
        assert "ReferenceRequirementAppender" in registry.names(artifact_type="prompt")
    finally:
        registry.reset_for_tests()


def test_a_declaration_naming_a_builtin_is_refused_by_both_sides() -> None:
    """Defence in depth, and the two errors serve different readers.

    The plugin SDK's conformance suite catches it in the author's test run; the
    server catches it at load time in case a plugin never ran conformance. The
    substitution this prevents -- installing a wheel silently changing what
    ``GEPA`` does for every agent already configured for it -- is invisible in a
    diff, so one check is not enough.
    """
    from caliber_plugin_sdk.conformance import check_declaration

    declaration = caliber_plugin_sdk.declare(
        "GEPA",
        summary="Not really GEPA.",
        artifact_types=("prompt",),
        factory=lambda: None,
    )

    # Author-side.
    assert any("built-in" in problem for problem in check_declaration(declaration))

    # Server-side, even if conformance was never run.
    registry = optimizer_registry()
    registry.reset_for_tests()
    with pytest.raises(PluginError, match="never redefine"):
        registry.register(coerce(declaration))


def test_the_two_packages_agree_on_the_entry_point_group_and_allowlist() -> None:
    """Two string constants in two distributions, and a typo in either is silent.

    A plugin advertising into the wrong group is simply never discovered, and a
    README naming the wrong variable sends an operator to set something that does
    nothing.
    """
    from caliber.extensibility import entrypoints

    assert caliber_plugin_sdk.ENTRY_POINT_GROUP == entrypoints.OPTIMIZER_GROUP
    assert caliber_plugin_sdk.ALLOWLIST_ENV_VAR == entrypoints.ALLOWLIST_ENV_VAR


def test_the_two_packages_agree_on_the_valid_artifact_kinds() -> None:
    """The plugin SDK rejects an unknown kind at declaration time, which is only
    useful if its list matches what the server can actually dispatch."""
    from caliber.extensibility.registry import BUILTIN_OPTIMIZERS

    server_kinds = {kind for spec in BUILTIN_OPTIMIZERS for kind in spec.artifact_types}
    assert server_kinds <= caliber_plugin_sdk.VALID_ARTIFACT_TYPES


def test_the_reserved_name_list_matches_what_the_server_ships() -> None:
    """The plugin SDK duplicates the built-in names rather than importing them.

    Duplicated on purpose -- importing them would make every plugin depend on the
    CALIBER distribution -- but a duplicate can go stale, which would downgrade an
    early warning into a late one. This is the test that keeps it honest.
    """
    from caliber_plugin_sdk.conformance import RESERVED_NAMES

    from caliber.extensibility.registry import BUILTIN_OPTIMIZERS

    assert {spec.name for spec in BUILTIN_OPTIMIZERS} == RESERVED_NAMES


# --- what the loader refuses from untyped third-party code -----------------


def test_a_string_of_artifact_types_is_refused_rather_than_iterated() -> None:
    """``artifact_types="prompt"`` would otherwise register "p", "r", "o", ...

    A plausible mistake, and one that produces an optimizer no job can ever
    select while looking correct in the declaration.
    """

    class Sloppy:
        name = "Sloppy"
        summary = "s"
        artifact_types = "prompt"

    with pytest.raises(PluginError, match="as a string"):
        coerce(Sloppy())


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"summary": "s", "artifact_types": ("prompt",)}, "missing"),
        ({"name": "", "summary": "s", "artifact_types": ("prompt",)}, "unusable name"),
        ({"name": "X", "summary": "s", "artifact_types": 42}, "not iterable"),
    ],
    ids=["no-name-attribute", "empty-name", "uniterable-kinds"],
)
def test_a_malformed_declaration_becomes_a_named_plugin_error(
    attributes: dict[str, Any], expected: str
) -> None:
    """Not an AttributeError from deep inside the registry.

    Everything here crossed a package boundary from code CALIBER did not compile,
    so the failure has to name the plugin and the problem.
    """
    declaration = type("Declared", (), attributes)()
    with pytest.raises(PluginError, match=expected):
        coerce(declaration)
