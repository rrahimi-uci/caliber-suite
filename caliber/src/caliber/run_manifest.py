"""The admission builder: a pure function from resolved pins, typed principals and a fixed scope to a run manifest
(B02.1b, #676; ADR 0003 ``run_manifest`` v1).

Admission pins a run *before* it exists. Every reference a run depends on (workflow version, prompts, tools, skills,
knowledge-base versions, model routes, policy version, and the rule bundle when one is enabled) is resolved to one
concrete value first; this module then turns that resolved input into the manifest record, its canonical bytes and
the SHA-256 of those bytes. The caller stores the result with ``CaliberRunManifest`` (#656); nothing here fills a
pin into an existing manifest, because a manifest is written once.

* **Pure.** There is no SQL, no clock, no randomness and no provider call in :func:`build_run_manifest`: the caller
  supplies the manifest id and the creation time, so the same input always yields the same bytes and digest.
  Reading a rule bundle from disk is :func:`pin_rule_bundle`, a separate step that runs *before* the build.
* **Resolved, not named.** A floating selector (``latest``, ``current``, a bare alias, ``name@latest``) is refused:
  a pin is the value the run will actually use, so a later move of an alias cannot change what the run was.
  Lists are sorted and must not repeat, so the order a caller resolved things in does not change the digest.
* **Typed principals.** The subject and workload are :class:`~caliber.policy.principal.Subject` and
  :class:`~caliber.policy.principal.Workload`; the manifest carries exactly the fields of the strict schema.
* **Fixed deployment scope.** :class:`DeploymentScope` (organization, project, region, residency) is server-derived.
  A caller that names another organization, or a subject acting in another project, is refused; there is no
  organization switching.
* **Strict for new writers, tolerant for old readers.** The builder demands full lowercase SHA-256 digests and clean
  identifiers, which the v1 schema does not (it accepts any non-empty string), so a record written before this
  module still validates and :func:`read_manifest` still reads it; it is the *writer* that is strict.
* **The digest excludes itself.** The record has no digest field; ``manifest_digest`` is the SHA-256 of the
  canonical bytes (sorted keys, no whitespace, UTF-8, no ``NaN``), the form ``caliber_run_manifests`` stores.

Not here: choosing the manifest (the admission intent and the idempotent retry are #680 and #684), persisting it, or
resolving pins from the database.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Final

from caliber.context.rule_bundle import RuleBundle, RuleBundleError, load_rule_bundle
from caliber.contracts import ContractValidationError, validate_record
from caliber.policy.principal import Subject, Workload

SCHEMA_VERSION: Final = "v1"
KIND: Final = "run_manifest"

# Every reason a manifest is refused.
UNRESOLVED_PIN: Final = "unresolved_pin"
BAD_PIN: Final = "bad_pin"
DUPLICATE_PIN: Final = "duplicate_pin"
BAD_SCOPE: Final = "bad_scope"
ORGANIZATION_SWITCH: Final = "organization_switch"
PROJECT_MISMATCH: Final = "project_mismatch"
BAD_FIELD: Final = "bad_field"
RULE_BUNDLE_REQUIRED: Final = "rule_bundle_required"
RULE_BUNDLE_UNEXPECTED: Final = "rule_bundle_unexpected"
RULE_BUNDLE_REFUSED: Final = "rule_bundle_refused"
INVALID_MANIFEST: Final = "invalid_manifest"
CODES: Final = frozenset(
    {
        UNRESOLVED_PIN,
        BAD_PIN,
        DUPLICATE_PIN,
        BAD_SCOPE,
        ORGANIZATION_SWITCH,
        PROJECT_MISMATCH,
        BAD_FIELD,
        RULE_BUNDLE_REQUIRED,
        RULE_BUNDLE_UNEXPECTED,
        RULE_BUNDLE_REFUSED,
        INVALID_MANIFEST,
    }
)

#: What the reference profile pins the deployment to (ADR 0001): one organization, one region, data processed locally.
FIXED_TENANT_ID: Final = "local"
FIXED_REGION: Final = "local"
FIXED_RESIDENCY: Final = "local"

#: Words that name a moving target rather than a version. A pin (or the version part of ``name@version``) is refused
#: when any alphanumeric segment of it, after Unicode folding, is one of these: ``latest``, ``v-latest``,
#: ``wfv:latest``, ``gpt-4o-latest``. The list is what the builder can *recognise*; that a value really is a resolved
#: version is the caller's duty (the builder never sees the database).
FLOATING: Final = frozenset(
    {
        "latest",
        "current",
        "head",
        "default",
        "stable",
        "newest",
        "newer",
        "any",
        "auto",
        "none",
        "main",
        "master",
        "trunk",
        "nightly",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SEGMENTS = re.compile(r"[^a-z0-9]+")
_RANGE_CHARS = frozenset("*?^~<>=|!$`{}[]")
_MAX_TEXT = 512
_MAX_ID = 64  # the width of the manifest and run id columns
_MAX_ITEMS = 256  # per list: a run pins a handful of prompts and tools, not thousands
_MAX_MANIFEST_BYTES = 1 << 20


class ManifestError(ValueError):
    """A manifest that cannot be built; ``code`` is one of :data:`CODES`. Never recoverable by retrying as is."""

    def __init__(self, code: str, message: str) -> None:
        if (
            code not in CODES
        ):  # a typo in a refusal code must not become an unknown code on the wire
            raise ValueError(f"unknown manifest refusal code {code!r}")
        self.code = code
        super().__init__(message)


def _text(code: str, name: str, value: object, *, limit: int = _MAX_TEXT) -> str:
    """A clean identifier: exactly ``str``, non-empty, bounded, NFC, no surrounding whitespace, no control char."""
    if type(value) is not str or not value:
        raise ManifestError(code, f"{name} must be a non-empty string")
    if value != value.strip() or len(value) > limit:
        raise ManifestError(
            code, f"{name} must have no surrounding whitespace and at most {limit} characters"
        )
    if unicodedata.normalize("NFC", value) != value:
        raise ManifestError(code, f"{name} must be in Unicode normal form NFC")
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    ):
        raise ManifestError(
            code, f"{name} must not contain control, format or line-separator characters"
        )
    return value


def _floating(value: str) -> bool:
    """Whether ``value`` names a moving target: a floating word as a segment (after NFKC folding, so fullwidth
    and compatibility forms count), a range or wildcard character, or a wildcard version segment such as ``1.x``."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    if any(ch in _RANGE_CHARS for ch in folded):
        return True
    segments = [seg for seg in _SEGMENTS.split(folded) if seg]
    return any(seg in FLOATING or seg == "x" for seg in segments)


