"""The optimizer registry: what exists, who provides it, and what it may target.

Before this module, ``generate_candidate`` dispatched on a chain of string
comparisons and the set of supported names lived in three places — the
comparison chain, its error message, and a route-level tuple. They drifted, and
nothing could tell you what was available without reading provider source.

A registry replaces the chain with data. The entry describes the optimizer well
enough to answer three questions that the chain could not:

* **May it run here?** ``artifact_types`` says whether it can target prompts,
  skills, or both. A skill job routed to a prompt-only optimizer is a
  configuration error, and it should be named as one rather than producing a
  candidate that silently ignores the skill's ``allowed_tools``.
* **Will it run here?** ``requires`` names the optional distribution an
  optimizer needs. That is what lets the UI say "GEPA needs ``gepa`` installed"
  instead of the operator discovering it from a fallback note after a run.
* **Who provides it?** ``source`` separates the built-ins from plugins. An
  operator reviewing what may write to production needs that distinction, and a
  registry that flattened it would be actively misleading.

Registration is explicit and total: nothing dispatches on a name this registry
does not carry.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger("caliber.extensibility.registry")

#: Artifact kinds an optimizer can target. Mirrors
#: ``caliber_refinement_jobs.artifact_type``.
ArtifactType = Literal["prompt", "skill", "workflow"]

#: Where an entry came from. ``builtin`` ships with CALIBER; ``plugin`` was
#: loaded from a third-party distribution's entry point.
OptimizerSource = Literal["builtin", "plugin"]


class PluginError(Exception):
    """A plugin could not be loaded, or tried to do something it may not.

    Distinct from a configuration error: the deployment asked for this plugin,
    so the failure is worth surfacing rather than falling back past.
    """


@dataclass(frozen=True)
class OptimizerSpec:
    """One registered optimizer.

    ``name`` is the value that appears in ``job.optimizer_type`` and in an
    agent's ``optimizer_config["type"]``, so it is a stored contract: renaming
    an entry orphans the rows that reference it.
    """

    name: str
    summary: str
    #: Artifact kinds this optimizer can target. Empty is rejected at
    #: registration — an optimizer that can target nothing is dead config, and
    #: silently accepting it would hide the mistake until a job failed.
    artifact_types: frozenset[str]
    source: OptimizerSource = "builtin"
    #: Importable distribution this optimizer needs, when it is optional.
    #: ``None`` means no extra dependency.
    requires: str | None = None
    #: Distribution that registered a plugin entry. ``None`` for built-ins.
    distribution: str | None = None
    #: True when no automatic selection rule chooses this optimizer, so it can
    #: only be reached by an explicit override. DSPyMIPRO is the standing
    #: example: implemented, and deliberately not automatic.
    explicit_only: bool = False
    #: Names of experimental status. Plugins are experimental until the plugin
    #: contract survives a release; the field says so per entry rather than in
    #: prose that can go stale.
    experimental: bool = False
    extra: dict[str, object] = field(default_factory=dict)

    def can_target(self, artifact_type: str) -> bool:
        return artifact_type in self.artifact_types


#: The optimizers CALIBER itself implements. This is the whole set — the
#: dispatch in ``caliber.llm.openai_agents`` consults the registry, so an
#: optimizer missing from here is unreachable no matter what the provider
#: contains.
BUILTIN_OPTIMIZERS: tuple[OptimizerSpec, ...] = (
    OptimizerSpec(
        name="MetaPrompt",
        summary="Single-pass rewrite of the prompt from the diagnosis.",
        artifact_types=frozenset({"prompt"}),
    ),
    OptimizerSpec(
        name="SkillMetaPrompt",
        summary="Single-pass rewrite of a skill's content and metadata.",
        artifact_types=frozenset({"skill"}),
    ),
    OptimizerSpec(
        name="GEPA",
        summary="Multi-generation genetic-Pareto search over prompt variants.",
        artifact_types=frozenset({"prompt", "skill"}),
        requires="gepa",
    ),
    OptimizerSpec(
        name="DSPyBootstrapFewShot",
        summary="Bootstraps few-shot demonstrations from the agent's eval dataset.",
        artifact_types=frozenset({"prompt"}),
        requires="dspy",
    ),
    OptimizerSpec(
        name="DSPyMIPRO",
        summary="DSPy MIPROv2 — rewrites the instruction and selects demonstrations.",
        artifact_types=frozenset({"prompt"}),
        requires="dspy",
        explicit_only=True,
    ),
)


class OptimizerRegistry:
    """A name-to-spec mapping with rules about who may claim a name.

    Guarded by a lock because plugin loading happens once, lazily, on whichever
    request or worker thread arrives first, and two threads racing to populate
    the same registry must not produce a half-loaded one.
    """

    def __init__(self, specs: tuple[OptimizerSpec, ...] = BUILTIN_OPTIMIZERS) -> None:
        self._lock = threading.RLock()
        self._specs: dict[str, OptimizerSpec] = {}
        self._plugins_loaded = False
        #: Load failures, kept rather than raised. One broken plugin must not
        #: make CALIBER unstartable, but the failure has to be visible: this is
        #: what ``/capabilities`` reports so an operator sees "requested and
        #: failed" rather than silence.
        self.load_errors: dict[str, str] = {}
        for spec in specs:
            self.register(spec)

    # -- registration ------------------------------------------------------

    def register(self, spec: OptimizerSpec) -> None:
        """Add an entry, refusing the two ways a name can be wrong.

        A plugin may not shadow a built-in. Allowing it would mean installing a
        wheel could silently change what ``"GEPA"`` does for every agent already
        configured to use it — the same name, a different author, no diff. That
        is a substitution attack with a plausible cover story, so it is an error
        even when the plugin is allowlisted.

        Two plugins claiming one name is likewise refused: whichever loaded
        first would win by entry-point iteration order, which is not a decision
        anybody made.
        """
        if not spec.name:
            raise PluginError("an optimizer must have a name")
        if not spec.artifact_types:
            raise PluginError(f"optimizer {spec.name!r} declares no artifact types it can target")

        with self._lock:
            existing = self._specs.get(spec.name)
            if existing is None:
                self._specs[spec.name] = spec
                return
            if existing.source == "builtin" and spec.source == "plugin":
                raise PluginError(
                    f"plugin {spec.distribution or '<unknown>'} tried to replace built-in "
                    f"optimizer {spec.name!r}; plugins may add optimizers but never "
                    "redefine one CALIBER ships"
                )
            if existing.source == "plugin" and spec.source == "plugin":
                raise PluginError(
                    f"optimizer name {spec.name!r} is claimed by both "
                    f"{existing.distribution or '<unknown>'} and "
                    f"{spec.distribution or '<unknown>'}; rename one"
                )
            # Re-registering an identical built-in is a no-op rather than an
            # error, so importing this module twice under different names (which
            # happens in test collection) does not fail.
            if existing != spec:
                raise PluginError(f"optimizer {spec.name!r} is already registered differently")

    # -- lookup ------------------------------------------------------------

    def get(self, name: str) -> OptimizerSpec:
        """Return the spec, or raise with the available names.

        The error names what *is* available, because "not implemented" without
        that list leaves the reader to go find it in source — which is exactly
        what the old dispatch chain's message did.
        """
        self.load_plugins()
        with self._lock:
            spec = self._specs.get(name)
            if spec is not None:
                return spec
            available = sorted(self._specs)
        raise PluginError(f"optimizer {name!r} is not registered; available: {available}")

    def has(self, name: str) -> bool:
        self.load_plugins()
        with self._lock:
            return name in self._specs

    def names(self, *, artifact_type: str | None = None) -> list[str]:
        """Registered names, optionally narrowed to what can target one kind."""
        self.load_plugins()
        with self._lock:
            specs = list(self._specs.values())
        if artifact_type is not None:
            specs = [spec for spec in specs if spec.can_target(artifact_type)]
        return sorted(spec.name for spec in specs)

    def selectable(self, artifact_type: str) -> list[str]:
        """Names an automatic selection rule is allowed to choose.

        Excludes ``explicit_only`` entries. A caller offering choices to an
        operator wants :meth:`names`; a caller deciding on its own wants this.
        """
        self.load_plugins()
        with self._lock:
            specs = list(self._specs.values())
        return sorted(
            spec.name for spec in specs if spec.can_target(artifact_type) and not spec.explicit_only
        )

    def __iter__(self) -> Iterator[OptimizerSpec]:
        self.load_plugins()
        with self._lock:
            return iter(sorted(self._specs.values(), key=lambda spec: spec.name))

    def __len__(self) -> int:
        self.load_plugins()
        with self._lock:
            return len(self._specs)

    # -- plugins -----------------------------------------------------------

    def load_plugins(self) -> None:
        """Load allowlisted third-party optimizers, exactly once.

        Deferred rather than done at import time so that importing CALIBER
        never executes third-party code as a side effect — an import-time load
        would run plugin code during ``alembic`` migrations and CLI ``--help``,
        neither of which has any business doing so.
        """
        with self._lock:
            if self._plugins_loaded:
                return
            self._plugins_loaded = True

        # Imported here to keep the module import graph acyclic: entrypoints
        # needs the registry types.
        from caliber.extensibility.entrypoints import (  # noqa: PLC0415
            discover_optimizer_plugins,
        )

        for spec, error in discover_optimizer_plugins():
            if error is not None:
                self.load_errors[error[0]] = error[1]
                logger.warning("optimizer plugin %s failed to load: %s", error[0], error[1])
                continue
            if spec is None:
                continue
            try:
                self.register(spec)
            except PluginError as exc:
                self.load_errors[spec.distribution or spec.name] = str(exc)
                logger.warning("optimizer plugin %s rejected: %s", spec.name, exc)

    def reset_for_tests(self) -> None:
        """Restore the built-in-only state, for tests that install fake plugins."""
        with self._lock:
            self._specs = {}
            self._plugins_loaded = False
            self.load_errors = {}
            for spec in BUILTIN_OPTIMIZERS:
                self._specs[spec.name] = spec


_REGISTRY = OptimizerRegistry()


def optimizer_registry() -> OptimizerRegistry:
    """The process-wide registry.

    A single instance rather than one per caller: the plugin allowlist is a
    deployment-level decision, and two registries could disagree about what is
    installed.
    """
    return _REGISTRY


__all__ = [
    "BUILTIN_OPTIMIZERS",
    "ArtifactType",
    "OptimizerRegistry",
    "OptimizerSource",
    "OptimizerSpec",
    "PluginError",
    "optimizer_registry",
]
