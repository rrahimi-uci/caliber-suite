"""Discovery of third-party optimizers, and the allowlist that gates it.

Python entry points are a discovery mechanism, not an authorization one: any
installed distribution can advertise itself into the ``caliber.optimizers``
group, and a plain ``importlib.metadata`` loop would execute all of them. For
most plugin systems that is the intended convenience. Here it is not.

An optimizer writes the artifact that CALIBER promotes to production. A
transitive dependency that quietly registered one would gain authority over
production prompts without appearing in any review, and the operator would have
no way to notice from the outside — the refinement loop would keep working. So
discovery reports everything and enablement requires the deployment to name the
distribution:

    CALIBER_PLUGIN_ALLOWLIST=acme-caliber-optimizers,another-plugin

The allowlist matches **distribution** names rather than optimizer names, so
allowlisting is a statement about who you trust rather than about what they
happened to call their entry point. An unlisted plugin is reported as available
but not loaded, which is what lets the UI offer "enable this" instead of leaving
an operator to guess that the wheel they installed did nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from importlib import metadata

from caliber.extensibility.registry import OptimizerSpec, PluginError

logger = logging.getLogger("caliber.extensibility.entrypoints")

#: Entry-point group third-party optimizers advertise into.
OPTIMIZER_GROUP = "caliber.optimizers"

#: Comma-separated distribution names permitted to register optimizers.
ALLOWLIST_ENV_VAR = "CALIBER_PLUGIN_ALLOWLIST"


def allowlisted_distributions() -> frozenset[str]:
    """Distributions the deployment permits, normalised.

    Normalised the way packaging does (lowercase, ``_``/``.`` → ``-``) so
    ``Acme_Caliber_Optimizers`` in the environment matches ``acme-caliber-
    optimizers`` in the metadata. Without that, an allowlist entry could look
    correct and silently match nothing.
    """
    raw = os.environ.get(ALLOWLIST_ENV_VAR, "")
    return frozenset(_normalize(item) for item in raw.split(",") if item.strip())


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _entry_points() -> list[metadata.EntryPoint]:
    try:
        return list(metadata.entry_points(group=OPTIMIZER_GROUP))
    except Exception as exc:  # pragma: no cover - a broken metadata cache
        # Discovery failing must not make CALIBER unstartable: no plugins is a
        # working deployment, and this is the path every deployment without
        # plugins takes.
        logger.warning("optimizer entry-point discovery failed: %s", exc)
        return []


def available_optimizer_plugins() -> list[dict[str, object]]:
    """Every advertised plugin and whether it is permitted, without loading any.

    Deliberately does not import the modules. This is the listing an operator
    reads *before* deciding to trust something, so producing it must not run the
    code being decided about.
    """
    allowed = allowlisted_distributions()
    found: list[dict[str, object]] = []
    for entry in _entry_points():
        distribution = _distribution_of(entry)
        found.append(
            {
                "name": entry.name,
                "distribution": distribution,
                "value": entry.value,
                "allowlisted": _normalize(distribution or "") in allowed,
            }
        )
    return sorted(found, key=lambda item: str(item["name"]))


def _distribution_of(entry: metadata.EntryPoint) -> str | None:
    """Distribution that advertised this entry point.

    ``EntryPoint.dist`` exists from 3.10 but is only populated when the entry
    point came from a real distribution; a hand-constructed one in a test has
    none, so this is read defensively rather than assumed.
    """
    dist = getattr(entry, "dist", None)
    name = getattr(dist, "name", None)
    return str(name) if name else None


def discover_optimizer_plugins() -> Iterator[tuple[OptimizerSpec | None, tuple[str, str] | None]]:
    """Yield ``(spec, error)`` for each allowlisted plugin.

    Exactly one of the two is set. Errors are yielded rather than raised so one
    broken plugin cannot take down a deployment that has three working ones —
    but they are yielded rather than swallowed, because a plugin the operator
    explicitly allowlisted and that then failed to load is a fact they need.
    """
    allowed = allowlisted_distributions()
    for entry in _entry_points():
        distribution = _distribution_of(entry)
        label = distribution or entry.name
        if _normalize(distribution or "") not in allowed:
            logger.info(
                "optimizer plugin %r from %s is installed but not allowlisted; "
                "add it to %s to enable",
                entry.name,
                label,
                ALLOWLIST_ENV_VAR,
            )
            continue
        try:
            yield _load(entry, distribution), None
        except PluginError as exc:
            yield None, (label, str(exc))
        except Exception as exc:
            # Any exception from third-party import or factory code. Caught
            # broadly on purpose: a plugin can raise anything, and the contract
            # here is "one plugin's failure is that plugin's failure".
            yield None, (label, f"{type(exc).__name__}: {exc}")


def _load(entry: metadata.EntryPoint, distribution: str | None) -> OptimizerSpec:
    """Import the entry point and coerce what it returns into a spec.

    Accepts an :class:`OptimizerSpec` or a zero-argument factory returning one.
    A factory is supported because a plugin may need to probe for an optional
    dependency before declaring what it can do, and it should be able to answer
    honestly rather than register capabilities it cannot deliver.
    """
    loaded = entry.load()
    declaration = loaded() if _is_factory(loaded) else loaded
    return _coerce(declaration, entry=entry, distribution=distribution)


def _is_factory(loaded: object) -> bool:
    """True when the entry point resolved to something to call, not a declaration.

    A class is callable and a declaration instance usually is not, so this cannot
    just test ``callable``: ``PluginDeclaration`` from the plugin SDK is a frozen
    dataclass instance, while a bare factory function is a plain callable. The
    discriminator is whether the object already describes an optimizer.
    """
    if hasattr(loaded, "name") and hasattr(loaded, "artifact_types"):
        return False
    return callable(loaded)


#: Attributes a declaration must carry. Read structurally rather than by type,
#: because a plugin declares itself with ``caliber_plugin_sdk.PluginDeclaration``
#: and this module must accept that without importing it -- a server that
#: required its *own* ``OptimizerSpec`` would force every plugin to depend on the
#: whole CALIBER distribution, which is exactly what the separate plugin SDK
#: exists to avoid.
_REQUIRED_ATTRIBUTES = ("name", "summary", "artifact_types")


def _coerce(
    declaration: object, *, entry: metadata.EntryPoint, distribution: str | None
) -> OptimizerSpec:
    """Translate a plugin's declaration into the registry's own type.

    Duck-typed on purpose (see :data:`_REQUIRED_ATTRIBUTES`), and validated field
    by field rather than trusted: everything here crossed a package boundary from
    code CALIBER did not compile, so a missing attribute or a wrong type must
    become a named plugin error and not an ``AttributeError`` from somewhere deep
    in the registry.
    """
    missing = [name for name in _REQUIRED_ATTRIBUTES if not hasattr(declaration, name)]
    if missing:
        raise PluginError(
            f"entry point {entry.name!r} returned {type(declaration).__name__}, which is "
            f"missing {missing}; expected a caliber_plugin_sdk.PluginDeclaration "
            "(or a callable returning one)"
        )

    name = getattr(declaration, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise PluginError(f"entry point {entry.name!r} declares an unusable name {name!r}")

    raw_types = getattr(declaration, "artifact_types", ())
    if isinstance(raw_types, str):
        # A single string would otherwise iterate into characters and register an
        # optimizer that targets "p", "r", "o"...
        raise PluginError(
            f"optimizer {name!r} declares artifact_types as a string; use a "
            "collection, e.g. ('prompt',)"
        )
    try:
        artifact_types = frozenset(str(item) for item in raw_types)
    except TypeError as exc:
        raise PluginError(
            f"optimizer {name!r} declares artifact_types that are not iterable"
        ) from exc

    extra = getattr(declaration, "extra", None)

    # A plugin does not get to describe itself as built-in, and does not get to
    # claim a different distribution than the one that shipped it. Both are set
    # here rather than read, so the provenance the registry reports is the
    # metadata's and not the plugin's own claim about itself.
    return OptimizerSpec(
        name=name.strip(),
        summary=str(getattr(declaration, "summary", "") or ""),
        artifact_types=artifact_types,
        source="plugin",
        requires=_optional_str(getattr(declaration, "requires", None)),
        distribution=distribution or entry.name,
        explicit_only=bool(getattr(declaration, "explicit_only", False)),
        # Experimental until the plugin contract survives a release, regardless
        # of what the plugin says about itself.
        experimental=True,
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "ALLOWLIST_ENV_VAR",
    "OPTIMIZER_GROUP",
    "allowlisted_distributions",
    "available_optimizer_plugins",
    "discover_optimizer_plugins",
]