def _pin(name: str, value: object) -> str:
    """One resolved reference: a clean identifier that is not a floating selector."""
    text = _text(UNRESOLVED_PIN if value in (None, "") else BAD_PIN, name, value)
    if _floating(text):
        raise ManifestError(
            UNRESOLVED_PIN,
            f"{name} {text!r} is a floating selector; pin the version it resolved to",
        )
    return text


def _digest(name: str, value: object) -> str:
    if value in (None, ""):
        raise ManifestError(UNRESOLVED_PIN, f"{name} is not resolved")
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ManifestError(BAD_PIN, f"{name} must be a full lowercase 64-hex SHA-256 digest")
    return value


def _pin_list(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ManifestError(BAD_PIN, f"{name} must be a list of resolved references")
    items = list(values)
    if len(items) > _MAX_ITEMS:
        raise ManifestError(BAD_PIN, f"{name} lists more than {_MAX_ITEMS} references")
    pins = [_pin(f"{name} entry", v) for v in items]
    if len(set(pins)) != len(pins):
        raise ManifestError(DUPLICATE_PIN, f"{name} names the same reference twice")
    return tuple(sorted(pins))


def _versioned_list(name: str, values: object) -> tuple[str, ...]:
    """Prompt, tool and skill pins are ``<name>@<version>`` (exactly one ``@``): the version is part of what was
    resolved, and only the version is checked for floating words (a prompt may be *named* ``production-support``)."""
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ManifestError(BAD_PIN, f"{name} must be a list of resolved references")
    items = list(values)
    if len(items) > _MAX_ITEMS:
        raise ManifestError(BAD_PIN, f"{name} lists more than {_MAX_ITEMS} references")
    pins: list[str] = []
    for item in items:
        text = _text(UNRESOLVED_PIN if item in (None, "") else BAD_PIN, f"{name} entry", item)
        head, sep, version = text.partition("@")
        if not sep or not head or not version or "@" in version:
            raise ManifestError(UNRESOLVED_PIN, f"{name} entry {text!r} must be <name>@<version>")
        if _floating(version):
            raise ManifestError(
                UNRESOLVED_PIN,
                f"{name} entry {text!r} names a floating version; pin the version it resolved to",
            )
        pins.append(text)
    if len(set(pins)) != len(pins):
        raise ManifestError(DUPLICATE_PIN, f"{name} names the same reference twice")
    return tuple(sorted(pins))


@dataclass(frozen=True)
class KnowledgeBasePin:
    """One knowledge base at the version the run will read."""

    knowledge_base_id: str
    version_id: str

    def __post_init__(self) -> None:
        _text(BAD_PIN, "knowledge_base_id", self.knowledge_base_id)
        _pin("knowledge base version_id", self.version_id)


@dataclass(frozen=True)
class ResolvedPins:
    """Everything a run depends on, each resolved to the concrete value it will use. Validated on construction.

    ``skill_versions`` and ``knowledge_base_versions`` may be empty (a run that uses none); every other field must be
    resolved. The lists are stored sorted, so the order of resolution never changes the manifest's digest.
    """

    workflow_version: str
    bundle_digest: str
    prompt_versions: tuple[str, ...]
    tool_versions: tuple[str, ...]
    skill_versions: tuple[str, ...]
    knowledge_base_versions: tuple[KnowledgeBasePin, ...]
    model_routes: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "workflow_version", _pin("workflow_version", self.workflow_version))
        set_(self, "bundle_digest", _digest("bundle_digest", self.bundle_digest))
        set_(self, "prompt_versions", _versioned_list("prompt_versions", self.prompt_versions))
        set_(self, "tool_versions", _versioned_list("tool_versions", self.tool_versions))
        set_(self, "skill_versions", _versioned_list("skill_versions", self.skill_versions))
        set_(self, "model_routes", _pin_list("model_routes", self.model_routes))
        if not self.model_routes:
            raise ManifestError(
                UNRESOLVED_PIN, "model_routes is not resolved: a run names at least one route"
            )
        set_(self, "policy_version", _pin("policy_version", self.policy_version))
        kbs = self.knowledge_base_versions
        if isinstance(kbs, str) or not isinstance(kbs, Iterable):
            raise ManifestError(
                BAD_PIN, "knowledge_base_versions must be a list of KnowledgeBasePin"
            )
        kbs = tuple(kbs)  # a generator is consumed once: take it here, before anything looks at it
        if len(kbs) > _MAX_ITEMS:
            raise ManifestError(
                BAD_PIN, f"knowledge_base_versions lists more than {_MAX_ITEMS} entries"
            )
        if not all(isinstance(kb, KnowledgeBasePin) for kb in kbs):
            raise ManifestError(
                BAD_PIN, "knowledge_base_versions must hold KnowledgeBasePin values"
            )
        ordered = tuple(sorted(kbs, key=lambda kb: (kb.knowledge_base_id, kb.version_id)))
        if len(set(ordered)) != len(ordered):
            raise ManifestError(
                DUPLICATE_PIN, "a knowledge base version is pinned twice: name each version once"
            )
        set_(self, "knowledge_base_versions", ordered)

    def to_manifest(self) -> dict[str, Any]:
        """The ``pins`` object of the ``run_manifest`` v1 schema."""
        return {
            "workflow_version": self.workflow_version,
            "bundle_digest": self.bundle_digest,
            "prompt_versions": list(self.prompt_versions),
            "tool_versions": list(self.tool_versions),
            "skill_versions": list(self.skill_versions),
            "knowledge_base_versions": [
                {"knowledge_base_id": kb.knowledge_base_id, "version_id": kb.version_id}
                for kb in self.knowledge_base_versions
            ],
            "model_routes": list(self.model_routes),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class DeploymentScope:
    """The server-derived scope of a run: organization, project, region and residency. Never taken from a caller."""

    tenant_id: str
    project_id: str | None
    region: str
    residency: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "region", "residency"):
            _text(BAD_SCOPE, name, getattr(self, name))
        if self.project_id is not None:
            _text(BAD_SCOPE, "project_id", self.project_id)

    @classmethod
    def fixed(cls, project_id: str | None) -> DeploymentScope:
        """The one configured deployment (ADR 0001) for ``project_id`` (``None`` for a run outside any project)."""
        return cls(FIXED_TENANT_ID, project_id, FIXED_REGION, FIXED_RESIDENCY)


