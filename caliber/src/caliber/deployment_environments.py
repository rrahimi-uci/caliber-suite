"""Classify a deployment alias into an *environment class*.

The review rejected keying production safety requirements to an alias **string**:
``CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES`` defaulted to the single
literal ``prod`` and was matched case-sensitively against an unvalidated alias
path segment, so ``production``, ``prod-eu``, and ``PROD`` all promoted with
local containment and no blocker. A safety requirement must be keyed to what the
deployment *is*, not to how it happens to be spelled.

This module is the single place that decides that. Three classes:

``production``
    Serves real traffic. Requires the strongest available boundaries.
``staging``
    Pre-production rehearsal. Real-ish, but not customer-facing.
``development``
    Local/experimental. The only class where local containment is acceptable.

Resolution order, first match wins:

1. an explicit operator mapping (``CALIBER_DEPLOYMENT_ENVIRONMENT_CLASSES``),
   which always wins so an operator can classify a house-style alias;
2. built-in name patterns covering the spellings teams actually use; and
3. the configured default for unrecognised aliases — ``production``, because
   *fail closed* is the only safe default for a safety gate. An operator who
   deploys to ``canary`` gets production requirements until they say otherwise.

This module is deliberately a leaf: it imports nothing from CALIBER beyond the
config type, so both :mod:`caliber.mcp_policy` and
:mod:`caliber.workflows.promoter` can use it without an import cycle.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from caliber.config import CaliberConfig

PRODUCTION: Final = "production"
STAGING: Final = "staging"
DEVELOPMENT: Final = "development"

#: Every class this module can return, ordered most to least restrictive.
ENVIRONMENT_CLASSES: Final[tuple[str, ...]] = (PRODUCTION, STAGING, DEVELOPMENT)

#: Built-in alias patterns. Matched against the *normalized* alias (casefolded,
#: with ``_`` and whitespace folded to ``-``), so ``PROD_EU`` and ``prod eu``
#: reach the same verdict as ``prod-eu``. Ordered: staging is checked before
#: production because ``pre-prod`` contains ``prod``.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        STAGING,
        re.compile(r"^(staging|stage|stg|uat|qa|preprod|pre-prod|pre-production)(-.*)?$"),
    ),
    (PRODUCTION, re.compile(r"^(prod|production|prd|live)(-.*)?$")),
    (
        DEVELOPMENT,
        re.compile(r"^(dev|development|devel|local|sandbox|test|preview|scratch)(-.*)?$"),
    ),
)

#: CALIBER's own non-deployment sentinels. ``manual`` and ``preview`` are not
#: aliases an operator ever promotes to — they mark "run this version directly,
#: outside any alias". They must not inherit the fail-closed production default,
#: which would make every direct run demand a production isolation boundary.
_NON_DEPLOYMENT_ALIASES: Final[frozenset[str]] = frozenset({"manual", "preview", "draft"})


def normalize_alias(alias: str) -> str:
    """Fold an alias to its comparison form.

    Alias values arrive as an unvalidated URL path segment, so a requirement
    keyed to them has to be insensitive to case and separator style or it is
    trivially sidestepped by spelling.
    """
    return re.sub(r"[\s_]+", "-", str(alias).strip().casefold())


def _explicit_mapping(config: CaliberConfig | None) -> dict[str, str]:
    """Parse ``alias=class,alias=class`` into a normalized lookup.

    Unknown class names are ignored rather than raising: a typo in operator
    configuration must not silently *downgrade* a requirement, and falling
    through to the patterns/default keeps the fail-closed behaviour.
    """
    raw = getattr(config, "deployment_environment_classes", "") or ""
    mapping: dict[str, str] = {}
    for item in str(raw).split(","):
        alias, _, klass = item.partition("=")
        alias_key = normalize_alias(alias)
        klass_key = klass.strip().casefold()
        if alias_key and klass_key in ENVIRONMENT_CLASSES:
            mapping[alias_key] = klass_key
    return mapping


def default_environment_class(config: CaliberConfig | None) -> str:
    raw = str(getattr(config, "deployment_default_environment_class", "") or "").strip().casefold()
    return raw if raw in ENVIRONMENT_CLASSES else PRODUCTION


def environment_class(alias: str, config: CaliberConfig | None = None) -> str:
    """Return the environment class ``alias`` belongs to.

    Never raises and never returns an unknown value, so callers can use the
    result directly in a policy decision.
    """
    key = normalize_alias(alias)
    explicit = _explicit_mapping(config)
    if key in explicit:
        return explicit[key]
    if key in _NON_DEPLOYMENT_ALIASES:
        return DEVELOPMENT
    for klass, pattern in _PATTERNS:
        if pattern.match(key):
            return klass
    return default_environment_class(config)


def isolation_required_classes(config: CaliberConfig | None = None) -> frozenset[str]:
    """Environment classes that require an external MCP isolation boundary."""
    raw = getattr(config, "mcp_require_external_isolation_for_environment_classes", "") or ""
    values = {item.strip().casefold() for item in str(raw).split(",") if item.strip()}
    selected = values & set(ENVIRONMENT_CLASSES)
    return frozenset(selected or {PRODUCTION})


def requires_external_isolation(alias: str, config: CaliberConfig | None = None) -> bool:
    """Whether promoting to ``alias`` must have an external isolation boundary.

    Keyed on the environment class. The legacy explicit alias list
    (``CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES``) is still honoured
    as an *additional* opt-in so an existing deployment's configuration keeps
    working; it can no longer be the only thing standing between ``prod-eu`` and
    local stdio execution.
    """
    if environment_class(alias, config) in isolation_required_classes(config):
        return True
    legacy = getattr(config, "mcp_require_external_isolation_for_aliases", "") or ""
    legacy_aliases = {normalize_alias(item) for item in str(legacy).split(",") if item.strip()}
    return normalize_alias(alias) in legacy_aliases


__all__ = [
    "DEVELOPMENT",
    "ENVIRONMENT_CLASSES",
    "PRODUCTION",
    "STAGING",
    "default_environment_class",
    "environment_class",
    "isolation_required_classes",
    "normalize_alias",
    "requires_external_isolation",
]
