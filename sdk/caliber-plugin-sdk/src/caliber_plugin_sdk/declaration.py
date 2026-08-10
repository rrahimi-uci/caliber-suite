"""How a plugin declares itself, and what CALIBER does with the declaration.

A plugin advertises one entry point in the ``caliber.optimizers`` group. What it
points at is either a :class:`PluginDeclaration` or a zero-argument callable
returning one — the callable form exists so a plugin can probe for its optional
dependency and declare honestly, rather than registering a capability it cannot
deliver and failing at run time.

Being discovered is not being enabled. CALIBER enables a plugin only when the
deployment names its **distribution** in ``CALIBER_PLUGIN_ALLOWLIST``. That is
deliberate and not a friction to work around: an optimizer authors the artifact
that gets promoted to production, so it is a supply-chain surface, and a
transitive dependency must not be able to acquire that authority by merely being
installed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from caliber_plugin_sdk.contracts import Optimizer

#: Entry-point group to advertise into:
#:
#: .. code-block:: toml
#:
#:     [project.entry-points."caliber.optimizers"]
#:     acme = "acme_caliber:declaration"
ENTRY_POINT_GROUP = "caliber.optimizers"

#: Environment variable a deployment sets to enable a plugin's distribution.
#: Named here so a plugin's own README can point at the right thing.
ALLOWLIST_ENV_VAR = "CALIBER_PLUGIN_ALLOWLIST"

#: Artifact kinds an optimizer may declare. A kind outside this set is rejected
#: at declaration time rather than producing an optimizer nothing can ever
#: select — a typo like ``"prompts"`` would otherwise be invisible.
VALID_ARTIFACT_TYPES = frozenset({"prompt", "skill", "workflow"})


class DeclarationError(ValueError):
    """The declaration is malformed.

    Raised at construction, in the plugin's own process, so the author sees it
    while testing rather than an operator seeing it in a server log.
    """


@dataclass(frozen=True)
class PluginDeclaration:
    """What a plugin claims to be, and the factory that builds it.

    ``name`` becomes the value operators put in an agent's
    ``optimizer_config["type"]`` and that CALIBER stores on every job it runs.
    It is therefore a durable contract, not a label: renaming it orphans the
    rows that reference it, so pick it once.

    A plugin cannot claim a name CALIBER ships. Allowing that would mean
    installing a wheel could change what ``"GEPA"`` does for every agent already
    configured to use it — same name, different author, nothing in any diff. The
    server refuses it; this class refuses it earlier, where the author is looking.
    """

    name: str
    summary: str
    #: Artifact kinds this optimizer can target. Required, and checked: an
    #: optimizer that targets nothing is dead configuration, and one that claims
    #: everything will be handed skill jobs it cannot do anything sensible with.
    artifact_types: frozenset[str]
    #: Builds the optimizer. A factory rather than an instance so CALIBER
    #: controls when construction happens, and so a plugin holding expensive
    #: state does not build it at import time.
    factory: Callable[[], Optimizer]
    #: Optional distribution this optimizer needs installed, when it has one.
    #: Surfaced in ``/capabilities`` so a UI can explain an unavailable option
    #: instead of hiding it.
    requires: str | None = None
    #: True when CALIBER's automatic selection rules should never pick this, so
    #: only an explicit operator override reaches it. The honest setting for an
    #: expensive or experimental optimizer.
    explicit_only: bool = False
    #: Free-form metadata, stored and displayed, never interpreted.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise DeclarationError("a plugin declaration needs a name")
        if not self.summary or not self.summary.strip():
            # Required because this string is what an operator reads when
            # deciding whether to allowlist the distribution. An empty summary
            # makes that decision blind.
            raise DeclarationError(f"optimizer {self.name!r} needs a summary")
        if not self.artifact_types:
            raise DeclarationError(
                f"optimizer {self.name!r} declares no artifact types; it could never be selected"
            )
        unknown = set(self.artifact_types) - VALID_ARTIFACT_TYPES
        if unknown:
            raise DeclarationError(
                f"optimizer {self.name!r} declares unknown artifact types {sorted(unknown)}; "
                f"valid: {sorted(VALID_ARTIFACT_TYPES)}"
            )
        if not callable(self.factory):
            raise DeclarationError(f"optimizer {self.name!r} has a factory that is not callable")

    def can_target(self, artifact_type: str) -> bool:
        return artifact_type in self.artifact_types


def declare(
    name: str,
    *,
    summary: str,
    artifact_types: Iterable[str],
    factory: Callable[[], Optimizer],
    requires: str | None = None,
    explicit_only: bool = False,
    **extra: Any,
) -> PluginDeclaration:
    """Build a declaration, with keyword arguments for everything but the name.

    Keyword-only past ``name`` because a positional call site — ``declare("X",
    "...", ["prompt"], factory)`` — is unreadable at exactly the moment it
    matters, which is an operator or reviewer reading what a plugin claims.
    """
    return PluginDeclaration(
        name=name,
        summary=summary,
        artifact_types=frozenset(artifact_types),
        factory=factory,
        requires=requires,
        explicit_only=explicit_only,
        extra=dict(extra),
    )


__all__ = [
    "ALLOWLIST_ENV_VAR",
    "ENTRY_POINT_GROUP",
    "VALID_ARTIFACT_TYPES",
    "DeclarationError",
    "PluginDeclaration",
    "declare",
]