@dataclass(frozen=True)
class PinnedRuleBundle:
    """A rule bundle whose exact bytes were verified against the digest the operator pinned, and where it came from.

    Make one per admission with :func:`pin_rule_bundle`: it is a snapshot, and one held for long records the digest of
    the bytes as they were then, not of the file as it is now. ``path`` is absolute so a later re-verification
    does not depend on the working directory.
    """

    path: str
    bundle: RuleBundle

    def __post_init__(self) -> None:
        _text(BAD_FIELD, "rule bundle path", self.path)
        if not PurePath(self.path).is_absolute():
            raise ManifestError(BAD_FIELD, "the rule bundle path must be absolute")
        if not isinstance(self.bundle, RuleBundle):
            raise ManifestError(BAD_FIELD, "bundle must be a verified RuleBundle")
        if type(self.bundle.sha256) is not str or not _DIGEST.fullmatch(self.bundle.sha256):
            raise ManifestError(BAD_FIELD, "the bundle's digest must be a full lowercase SHA-256")


def pin_rule_bundle(path: str | Path, expected_sha256: str) -> PinnedRuleBundle:
    """Load and verify the configured rule bundle (#726) before any pin is final: missing, oversized, changed,
    malformed or unreadable bytes, and a path that is not a path, refuse admission (``rule_bundle_refused``, with
    the loader's own code in the message). The recorded path is resolved to an absolute one."""
    if not isinstance(path, (str, Path)) or not str(path):
        raise ManifestError(RULE_BUNDLE_REFUSED, "the rule bundle path must be a non-empty path")
    try:
        resolved = Path(path).resolve()
        bundle = load_rule_bundle(resolved, expected_sha256)
    except RuleBundleError as exc:
        raise ManifestError(RULE_BUNDLE_REFUSED, f"rule bundle refused: {exc}") from exc
    except (
        OSError,
        ValueError,
        TypeError,
    ) as exc:  # an unreadable file, an embedded NUL, a bad digest type
        raise ManifestError(
            RULE_BUNDLE_REFUSED, f"rule bundle could not be read: {type(exc).__name__}"
        ) from exc
    return PinnedRuleBundle(path=str(resolved), bundle=bundle)


