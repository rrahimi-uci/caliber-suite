"""RuleBundle v1: a hashed, strictly parsed set of rules, loaded before anything is admitted (B02 rule-loader, #726).

A rule bundle is a YAML file an operator pins by SHA-256. :func:`load_rule_bundle` verifies the **raw bytes** against
the pinned digest *before it parses a single character*, so a byte that was altered is refused even when the YAML
values would read the same (a comment, whitespace, a re-ordered key). Only then is the document parsed, strictly:

.. code-block:: yaml

    schema_version: v1
    bundle_id: support-rules          # [a-z0-9][a-z0-9._-]*, up to 128 characters
    version: "2026-10-01"             # a string, so 1.0 is never silently a float
    rules:
      - id: refund-window             # unique within the bundle, same pattern as bundle_id
        text: "Refunds are allowed within 30 days of delivery."
        source_ref: "policy/refunds#section-2"
        applies_to: [refund, order-status]

Every refusal is a :class:`RuleBundleError` with a typed ``code`` (see :data:`CODES`) and a message that never
quotes the file's contents. The loader is pure: no database, no provider, no network, no run, and no dependency on a
context manifest (that is B07). #676 consumes it before immutable admission, and #736 re-verifies the pinned bytes
when the bundle is used.

The bundle schema is enforced here, in code, with closed key sets: an unknown key is refused rather than ignored,
so a later field is added readers-first (a reader that understands it ships before a writer that uses it).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

#: Every reason a bundle is refused.
BAD_EXPECTED_DIGEST: Final = "bad_expected_digest"
MISSING_BYTES: Final = "missing_bytes"
TOO_LARGE: Final = "too_large"
DIGEST_MISMATCH: Final = "digest_mismatch"
INVALID_YAML: Final = "invalid_yaml"
UNSUPPORTED_VERSION: Final = "unsupported_version"
UNKNOWN_FIELD: Final = "unknown_field"
MALFORMED: Final = "malformed"
DUPLICATE_RULE_ID: Final = "duplicate_rule_id"
UNKNOWN_RULE: Final = "unknown_rule"
CODES: Final = frozenset(
    {
        BAD_EXPECTED_DIGEST,
        MISSING_BYTES,
        TOO_LARGE,
        DIGEST_MISMATCH,
        INVALID_YAML,
        UNSUPPORTED_VERSION,
        UNKNOWN_FIELD,
        MALFORMED,
        DUPLICATE_RULE_ID,
        UNKNOWN_RULE,
    }
)

SCHEMA_VERSION: Final = "v1"
MAX_BUNDLE_BYTES: Final = 1_048_576
MAX_RULES: Final = 1000
MAX_RULE_TEXT: Final = 8000
MAX_FIELD: Final = 512
#: The most canonical-JSON bytes the ``rule_bundle`` entry a context manifest cites may take (B07.5b, #736). A context
#: manifest is bounded to 1 MiB; this leaves its segments headroom. Admission refuses a bundle whose citation is larger.
MAX_CITATION_BYTES: Final = 262_144

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_OPEN_FLAGS: Final = ("O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC")
_TOP_KEYS: Final = frozenset({"schema_version", "bundle_id", "version", "rules"})
_RULE_KEYS: Final = frozenset({"id", "text", "source_ref", "applies_to"})


class RuleBundleError(ValueError):
    """A bundle that cannot be loaded. ``code`` is one of :data:`CODES`; the message never quotes the file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Rule:
    """One rule: what it says, where it comes from, and what it applies to."""

    id: str
    text: str
    source_ref: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class RuleBundle:
    """A verified bundle. ``sha256`` is the digest of the exact bytes it was parsed from."""

    bundle_id: str
    version: str
    rules: tuple[Rule, ...]
    sha256: str
    size_bytes: int

    def rule(self, rule_id: str) -> Rule:
        """The rule with this id, or :class:`RuleBundleError` ``unknown_rule`` (the bundle is closed)."""
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise RuleBundleError(UNKNOWN_RULE, "the bundle has no rule with that id")

    def rules_for(self, topic: str) -> tuple[Rule, ...]:
        """The rules whose ``applies_to`` names ``topic`` exactly, in bundle order."""
        return tuple(rule for rule in self.rules if topic in rule.applies_to)


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


