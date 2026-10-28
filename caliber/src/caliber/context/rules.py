"""Reference a run's admitted rule bundle from its context, and verify its bytes again when it is used (B07.5b, #736).

Admission pins the configured rule bundle once: :func:`caliber.run_manifest.pin_rule_bundle` verifies the file's bytes
against the operator's digest and the run manifest records the absolute path and that SHA-256. A run manifest is
immutable, so this module never writes one: it *reads* the pin from the manifest the worker already verified, and each
agent step's context manifest cites it.

:func:`rule_bundle_reference` is called for every agent step, before its context manifest is built. It re-reads the bytes
at the **path the manifest recorded** (never the configured path or an alias, so a setting changed after admission cannot
change what a run uses), verifies them against the **digest the manifest recorded** and only then parses them
(:func:`caliber.context.rule_bundle.load_rule_bundle`). Bytes that no longer match, a file that is gone and a manifest
that names a path without a digest (or a digest without a path) are refused with a typed
:class:`~caliber.context.manifest.ContextManifestError`; nothing is rewritten, backfilled or substituted.

What the context manifest records is a **reference**, not the rule text: the bundle's id, version and SHA-256, and for
each selected rule its id, its ``source_ref`` (provenance) and the SHA-256 of its text. Delivering the text to the
provider is the agent-step adapter's job (B08), as for the other context caps. Selection is the whole bundle: every rule
of the operator-pinned bundle is selected, in bundle order. Narrowing it by topic is :meth:`RuleBundle.rules_for`, which
B08 may apply; this module never invents a topic.

Pure apart from reading the one pinned file: no database, no provider, no network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Final

from caliber.context.manifest import (
    RULE_BUNDLE_CHANGED,
    RULE_BUNDLE_MISMATCH,
    RULE_BUNDLE_UNAVAILABLE,
    RULE_BUNDLE_UNPINNED,
    ContextManifestError,
)
from caliber.context.rule_bundle import (
    DIGEST_MISMATCH,
    TOO_LARGE,
    RuleBundle,
    RuleBundleError,
    load_rule_bundle,
)

_CHANGED_CODES: Final = frozenset({DIGEST_MISMATCH, TOO_LARGE})


def rule_bundle_record(bundle: RuleBundle) -> dict[str, Any]:
    """The ``rule_bundle`` entry of a context manifest for a verified ``bundle``: its identity and every rule's id,
    provenance (``source_ref``) and text digest. The rule text itself is not recorded."""
    return {
        "bundle_id": bundle.bundle_id,
        "version": bundle.version,
        "sha256": bundle.sha256,
        "rules": [
            {
                "rule_id": rule.id,
                "source_ref": rule.source_ref,
                "text_digest": hashlib.sha256(rule.text.encode("utf-8")).hexdigest(),
            }
            for rule in bundle.rules
        ],
    }


def rule_bundle_reference(run_manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``rule_bundle`` entry for the run whose manifest this is, after verifying the pinned bytes now.

    ``None`` when the run was admitted without a rule bundle (the manifest has neither a path nor a digest): a bundle
    enabled after admission is never applied to a run that was not pinned to it. Raises
    :class:`~caliber.context.manifest.ContextManifestError` with :data:`RULE_BUNDLE_CHANGED` when the bytes no longer
    match the admitted digest and :data:`RULE_BUNDLE_UNAVAILABLE` for anything else that stops the bundle being read.
    The message never quotes the file.
    """
    path = run_manifest.get("rule_bundle_path")
    digest = run_manifest.get("rule_bundle_sha256")
    if path is None and digest is None:
        return None
    if type(path) is not str or type(digest) is not str or not path:
        raise ContextManifestError(
            RULE_BUNDLE_UNAVAILABLE,
            "the run manifest does not pin both a rule bundle path and its digest",
        )
    try:
        bundle = load_rule_bundle(path, digest)
    except (RuleBundleError, ValueError) as exc:
        # ``ValueError``: a path with an embedded NUL. Neither message carries the file's contents.
        code = (
            RULE_BUNDLE_CHANGED
            if isinstance(exc, RuleBundleError) and exc.code in _CHANGED_CODES
            else RULE_BUNDLE_UNAVAILABLE
        )
        detail = exc.code if isinstance(exc, RuleBundleError) else "unreadable"
        raise ContextManifestError(
            code, f"the run's pinned rule bundle cannot be used now ({detail})"
        ) from None
    return rule_bundle_record(bundle)


__all__ = [
    "RULE_BUNDLE_CHANGED",
    "RULE_BUNDLE_MISMATCH",
    "RULE_BUNDLE_UNAVAILABLE",
    "RULE_BUNDLE_UNPINNED",
    "rule_bundle_record",
    "rule_bundle_reference",
]