@dataclass(frozen=True)
class BuiltManifest:
    """The result: the canonical bytes of the validated record and the SHA-256 of those bytes.

    The record is derived from the bytes on each access, so a caller that edits it edits a copy: what is stored and
    what is digested cannot drift apart.
    """

    manifest_bytes: bytes
    manifest_digest: str

    def __post_init__(self) -> None:
        read_manifest(
            self.manifest_bytes, self.manifest_digest
        )  # bytes, digest and schema must agree

    @property
    def record(self) -> dict[str, Any]:
        record: dict[str, Any] = json.loads(self.manifest_bytes)
        return record

    @property
    def manifest_id(self) -> str:
        return str(self.record["manifest_id"])

    @property
    def workflow_run_id(self) -> str:
        return str(self.record["workflow_run_id"])

    def row_values(self) -> dict[str, Any]:
        """The columns of ``caliber_run_manifests`` this manifest fills; ``created_at`` is the record's own time."""
        created = datetime.fromisoformat(str(self.record["created_at"]).replace("Z", "+00:00"))
        return {
            "manifest_id": self.manifest_id,
            "workflow_run_id": self.workflow_run_id,
            "schema_version": SCHEMA_VERSION,
            "manifest_bytes": self.manifest_bytes,
            "manifest_digest": self.manifest_digest,
            "created_at": created.astimezone(timezone.utc).replace(tzinfo=None),
        }