def sha256_of(data: bytes) -> str:
    """The lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


class _StrictLoader(yaml.SafeLoader):
    """``yaml.safe_load`` that refuses a duplicate mapping key instead of letting the last one win."""


def _construct_unique_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.YAMLError("an unhashable mapping key") from None
        if duplicate:
            raise yaml.YAMLError("a duplicate mapping key")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_rule_bundle(path: str | Path, expected_sha256: str) -> RuleBundle:
    """Read ``path`` (a regular file, read once and bounded), then :func:`parse_rule_bundle` its bytes."""
    return parse_rule_bundle(_read(Path(path)), expected_sha256)


def read_rule_bundle_bytes(path: str | Path) -> bytes:
    """The bytes of ``path`` exactly as :func:`load_rule_bundle` reads them (a regular file, read once, bounded), for
    a caller that verifies the digest itself before parsing (:func:`parse_rule_bundle`). Raises
    :class:`RuleBundleError` ``missing_bytes`` or ``too_large``."""
    return _read(Path(path))


def parse_rule_bundle(data: bytes, expected_sha256: str) -> RuleBundle:
    """Verify ``data`` against ``expected_sha256`` and only then parse and validate it.

    Order matters: the digest is checked on the raw bytes first, so nothing is parsed from bytes the operator did not
    pin. This is the entry point for a caller that already holds the pinned bytes (the admission record, #676; the
    context code re-verifying them, #736). Raises :class:`RuleBundleError` (``bad_expected_digest``,
    ``missing_bytes``, ``too_large``, ``digest_mismatch``, ``invalid_yaml``, ``unsupported_version``,
    ``unknown_field``, ``malformed``, ``duplicate_rule_id``) and nothing else; returns a :class:`RuleBundle`.
    """
    if type(expected_sha256) is not str or not _DIGEST.fullmatch(expected_sha256):
        raise RuleBundleError(
            BAD_EXPECTED_DIGEST, "expected_sha256 must be a full lowercase 64-hex SHA-256 digest"
        )
    if type(data) is not bytes or not data:
        raise RuleBundleError(MISSING_BYTES, "there are no bytes to verify")
    if len(data) > MAX_BUNDLE_BYTES:
        raise RuleBundleError(TOO_LARGE, f"the bundle is larger than {MAX_BUNDLE_BYTES} bytes")
    if not hmac.compare_digest(sha256_of(data), expected_sha256):
        raise RuleBundleError(DIGEST_MISMATCH, "the bundle's bytes do not match the pinned digest")
    return _parse(data, expected_sha256)


def _read(path: Path) -> bytes:
    """The file's bytes, read once from one open file: a regular file only, never more than the cap plus one byte.

    A FIFO, a device (``/dev/zero``) or a directory is refused before any read, and the read itself is bounded, so a
    growing or endless file cannot exhaust memory and there is no gap between a size check and the read.
    """
    try:
        # ``O_NONBLOCK`` so that opening a FIFO with no writer returns at once instead of blocking forever;
        # ``O_NOCTTY`` so that a terminal device is never made the process's controlling terminal and ``O_CLOEXEC``
        # so that the descriptor is not inherited by a child process.
        flags = os.O_RDONLY | sum(getattr(os, name, 0) for name in _OPEN_FLAGS)
        descriptor = os.open(path, flags)
    except OSError:
        raise RuleBundleError(MISSING_BYTES, "the bundle file cannot be read") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuleBundleError(MISSING_BYTES, "the bundle path is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_BUNDLE_BYTES + 1)
    except OSError:
        raise RuleBundleError(MISSING_BYTES, "the bundle file cannot be read") from None
    finally:
        os.close(descriptor)
    if not data:
        raise RuleBundleError(MISSING_BYTES, "the bundle file is empty")
    if len(data) > MAX_BUNDLE_BYTES:
        raise RuleBundleError(TOO_LARGE, f"the bundle is larger than {MAX_BUNDLE_BYTES} bytes")
    return data


def _parse(data: bytes, digest: str) -> RuleBundle:
    try:
        document = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except (
        UnicodeDecodeError,
        yaml.YAMLError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
    ):
        # PyYAML raises more than YAMLError for hostile or merely odd input (an impossible date, a huge integer, a
        # bad explicit tag, deep nesting); every one of them is "not a valid bundle", never a crash.
        raise RuleBundleError(
            INVALID_YAML, "the bundle is not valid UTF-8 YAML with unique keys"
        ) from None
    if not isinstance(document, dict):
        raise RuleBundleError(MALFORMED, "the bundle must be a mapping")
    _closed(document, _TOP_KEYS, "the bundle")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise RuleBundleError(UNSUPPORTED_VERSION, f"schema_version must be {SCHEMA_VERSION!r}")
    bundle_id = _identifier(document.get("bundle_id"), "bundle_id")
    version = _text(document.get("version"), "version", MAX_FIELD)
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleBundleError(MALFORMED, "rules must be a non-empty list")
    if len(raw_rules) > MAX_RULES:
        raise RuleBundleError(MALFORMED, f"a bundle may hold at most {MAX_RULES} rules")
    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rules):
        rule = _rule(raw, index)
        if rule.id in seen:
            raise RuleBundleError(DUPLICATE_RULE_ID, f"rule {index + 1} repeats an earlier rule id")
        seen.add(rule.id)
        rules.append(rule)
    return RuleBundle(
        bundle_id=bundle_id,
        version=version,
        rules=tuple(rules),
        sha256=digest,
        size_bytes=len(data),
    )


def _rule(raw: object, index: int) -> Rule:
    where = f"rule {index + 1}"
    if not isinstance(raw, dict):
        raise RuleBundleError(MALFORMED, f"{where} must be a mapping")
    _closed(raw, _RULE_KEYS, where)
    missing = sorted(_RULE_KEYS - set(raw))
    if missing:
        raise RuleBundleError(MALFORMED, f"{where} is missing {missing}")
    applies = raw["applies_to"]
    if not isinstance(applies, list) or not applies:
        raise RuleBundleError(MALFORMED, f"{where}: applies_to must be a non-empty list")
    topics = [_text(topic, f"{where}: applies_to entry", MAX_FIELD) for topic in applies]
    if len(set(topics)) != len(topics):
        raise RuleBundleError(MALFORMED, f"{where}: applies_to repeats an entry")
    return Rule(
        id=_identifier(raw["id"], f"{where}: id"),
        text=_text(raw["text"], f"{where}: text", MAX_RULE_TEXT, multiline=True),
        source_ref=_text(raw["source_ref"], f"{where}: source_ref", MAX_FIELD),
        applies_to=tuple(topics),
    )


def _closed(mapping: dict[Any, Any], allowed: frozenset[str], where: str) -> None:
    if any(type(key) is not str for key in mapping):
        raise RuleBundleError(MALFORMED, f"{where} has a key that is not a string")
    unknown = set(mapping) - allowed
    if unknown:
        raise RuleBundleError(
            UNKNOWN_FIELD,
            f"{where} has {len(unknown)} unknown field(s); the allowed fields are {sorted(allowed)}",
        )


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise RuleBundleError(MALFORMED, f"{name} must match [a-z0-9][a-z0-9._-]{{0,127}}")
    return value


def _text(value: object, name: str, limit: int, *, multiline: bool = False) -> str:
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > limit:
        raise RuleBundleError(
            MALFORMED,
            f"{name} must be a non-empty string with no surrounding whitespace, up to {limit} characters",
        )
    allowed = "\n\t" if multiline else ""
    if any(
        unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Cn", "Zl", "Zp"} and ch not in allowed
        for ch in value
    ):
        raise RuleBundleError(
            MALFORMED,
            f"{name} must not contain control, format, surrogate or unassigned characters",
        )
    return value


__all__ = [
    "BAD_EXPECTED_DIGEST",
    "CODES",
    "DIGEST_MISMATCH",
    "DUPLICATE_RULE_ID",
    "INVALID_YAML",
    "MALFORMED",
    "MAX_BUNDLE_BYTES",
    "MAX_CITATION_BYTES",
    "MISSING_BYTES",
    "SCHEMA_VERSION",
    "TOO_LARGE",
    "UNKNOWN_FIELD",
    "UNKNOWN_RULE",
    "UNSUPPORTED_VERSION",
    "Rule",
    "RuleBundle",
    "RuleBundleError",
    "load_rule_bundle",
    "parse_rule_bundle",
    "read_rule_bundle_bytes",
    "rule_bundle_record",
    "sha256_of",
]
