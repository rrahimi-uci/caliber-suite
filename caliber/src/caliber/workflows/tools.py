"""Registered-tool resolution and binding (plan §14.4, §18.1).

Workflow designers pick *registered* tools by reference; they never write tool
code. This module is the seam between a manifest's ``registry_ref`` +
``version_constraint`` and an actual callable.

Two layers:

* **Resolution** (metadata only): given a ``registry_ref`` and a
  ``version_constraint``, return the best-matching :class:`ToolRegistryEntry`.
  Validation uses this — it must not import the Agents SDK or load arbitrary
  modules.
* **Binding** (callable): given a resolved entry, import its module and wrap the
  callable as an Agents SDK ``function_tool``. Compile/runtime use this.

Version resolution follows plan §14.4 / §19.12: among entries sharing a family
name, keep those satisfying the constraint, prefer ``active`` over
``deprecated``, and return the highest version. Deprecated-only resolution
succeeds with a warning (and a successor recommendation if set).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

_FAMILY_RE = re.compile(r"^(?P<name>.+?)(?:\.v\d+)?$")


class ToolResolutionError(Exception):
    """Raised when no registered tool version satisfies a reference."""


class ToolBindingError(Exception):
    """Raised when a resolved tool's module/callable can't be imported."""


@dataclass(frozen=True)
class ToolRegistryEntry:
    """A single registered tool version (mirror of ``caliber_tool_registry``)."""

    name: str
    version: str
    module_path: str
    callable_name: str
    side_effect_level: str = "read"  # read | write | external_action
    requires_approval: bool = False
    allow_in_preview: bool = False
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    secret_refs: tuple[str, ...] = ()
    status: str = "active"  # active | deprecated | archived
    successor_ref: str | None = None
    description: str = ""

    @property
    def registry_ref(self) -> str:
        """Canonical reference string, e.g. ``tool.lookup_policy.v1``."""
        major = self.version.split(".", 1)[0]
        return f"tool.{self.name}.v{major}"


@dataclass
class ToolResolution:
    entry: ToolRegistryEntry
    warnings: list[str] = field(default_factory=list)


def family_name(registry_ref: str) -> str:
    """Extract the tool family name from a reference.

    ``tool.lookup_policy.v1`` -> ``lookup_policy``; ``lookup_policy`` ->
    ``lookup_policy``. The ``tool.`` prefix and the trailing ``.vN`` major are
    stripped; everything else is treated as the family name.
    """
    ref = registry_ref
    if ref.startswith("tool."):
        ref = ref[len("tool.") :]
    match = _FAMILY_RE.match(ref)
    return match.group("name") if match else ref


class ToolResolver(Protocol):
    """Resolve a manifest tool reference to registry metadata."""

    def resolve(self, registry_ref: str, version_constraint: str = "") -> ToolResolution: ...


class InMemoryToolResolver:
    """Resolver backed by an in-process list of entries (tests, previews).

    Construct from explicit :class:`ToolRegistryEntry` objects, or from the
    plan's ``FAKE_REGISTRY`` mapping of ``ref -> callable`` via
    :meth:`from_callables`.
    """

    def __init__(self, entries: list[ToolRegistryEntry] | None = None) -> None:
        self._by_name: dict[str, list[ToolRegistryEntry]] = {}
        for entry in entries or []:
            self._by_name.setdefault(entry.name, []).append(entry)
        self._callables: dict[str, Callable[..., Any]] = {}

    @classmethod
    def from_callables(
        cls,
        callables: dict[str, Callable[..., Any]],
        *,
        side_effect_level: str = "read",
        allow_in_preview: bool = False,
    ) -> InMemoryToolResolver:
        """Build a resolver + binding source from ``{registry_ref: callable}``."""
        entries: list[ToolRegistryEntry] = []
        resolver = cls()
        for ref, fn in callables.items():
            name = family_name(ref)
            major = ref.rsplit(".v", 1)[-1] if ".v" in ref else "1"
            version = f"{major}.0" if major.isdigit() else "1.0"
            entry = ToolRegistryEntry(
                name=name,
                version=version,
                module_path="<in-memory>",
                callable_name=getattr(fn, "__name__", name),
                side_effect_level=side_effect_level,
                allow_in_preview=allow_in_preview,
            )
            entries.append(entry)
            resolver._callables[ref] = fn
            resolver._callables[entry.registry_ref] = fn
        resolver._by_name = {}
        for entry in entries:
            resolver._by_name.setdefault(entry.name, []).append(entry)
        return resolver

    def register(
        self, entry: ToolRegistryEntry, callable_: Callable[..., Any] | None = None
    ) -> None:
        self._by_name.setdefault(entry.name, []).append(entry)
        if callable_ is not None:
            self._callables[entry.registry_ref] = callable_

    def get_callable(self, registry_ref: str) -> Callable[..., Any] | None:
        return self._callables.get(registry_ref)

    def resolve(self, registry_ref: str, version_constraint: str = "") -> ToolResolution:
        name = family_name(registry_ref)
        candidates = [e for e in self._by_name.get(name, []) if e.status != "archived"]
        if not candidates:
            raise ToolResolutionError(
                f"tool {registry_ref!r} is not registered (family {name!r} has no active versions)"
            )

        spec = SpecifierSet(version_constraint) if version_constraint else None

        def _satisfies(entry: ToolRegistryEntry) -> bool:
            if spec is None:
                return True
            try:
                return Version(entry.version) in spec
            except InvalidVersion:
                return False

        matching = [e for e in candidates if _satisfies(e)]
        if not matching:
            raise ToolResolutionError(
                f"no version of {name!r} satisfies constraint {version_constraint!r} "
                f"(available: {sorted(e.version for e in candidates)})"
            )

        def _sort_key(entry: ToolRegistryEntry) -> tuple[int, Version]:
            # Prefer active over deprecated, then highest version.
            active_rank = 1 if entry.status == "active" else 0
            try:
                ver = Version(entry.version)
            except InvalidVersion:
                ver = Version("0")
            return (active_rank, ver)

        best = max(matching, key=_sort_key)
        warnings: list[str] = []
        if best.status == "deprecated":
            msg = f"tool {best.registry_ref!r} is deprecated"
            if best.successor_ref:
                msg += f"; consider migrating to {best.successor_ref!r}"
            warnings.append(msg)
        return ToolResolution(entry=best, warnings=warnings)


#: Process-wide allowlist of module prefixes a registered tool may be imported from.
#:
#: Bound at app startup from ``config.registered_tool_module_allowlist`` rather than
#: threaded through every call site: a registered tool is imported from the runtime,
#: the compiler's generated code, and two route paths, and an allowlist that only some
#: of them consulted would be worse than none — it would read as enforced while
#: leaving the unthreaded paths open. ``None`` means unrestricted (see the config
#: field for why that is the shipped default, and how it is surfaced).
_MODULE_ALLOWLIST: str | None = None
_BOUND_MODULE_ALLOWLIST: object = object()


def bind_module_allowlist(allowlist: str | None) -> None:
    """Install the process-wide registered-tool module allowlist."""
    global _MODULE_ALLOWLIST  # noqa: PLW0603 - one process-wide policy by design
    text = (allowlist or "").strip()
    _MODULE_ALLOWLIST = text or None


def registered_tool_module_allowed(module_path: str, allowlist: str | None = None) -> bool:
    """Whether ``module_path`` may be imported as a registered tool.

    An unset allowlist permits everything, which is the documented default. When set,
    a module must match an entry exactly or fall under a ``prefix.*`` entry — the same
    semantics as the external-app entrypoint allowlist, so an operator learns one rule
    rather than two.
    """
    patterns = _MODULE_ALLOWLIST if allowlist is None else allowlist
    if not patterns:
        return True
    candidate = (module_path or "").strip()
    if not candidate:
        return False
    for raw in str(patterns).split(","):
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            # A bare '*' would mean "allow everything", which defeats the control.
            if prefix and candidate.startswith(prefix):
                return True
            continue
        if candidate == pattern:
            return True
    return False


def bind_registered_tool(
    entry: ToolRegistryEntry,
    *,
    callable_override: Callable[..., Any] | None = None,
    module_allowlist: str | object | None = _BOUND_MODULE_ALLOWLIST,
) -> Callable[..., Any]:
    """Resolve a registry entry to a concrete callable.

    ``callable_override`` short-circuits import (used by the in-memory resolver
    and previews). Otherwise the entry's ``module_path``/``callable_name`` is
    imported. Wrapping as an Agents SDK ``function_tool`` is deferred to the
    runtime executor that actually needs the SDK object — keeping this module
    importable without the ``[llm]`` extra.

    The import is checked against :func:`registered_tool_module_allowed` first. This
    path imports and invokes installed Python **in the control-plane process**, so
    when an operator has declared which modules are legitimate, a registry row naming
    anything else is refused before the import rather than after.
    """
    if callable_override is not None:
        return callable_override
    if entry.module_path == "<in-memory>":
        raise ToolBindingError(
            f"tool {entry.registry_ref!r} has no importable module and no override callable"
        )
    allowed = (
        registered_tool_module_allowed(entry.module_path)
        if module_allowlist is _BOUND_MODULE_ALLOWLIST
        # Passing an explicit empty/None value means unrestricted for this binding;
        # use "" so registered_tool_module_allowed does not fall back to the global.
        else registered_tool_module_allowed(entry.module_path, str(module_allowlist or ""))
    )
    if not allowed:
        raise ToolBindingError(
            f"tool {entry.registry_ref!r} module {entry.module_path!r} is not in "
            "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST; this module is imported and "
            "invoked in the CALIBER process"
        )
    import importlib  # noqa: PLC0415

    try:
        module = importlib.import_module(entry.module_path)
    except ImportError as exc:  # pragma: no cover - exercised via compile-error test
        raise ToolBindingError(
            f"cannot import module {entry.module_path!r} for tool {entry.registry_ref!r}: {exc}"
        ) from exc
    try:
        fn: Callable[..., Any] = getattr(module, entry.callable_name)
    except AttributeError as exc:
        raise ToolBindingError(
            f"module {entry.module_path!r} has no attribute {entry.callable_name!r} "
            f"for tool {entry.registry_ref!r}"
        ) from exc
    return fn


__all__ = [
    "InMemoryToolResolver",
    "ToolBindingError",
    "ToolRegistryEntry",
    "ToolResolution",
    "ToolResolutionError",
    "ToolResolver",
    "bind_module_allowlist",
    "bind_registered_tool",
    "family_name",
    "registered_tool_module_allowed",
]