def new_manifest_id() -> str:
    """A fresh manifest id. Impure on purpose and so kept out of :func:`build_run_manifest`."""
    return f"mf-{uuid.uuid4().hex}"


def canonical_bytes(record: Mapping[str, Any]) -> bytes:
    """The canonical JSON of ``record``: sorted keys, no whitespace, UTF-8, and no ``NaN`` or ``Infinity``."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest_of(data: bytes) -> str:
    """The lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ManifestError(BAD_FIELD, "created_at must be a timezone-aware datetime")
    try:
        utc = value.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ManifestError(BAD_FIELD, "created_at is outside the representable range") from exc
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_run_manifest(
    *,
    manifest_id: str,
    workflow_run_id: str,
    pins: ResolvedPins,
    subject: Subject,
    workload: Workload,
    purpose: str,
    scope: DeploymentScope,
    created_at: datetime,
    requested_tenant_id: str | None = None,
    rule_bundle: PinnedRuleBundle | None = None,
    rule_bundles_enabled: bool = False,
) -> BuiltManifest:
    """Build and validate the manifest of one run. Pure: raises :class:`ManifestError`, otherwise returns bytes.

    ``scope`` is the server's; ``requested_tenant_id`` is what the caller asked for, if anything, and must equal it
    (no organization switching). ``subject.project_id`` must equal ``scope.project_id``. When rule bundles are
    ``enabled`` a verified :class:`PinnedRuleBundle` is required and its digest and path are recorded; when they are
    not, passing one is refused rather than silently ignored.
    """
    if not isinstance(pins, ResolvedPins):
        raise ManifestError(BAD_PIN, "pins must be ResolvedPins")
    if not isinstance(subject, Subject) or not isinstance(workload, Workload):
        raise ManifestError(
            BAD_FIELD, "subject and workload must be typed Subject and Workload values"
        )
    if not isinstance(scope, DeploymentScope):
        raise ManifestError(BAD_SCOPE, "scope must be a DeploymentScope")
    if scope != DeploymentScope.fixed(scope.project_id):
        raise ManifestError(
            BAD_SCOPE,
            f"this deployment is fixed to organization {FIXED_TENANT_ID!r}, region {FIXED_REGION!r} "
            f"and residency {FIXED_RESIDENCY!r}; the scope names others",
        )
    if requested_tenant_id is not None and requested_tenant_id != scope.tenant_id:
        raise ManifestError(
            ORGANIZATION_SWITCH,
            "this deployment serves one organization; the caller cannot choose another",
        )
    if subject.project_id != scope.project_id:
        raise ManifestError(
            PROJECT_MISMATCH, "the subject acts in a different project than the run's scope"
        )
    if rule_bundle is not None and not rule_bundles_enabled:
        raise ManifestError(
            RULE_BUNDLE_UNEXPECTED, "a rule bundle was given but rule bundles are not enabled"
        )
    if rule_bundle is None and rule_bundles_enabled:
        raise ManifestError(
            RULE_BUNDLE_REQUIRED,
            "rule bundles are enabled: load and verify the bundle (pin_rule_bundle) first",
        )
    if rule_bundle is not None and not isinstance(rule_bundle, PinnedRuleBundle):
        raise ManifestError(RULE_BUNDLE_REQUIRED, "rule_bundle must be a PinnedRuleBundle")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": _text(BAD_FIELD, "manifest_id", manifest_id, limit=_MAX_ID),
        "workflow_run_id": _text(BAD_FIELD, "workflow_run_id", workflow_run_id, limit=_MAX_ID),
        "tenant_id": scope.tenant_id,
        "region": scope.region,
        "residency": scope.residency,
        "pins": pins.to_manifest(),
        "subject": subject.to_manifest(),
        "workload": workload.to_manifest(),
        "purpose": _text(BAD_FIELD, "purpose", purpose),
        "legacy": False,
        "rule_bundle_path": rule_bundle.path if rule_bundle is not None else None,
        "rule_bundle_sha256": rule_bundle.bundle.sha256 if rule_bundle is not None else None,
        "created_at": _timestamp(created_at),
    }
    if scope.project_id is not None:
        record["project_id"] = scope.project_id
    return _finish(record)


