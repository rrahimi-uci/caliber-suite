"""Reference a run's admitted rule bundle from its context, and verify its bytes again when it is used (B07.5b, #736).

Admission pins the configured rule bundle once: :func:`caliber.run_manifest.pin_rule_bundle` verifies the file's bytes
against the operator's digest and the run manifest records the **symlink-resolved** absolute path and that SHA-256. A run
manifest is immutable, so this module never writes one: it *reads* the pin from the manifest the worker already
verified, and each agent step's context manifest cites it.

:func:`rule_bundle_reference` is called for every agent step, before its context manifest is built. It re-reads the bytes
at the **path the manifest recorded** (never the configured path or an alias, so a setting changed after admission cannot
change what a run uses) and re-hashes them on **every** call, then compares the hash with the **digest the manifest
recorded**. Only bytes whose hash equals the pinned digest are used, and only then are they parsed
(:func:`caliber.context.rule_bundle.parse_rule_bundle`). The parse and the manifest entry are cached by that verified
digest (a small bounded LRU): a digest is the hash of exactly one byte string, so a cache hit can only ever be the
bundle those bytes parse to, and the cache never replaces the read and the hash. Bytes that no longer match, a file that
is gone and a manifest that names a path without a digest (or a digest without a path) are refused with a typed
:class:`~caliber.context.manifest.ContextManifestError`; nothing is rewritten, backfilled or substituted.

The pinned path is the one admission recorded, resolved through symlinks at that moment, and it is read as written by
**whichever node executes the run**. It must therefore exist and hold the same bytes on every node that can run the run:
a mount whose real path changes on rotation (a Kubernetes ConfigMap or Secret ``..data/<timestamp>`` directory, a Docker
secret) leaves the recorded path missing, and the step is refused as ``rule_bundle_unavailable``.

What the context manifest records is a **reference**, not the rule text: the bundle's id, version and SHA-256, and for
each selected rule its id, its ``source_ref`` (provenance) and the SHA-256 of its text. Delivering the text to the
provider is the agent-step adapter's job (B08), as for the other context caps. Selection is the whole bundle: every rule
of the operator-pinned bundle is selected, in bundle order. Narrowing it by topic is :meth:`RuleBundle.rules_for`, which
B08 may apply; this module never invents a topic. :func:`rule_bundle_reference` hands back the **verified in-memory
bundle** with the entry (:class:`VerifiedRuleBundle`): B08 must take the rules from that object and never reopen the
path, so the text it gives a provider is the text whose digest the manifest cites.

Pure apart from reading the one pinned file: no database, no provider, no network.
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
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
    parse_rule_bundle,
    read_rule_bundle_bytes,
    rule_bundle_record,
    sha256_of,
)

_CHANGED_CODES: Final = frozenset({DIGEST_MISMATCH, TOO_LARGE})
#: How many verified bundles are kept. A deployment pins one bundle, so a handful covers a rotation in progress.
CACHE_SIZE: Final = 8


@dataclass(frozen=True)
class VerifiedRuleBundle:
    """A bundle whose bytes were verified against the run's pinned digest just now, and its manifest entry.

    ``bundle`` is the parsed, immutable bundle (hand its rules to the provider from here, never from the path);
    ``record`` is the ``rule_bundle`` entry for the context manifest (a fresh copy on every call, safe to mutate).
    """

    bundle: RuleBundle
    record: dict[str, Any]


_cache: OrderedDict[str, tuple[RuleBundle, dict[str, Any]]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_rule_bundle_cache() -> None:
    """Forget every cached bundle (tests; a hit is always safe, so production never needs this)."""
    with _cache_lock:
        _cache.clear()


def _verified(data: bytes, digest: str) -> tuple[RuleBundle, dict[str, Any]]:
    """``(bundle, entry)`` for ``data`` whose SHA-256 is ``digest``; parses at most once per verified digest.

    The hash of ``data`` is compared with ``digest`` on every call. Anything else (a malformed digest, other bytes) goes
    to :func:`parse_rule_bundle`, which refuses it with the typed reason.
    """
    if type(digest) is str and sha256_of(data) == digest:
        with _cache_lock:
            hit = _cache.get(digest)
            if hit is not None:
                _cache.move_to_end(digest)
                return hit
    bundle = parse_rule_bundle(data, digest)  # raises unless the bytes hash to the digest
    entry = (bundle, rule_bundle_record(bundle))
    with _cache_lock:
        _cache[digest] = entry
        _cache.move_to_end(digest)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return entry


def rule_bundle_reference(run_manifest: Mapping[str, Any]) -> VerifiedRuleBundle | None:
    """The verified bundle and ``rule_bundle`` entry for the run whose manifest this is, after verifying the bytes now.

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
        bundle, entry = _verified(read_rule_bundle_bytes(path), digest)
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
    return VerifiedRuleBundle(bundle=bundle, record=copy.deepcopy(entry))


__all__ = [
    "CACHE_SIZE",
    "RULE_BUNDLE_CHANGED",
    "RULE_BUNDLE_MISMATCH",
    "RULE_BUNDLE_UNAVAILABLE",
    "RULE_BUNDLE_UNPINNED",
    "VerifiedRuleBundle",
    "clear_rule_bundle_cache",
    "rule_bundle_record",
    "rule_bundle_reference",
]