def _finish(record: dict[str, Any]) -> BuiltManifest:
    try:
        validate_record(KIND, record)
    except (
        ContractValidationError
    ) as exc:  # pragma: no cover - the typed inputs above make this unreachable
        raise ManifestError(INVALID_MANIFEST, str(exc)) from exc
    data = canonical_bytes(record)
    return BuiltManifest(manifest_bytes=data, manifest_digest=digest_of(data))


def read_manifest(manifest_bytes: bytes, manifest_digest: str | None = None) -> dict[str, Any]:
    """Read a stored manifest back: check the digest when given, parse the bytes and validate against ADR 0003.

    Accepts any record the v1 schema accepts, including those written before the builder was strict (the
    ``valid_*`` contract fixtures), pretty-printed or not. The bytes must be UTF-8 without a byte-order mark, at most
    1 MiB, with no duplicate key, ``NaN`` or lone surrogate. Raises :class:`ManifestError` ``invalid_manifest`` for
    anything else, and nothing else.
    """
    if type(manifest_bytes) is not bytes or not manifest_bytes:
        raise ManifestError(INVALID_MANIFEST, "there are no manifest bytes to read")
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ManifestError(INVALID_MANIFEST, "the manifest is larger than 1 MiB")
    if manifest_digest is not None and digest_of(manifest_bytes) != manifest_digest:
        raise ManifestError(INVALID_MANIFEST, "the manifest bytes do not match their digest")
    try:
        record = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
        )
        validate_record(KIND, record)
        canonical_bytes(record)  # a lone surrogate parses, but can be neither stored nor digested
    except (
        ValueError,  # includes JSON, Unicode and contract errors
        RecursionError,
    ) as exc:
        raise ManifestError(INVALID_MANIFEST, f"the manifest is not valid: {exc}") from exc
    if not isinstance(record, dict):  # unreachable: validate_record refuses anything but an object
        raise ManifestError(INVALID_MANIFEST, "the manifest must be an object")  # pragma: no cover
    return record


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice")
    return dict(pairs)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


__all__ = [
    "CODES",
    "FIXED_REGION",
    "FIXED_RESIDENCY",
    "FIXED_TENANT_ID",
    "FLOATING",
    "BuiltManifest",
    "DeploymentScope",
    "KnowledgeBasePin",
    "ManifestError",
    "PinnedRuleBundle",
    "ResolvedPins",
    "build_run_manifest",
    "canonical_bytes",
    "digest_of",
    "new_manifest_id",
    "pin_rule_bundle",
    "read_manifest",
]
