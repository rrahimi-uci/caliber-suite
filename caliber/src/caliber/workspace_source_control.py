"""Provider-neutral source-control contracts for Workspace integration.

The Workspace service must not know whether authored source lives in GitHub,
GitLab, Bitbucket, or a local test double.  This module is the narrow boundary
between that service and an adapter.  Values crossing the boundary are
normalized, immutable, and deliberately free of provider SDK response types.

This module provides the contract and an in-memory fake for deterministic tests.
Durable webhook/event and actor-link persistence lives in
``caliber.workspace_source_events``; provider credentials and network I/O stay
behind separate adapters such as ``caliber.github_source_control``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_NAMES = (
    "commit_tree_fetch",
    "commit_reachability",
    "change_request_lookup",
    "exact_head_reviews",
    "required_checks",
    "ruleset_observation",
    "webhook_verification",
    "status_publication",
)
_REVIEW_DECISIONS = frozenset({"approve", "request_changes", "comment"})
_ACTOR_KINDS = frozenset({"human", "bot", "unknown"})
_CHECK_CONCLUSIONS = frozenset(
    {"success", "failure", "neutral", "cancelled", "skipped", "pending", "unknown"}
)
_STATUS_STATES = frozenset({"pending", "success", "failure", "error"})


class SourceControlError(RuntimeError):
    """Base error raised by a source-control adapter."""

    code = "source_control_error"


class SourceControlCapabilityError(SourceControlError):
    """The configured adapter cannot provide a required capability."""

    code = "source_control_capability_unavailable"


class SourceControlNotFoundError(SourceControlError):
    """A repository object required for verification does not exist."""

    code = "source_control_object_not_found"


class SourceControlVerificationError(SourceControlError):
    """A provider object cannot be normalized or verified."""

    code = "source_control_verification_failed"


class SourceControlWebhookError(SourceControlError):
    """A webhook is malformed, untrusted, or not bound to the repository."""

    code = "source_control_webhook_invalid"


def _text(value: str, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if not result and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    return result


def _sha(value: str, field: str) -> str:
    result = _text(value, field).lower()
    if not _SHA_RE.fullmatch(result):
        raise ValueError(f"{field} must be a full hexadecimal commit SHA")
    return result


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path(value: str, field: str = "path") -> str:
    result = _text(value, field).replace("\\", "/")
    if result.startswith("/") or any(part in {"", ".", ".."} for part in result.split("/")):
        raise ValueError(f"{field} must be a normalized repository-relative path")
    return result


def _paths(values: Sequence[str], field: str = "paths") -> tuple[str, ...]:
    return tuple(sorted({_path(value, field) for value in values}))


def _identities(values: Sequence[str], field: str) -> tuple[str, ...]:
    return tuple(sorted({_text(value, field) for value in values}))


def _optional_sha(value: str | None, field: str) -> str | None:
    return None if value is None else _sha(value, field)


def _root_path(value: str) -> str:
    if not value:
        return ""
    return _path(value, "root_path")


def _in_root(path: str, root_path: str) -> bool:
    return not root_path or path == root_path or path.startswith(f"{root_path}/")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


@dataclass(frozen=True)
class SourceControlCapabilities:
    """Closed capability projection exposed by a provider adapter."""

    commit_tree_fetch: bool = False
    commit_reachability: bool = False
    change_request_lookup: bool = False
    exact_head_reviews: bool = False
    required_checks: bool = False
    ruleset_observation: bool = False
    webhook_verification: bool = False
    status_publication: bool = False

    def __post_init__(self) -> None:
        for name in _CAPABILITY_NAMES:
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in _CAPABILITY_NAMES}

    def require(self, capability: str) -> None:
        name = _text(capability, "capability")
        if name not in _CAPABILITY_NAMES:
            raise ValueError(f"unknown source-control capability: {name}")
        if not getattr(self, name):
            raise SourceControlCapabilityError(
                f"{self.__class__.__name__}: capability {name!r} is unavailable"
            )


@dataclass(frozen=True)
class SourceControlCommit:
    """Normalized immutable commit/tree observation."""

    sha: str
    parents: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    tree_sha256: str = ""
    author_id: str = ""
    author_kind: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha", _sha(self.sha, "sha"))
        object.__setattr__(
            self, "parents", tuple(_sha(parent, "parent SHA") for parent in self.parents)
        )
        object.__setattr__(self, "changed_paths", _paths(self.changed_paths, "changed path"))
        tree = _text(self.tree_sha256, "tree_sha256")
        if not _HEX64_RE.fullmatch(tree.lower()):
            raise ValueError("tree_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "tree_sha256", tree.lower())
        object.__setattr__(self, "author_id", _text(self.author_id, "author_id", allow_empty=True))
        kind = _text(self.author_kind, "author_kind")
        if kind not in _ACTOR_KINDS:
            raise ValueError(f"unsupported author_kind: {kind}")
        object.__setattr__(self, "author_kind", kind)


@dataclass(frozen=True)
class SourceControlComparison:
    """The complete normalized source delta between two reachable commits."""

    base_commit: str
    head_commit: str
    commits: tuple[SourceControlCommit, ...]
    changed_paths: tuple[str, ...]
    head_tree_sha256: str
    coverage_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_commit", _sha(self.base_commit, "base_commit"))
        object.__setattr__(self, "head_commit", _sha(self.head_commit, "head_commit"))
        object.__setattr__(self, "changed_paths", _paths(self.changed_paths, "changed path"))
        tree = _text(self.head_tree_sha256, "head_tree_sha256").lower()
        if not _HEX64_RE.fullmatch(tree):
            raise ValueError("head_tree_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "head_tree_sha256", tree)
        coverage = _text(self.coverage_digest, "coverage_digest").lower()
        if not _HEX64_RE.fullmatch(coverage):
            raise ValueError("coverage_digest must be a 64-character hexadecimal digest")
        object.__setattr__(self, "coverage_digest", coverage)


@dataclass(frozen=True)
class SourceControlChangeRequest:
    """Provider change-request coordinates, without provider SDK fields."""

    request_id: str
    repository_id: str
    base_commit: str
    head_commit: str
    resulting_commit: str | None = None
    merged: bool = False
    merge_method: str | None = None
    merge_actor_id: str | None = None
    provider_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        object.__setattr__(self, "repository_id", _text(self.repository_id, "repository_id"))
        object.__setattr__(self, "base_commit", _sha(self.base_commit, "base_commit"))
        object.__setattr__(self, "head_commit", _sha(self.head_commit, "head_commit"))
        object.__setattr__(
            self, "resulting_commit", _optional_sha(self.resulting_commit, "resulting_commit")
        )
        if not isinstance(self.merged, bool):
            raise ValueError("merged must be a boolean")
        object.__setattr__(
            self,
            "merge_method",
            None if self.merge_method is None else _text(self.merge_method, "merge_method"),
        )
        object.__setattr__(
            self,
            "merge_actor_id",
            None if self.merge_actor_id is None else _text(self.merge_actor_id, "merge_actor_id"),
        )
        object.__setattr__(
            self, "provider_event_ids", _identities(self.provider_event_ids, "event ID")
        )


@dataclass(frozen=True)
class SourceControlReview:
    """A normalized provider review; actor eligibility is CALIBER policy input."""

    review_id: str
    actor_id: str
    actor_kind: str
    decision: str
    commit_sha: str
    submitted_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "review_id", _text(self.review_id, "review_id"))
        object.__setattr__(self, "actor_id", _text(self.actor_id, "actor_id"))
        kind = _text(self.actor_kind, "actor_kind")
        if kind not in _ACTOR_KINDS:
            raise ValueError(f"unsupported actor_kind: {kind}")
        object.__setattr__(self, "actor_kind", kind)
        decision = _text(self.decision, "decision")
        if decision not in _REVIEW_DECISIONS:
            raise ValueError(f"unsupported review decision: {decision}")
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "commit_sha", _sha(self.commit_sha, "commit_sha"))


@dataclass(frozen=True)
class SourceControlCheck:
    """A normalized required-check observation."""

    name: str
    source_id: str
    commit_sha: str
    conclusion: str
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "check name"))
        object.__setattr__(self, "source_id", _text(self.source_id, "check source_id"))
        object.__setattr__(self, "commit_sha", _sha(self.commit_sha, "check commit_sha"))
        conclusion = _text(self.conclusion, "check conclusion")
        if conclusion not in _CHECK_CONCLUSIONS:
            raise ValueError(f"unsupported check conclusion: {conclusion}")
        object.__setattr__(self, "conclusion", conclusion)


@dataclass(frozen=True)
class SourceControlVerificationPolicy:
    """CALIBER-owned policy inputs consumed by a provider adapter."""

    required_checks: tuple[str, ...] = ()
    trusted_check_sources: tuple[str, ...] = ()
    required_approvals: int = 1
    eligible_reviewer_ids: tuple[str, ...] = ()
    require_complete_range: bool = True
    require_merged: bool = False
    expected_ruleset_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_checks", _identities(self.required_checks, "check name"))
        object.__setattr__(
            self,
            "trusted_check_sources",
            _identities(self.trusted_check_sources, "trusted check source"),
        )
        if not isinstance(self.required_approvals, int) or self.required_approvals < 0:
            raise ValueError("required_approvals must be a non-negative integer")
        object.__setattr__(
            self,
            "eligible_reviewer_ids",
            _identities(self.eligible_reviewer_ids, "eligible reviewer ID"),
        )
        if not isinstance(self.require_complete_range, bool):
            raise ValueError("require_complete_range must be a boolean")
        if not isinstance(self.require_merged, bool):
            raise ValueError("require_merged must be a boolean")
        ruleset = self.expected_ruleset_sha256
        if ruleset is not None:
            ruleset = _text(ruleset, "expected_ruleset_sha256").lower()
            if not _HEX64_RE.fullmatch(ruleset):
                raise ValueError(
                    "expected_ruleset_sha256 must be a 64-character hexadecimal digest"
                )
        object.__setattr__(self, "expected_ruleset_sha256", ruleset)


@dataclass(frozen=True)
class SourceControlVerification:
    """Normalized, digestable review evidence returned by an adapter."""

    repository_id: str
    change_request_id: str
    base_commit: str
    head_commit: str
    resulting_commit: str | None
    source_tree_sha256: str
    policy: SourceControlVerificationPolicy
    check_conclusions: Mapping[str, str]
    reviews: tuple[SourceControlReview, ...]
    status: str
    reason: str
    coverage_digest: str
    uncovered_commits: tuple[str, ...] = ()
    uncovered_paths: tuple[str, ...] = ()
    provider_event_ids: tuple[str, ...] = ()
    ruleset_sha256: str | None = None
    adapter_version: str = ""
    verification_input_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository_id", _text(self.repository_id, "repository_id"))
        object.__setattr__(
            self, "change_request_id", _text(self.change_request_id, "change_request_id")
        )
        object.__setattr__(self, "base_commit", _sha(self.base_commit, "base_commit"))
        object.__setattr__(self, "head_commit", _sha(self.head_commit, "head_commit"))
        object.__setattr__(
            self, "resulting_commit", _optional_sha(self.resulting_commit, "resulting_commit")
        )
        tree = _text(self.source_tree_sha256, "source_tree_sha256").lower()
        if not _HEX64_RE.fullmatch(tree):
            raise ValueError("source_tree_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "source_tree_sha256", tree)
        if self.status not in {"verified", "insufficient", "stale", "revoked"}:
            raise ValueError(f"unsupported verification status: {self.status}")
        object.__setattr__(self, "reason", _text(self.reason, "reason", allow_empty=True))
        object.__setattr__(
            self, "coverage_digest", _sha256_digest(self.coverage_digest, "coverage_digest")
        )
        object.__setattr__(
            self,
            "uncovered_commits",
            tuple(_sha(value, "uncovered commit") for value in self.uncovered_commits),
        )
        object.__setattr__(self, "uncovered_paths", _paths(self.uncovered_paths, "uncovered path"))
        object.__setattr__(
            self, "provider_event_ids", _identities(self.provider_event_ids, "event ID")
        )
        ruleset = self.ruleset_sha256
        if ruleset is not None:
            ruleset = _text(ruleset, "ruleset_sha256").lower()
            if not _HEX64_RE.fullmatch(ruleset):
                raise ValueError("ruleset_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "ruleset_sha256", ruleset)
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version"))
        digest = _text(self.verification_input_digest, "verification_input_digest").lower()
        if not _HEX64_RE.fullmatch(digest):
            raise ValueError("verification_input_digest must be a 64-character digest")
        object.__setattr__(self, "verification_input_digest", digest)

    def as_external_attestation(
        self,
        *,
        source_id: str,
        head_id: str,
        workspace_revision_sha256: str,
        policy_version: str,
        policy_sha256: str,
        provider_url: str | None = None,
    ) -> dict[str, object]:
        """Project evidence into the existing P4-D service input shape."""

        return {
            "source_id": _text(source_id, "source_id"),
            "head_id": _text(head_id, "head_id"),
            "provider_change_request_id": self.change_request_id,
            "provider_url": provider_url,
            "provider_head_commit": self.head_commit,
            "provider_resulting_commit": self.resulting_commit or self.head_commit,
            "source_tree_sha256": self.source_tree_sha256,
            "workspace_revision_sha256": _text(
                workspace_revision_sha256, "workspace_revision_sha256"
            ),
            "policy_version": _text(policy_version, "policy_version"),
            "policy_sha256": _text(policy_sha256, "policy_sha256"),
            "provider_ruleset_sha256": self.ruleset_sha256,
            "required_checks": list(self.policy.required_checks),
            "trusted_check_sources": list(self.policy.trusted_check_sources),
            "check_conclusions": dict(self.check_conclusions),
            "review_actors": [
                {
                    "review_id": review.review_id,
                    "actor_id": review.actor_id,
                    "actor_kind": review.actor_kind,
                    "decision": review.decision,
                    "commit_sha": review.commit_sha,
                }
                for review in self.reviews
            ],
            "provider_event_ids": list(self.provider_event_ids),
            "adapter_version": self.adapter_version,
            "status": self.status,
            "reason": self.reason,
            "verification_input_digest": self.verification_input_digest,
            "coverage_digest": self.coverage_digest,
            "uncovered_commits": list(self.uncovered_commits),
            "uncovered_paths": list(self.uncovered_paths),
        }


def _sha256_digest(value: str, field: str) -> str:
    result = _text(value, field).lower()
    if not _HEX64_RE.fullmatch(result):
        raise ValueError(f"{field} must be a 64-character hexadecimal digest")
    return result


@dataclass(frozen=True)
class SourceControlWebhook:
    """Normalized webhook envelope; raw provider payloads do not cross the boundary."""

    delivery_id: str
    event_type: str
    repository_id: str
    payload_sha256: str
    signature_verified: bool
    action: str | None = None
    head_commit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_id", _text(self.delivery_id, "delivery_id"))
        object.__setattr__(self, "event_type", _text(self.event_type, "event_type"))
        object.__setattr__(self, "repository_id", _text(self.repository_id, "repository_id"))
        object.__setattr__(
            self, "payload_sha256", _sha256_digest(self.payload_sha256, "payload_sha256")
        )
        if not isinstance(self.signature_verified, bool):
            raise ValueError("signature_verified must be a boolean")
        object.__setattr__(
            self, "action", None if self.action is None else _text(self.action, "action")
        )
        object.__setattr__(self, "head_commit", _optional_sha(self.head_commit, "head_commit"))


@dataclass(frozen=True)
class SourceControlStatus:
    """Normalized optional status publication result."""

    repository_id: str
    commit_sha: str
    state: str
    description: str
    status_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository_id", _text(self.repository_id, "repository_id"))
        object.__setattr__(self, "commit_sha", _sha(self.commit_sha, "commit_sha"))
        state = _text(self.state, "state")
        if state not in _STATUS_STATES:
            raise ValueError(f"unsupported status state: {state}")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "status_id", _text(self.status_id, "status_id"))


def verify_normalized_change_request(
    *,
    repository_id: str,
    request: SourceControlChangeRequest,
    comparison: SourceControlComparison,
    reviews: tuple[SourceControlReview, ...],
    checks: tuple[SourceControlCheck, ...],
    policy: SourceControlVerificationPolicy,
    expected_head_commit: str,
    expected_base_commit: str | None = None,
    expected_resulting_commit: str | None = None,
    covered_commits: Sequence[str] = (),
    covered_paths: Sequence[str] = (),
    ruleset_sha256: str | None = None,
    adapter_version: str = "",
) -> SourceControlVerification:
    """Evaluate normalized provider evidence without provider-specific logic."""

    repository = _text(repository_id, "repository_id")
    expected_head = _sha(expected_head_commit, "expected_head_commit")
    expected_base = _optional_sha(expected_base_commit, "expected_base_commit")
    expected_resulting = _optional_sha(expected_resulting_commit, "expected_resulting_commit")
    normalized_covered_commits = tuple(_sha(value, "covered commit") for value in covered_commits)
    normalized_covered_paths = _paths(covered_paths, "covered path")
    stale = (
        request.head_commit != expected_head
        or (expected_base is not None and request.base_commit != expected_base)
        or (expected_resulting is not None and request.resulting_commit != expected_resulting)
    )
    uncovered_commits = tuple(
        sorted({commit.sha for commit in comparison.commits} - set(normalized_covered_commits))
    )
    uncovered_paths = tuple(sorted(set(comparison.changed_paths) - set(normalized_covered_paths)))
    reasons: list[str] = []
    if stale:
        reasons.append("change request coordinates do not match the expected immutable head")
    if policy.require_complete_range and (uncovered_commits or uncovered_paths):
        reasons.append("provider review does not cover the complete source range")
    if policy.require_merged and not request.merged:
        reasons.append("change request is not merged")
    if policy.require_merged and request.merged and request.resulting_commit is None:
        reasons.append("merged change request has no resulting commit")
    approvals = {
        review.actor_id
        for review in reviews
        if review.decision == "approve"
        and review.actor_kind == "human"
        and review.commit_sha == request.head_commit
        and bool(policy.eligible_reviewer_ids)
        and review.actor_id in policy.eligible_reviewer_ids
    }
    if len(approvals) < policy.required_approvals:
        reasons.append("required eligible human approvals are incomplete")
    check_conclusions: dict[str, str] = {}
    for required_name in policy.required_checks:
        trusted = [
            check
            for check in checks
            if check.name == required_name
            and check.commit_sha == request.head_commit
            and check.source_id in policy.trusted_check_sources
            and check.conclusion == "success"
        ]
        if trusted:
            check_conclusions[required_name] = trusted[-1].conclusion
        else:
            check_conclusions[required_name] = "missing"
            reasons.append(f"required check {required_name!r} is not trusted and successful")
    ruleset = None if ruleset_sha256 is None else _sha256_digest(ruleset_sha256, "ruleset_sha256")
    if policy.expected_ruleset_sha256 is not None and ruleset != policy.expected_ruleset_sha256:
        reasons.append("observed provider ruleset does not match the policy snapshot")
    status = "stale" if stale else "verified" if not reasons else "insufficient"
    reason = "; ".join(dict.fromkeys(reasons))
    input_digest = _digest(
        {
            "request": request,
            "policy": policy,
            "reviews": reviews,
            "checks": checks,
            "coverage_digest": comparison.coverage_digest,
            "covered_commits": normalized_covered_commits,
            "covered_paths": normalized_covered_paths,
            "ruleset_sha256": ruleset,
        }
    )
    return SourceControlVerification(
        repository_id=repository,
        change_request_id=request.request_id,
        base_commit=request.base_commit,
        head_commit=request.head_commit,
        resulting_commit=request.resulting_commit,
        source_tree_sha256=comparison.head_tree_sha256,
        policy=policy,
        check_conclusions=check_conclusions,
        reviews=reviews,
        status=status,
        reason=reason,
        coverage_digest=comparison.coverage_digest,
        uncovered_commits=uncovered_commits,
        uncovered_paths=uncovered_paths,
        provider_event_ids=request.provider_event_ids,
        ruleset_sha256=ruleset,
        adapter_version=_text(adapter_version, "adapter_version"),
        verification_input_digest=input_digest,
    )


class SourceControlProvider(Protocol):
    """The provider-neutral interface implemented by source-control adapters."""

    name: str
    adapter_version: str

    def capabilities(self) -> SourceControlCapabilities: ...

    def canonical_repository_id(self, repository: str) -> str: ...

    def fetch_commit(self, repository_id: str, commit_sha: str) -> SourceControlCommit: ...

    def compare_commits(
        self, repository_id: str, base_commit: str, head_commit: str, *, root_path: str = ""
    ) -> SourceControlComparison: ...

    def get_change_request(
        self, repository_id: str, change_request_id: str
    ) -> SourceControlChangeRequest: ...

    def verify_change_request(
        self,
        repository_id: str,
        change_request_id: str,
        *,
        policy: SourceControlVerificationPolicy,
        expected_head_commit: str,
        expected_base_commit: str | None = None,
        expected_resulting_commit: str | None = None,
        root_path: str = "",
    ) -> SourceControlVerification: ...

    def verify_webhook(
        self, repository_id: str, headers: Mapping[str, str], payload: bytes
    ) -> SourceControlWebhook: ...

    def publish_status(
        self, repository_id: str, commit_sha: str, *, state: str, description: str
    ) -> SourceControlStatus: ...


class SourceControlProviderRegistry:
    """Explicit adapter registry; it never constructs network clients."""

    def __init__(self, providers: Mapping[str, SourceControlProvider] | None = None) -> None:
        self._providers: dict[str, SourceControlProvider] = {}
        for provider in (providers or {}).values():
            self.register(provider)

    def register(self, provider: SourceControlProvider, *, replace: bool = False) -> None:
        name = _text(provider.name, "source-control provider name").lower()
        if name in self._providers and not replace:
            raise ValueError(f"source-control provider {name!r} is already registered")
        self._providers[name] = provider

    def get(self, provider_name: str) -> SourceControlProvider | None:
        name = _text(provider_name, "provider_name").lower()
        return self._providers.get(name)

    def require(self, provider_name: str) -> SourceControlProvider:
        provider = self.get(provider_name)
        if provider is None:
            raise SourceControlCapabilityError(
                f"{SourceControlCapabilityError.code}: no adapter is installed for "
                f"provider {provider_name!r}"
            )
        return provider

    def capabilities(self, provider_name: str) -> SourceControlCapabilities | None:
        provider = self.get(provider_name)
        if provider is None:
            return None
        try:
            return provider.capabilities()
        except SourceControlError:
            return None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class FakeSourceControlProvider:
    """Deterministic offline adapter for contract and integration tests."""

    def __init__(
        self,
        *,
        name: str = "fake",
        webhook_secret: bytes = b"caliber-test-secret",
        capabilities: SourceControlCapabilities | None = None,
        adapter_version: str = "fake-source-control/1",
    ) -> None:
        self.name = _text(name, "name").lower()
        if not isinstance(webhook_secret, bytes) or not webhook_secret:
            raise ValueError("webhook_secret must be non-empty bytes")
        self._webhook_secret = webhook_secret
        self._capabilities = capabilities or SourceControlCapabilities(
            commit_tree_fetch=True,
            commit_reachability=True,
            change_request_lookup=True,
            exact_head_reviews=True,
            required_checks=True,
            ruleset_observation=True,
            webhook_verification=True,
            status_publication=True,
        )
        self.adapter_version = _text(adapter_version, "adapter_version")
        self._commits: dict[str, dict[str, SourceControlCommit]] = {}
        self._requests: dict[tuple[str, str], SourceControlChangeRequest] = {}
        self._coverage: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._reviews: dict[tuple[str, str], list[SourceControlReview]] = {}
        self._checks: dict[tuple[str, str], list[SourceControlCheck]] = {}
        self._rulesets: dict[str, str] = {}

    def capabilities(self) -> SourceControlCapabilities:
        return self._capabilities

    def _require(self, capability: str) -> None:
        self._capabilities.require(capability)

    def canonical_repository_id(self, repository: str) -> str:
        value = _text(repository, "repository").replace("\\", "/").strip("/").lower()
        prefix = f"{self.name}:"
        if value.startswith(prefix):
            value = value[len(prefix) :].strip("/")
        if not value:
            raise ValueError("repository must contain a canonical path")
        return f"{self.name}:{value}"

    def add_commit(
        self,
        repository: str,
        sha: str,
        *,
        parents: Sequence[str] = (),
        changed_paths: Sequence[str] = (),
        tree_sha256: str | None = None,
        author_id: str = "developer",
        author_kind: str = "human",
    ) -> SourceControlCommit:
        repository_id = self.canonical_repository_id(repository)
        normalized_sha = _sha(sha, "sha")
        commit = SourceControlCommit(
            sha=normalized_sha,
            parents=tuple(parents),
            changed_paths=tuple(changed_paths),
            tree_sha256=tree_sha256
            or _digest({"repository": repository_id, "sha": normalized_sha}),
            author_id=author_id,
            author_kind=author_kind,
        )
        self._commits.setdefault(repository_id, {})[commit.sha] = commit
        return commit

    def add_change_request(
        self,
        repository: str,
        request_id: str,
        *,
        base_commit: str,
        head_commit: str,
        resulting_commit: str | None = None,
        merged: bool = False,
        merge_method: str | None = None,
        merge_actor_id: str | None = None,
        covered_commit_shas: Sequence[str] | None = None,
        covered_paths: Sequence[str] | None = None,
        provider_event_ids: Sequence[str] = (),
    ) -> SourceControlChangeRequest:
        repository_id = self.canonical_repository_id(repository)
        request = SourceControlChangeRequest(
            request_id=request_id,
            repository_id=repository_id,
            base_commit=base_commit,
            head_commit=head_commit,
            resulting_commit=resulting_commit,
            merged=merged,
            merge_method=merge_method,
            merge_actor_id=merge_actor_id,
            provider_event_ids=tuple(provider_event_ids),
        )
        comparison = self.compare_commits(repository_id, request.base_commit, request.head_commit)
        commits = tuple(sorted(commit.sha for commit in comparison.commits))
        paths = comparison.changed_paths
        self._requests[(repository_id, request.request_id)] = request
        self._coverage[(repository_id, request.request_id)] = (
            commits
            if covered_commit_shas is None
            else tuple(_sha(value, "covered commit") for value in covered_commit_shas),
            paths if covered_paths is None else _paths(covered_paths, "covered path"),
        )
        self._reviews.setdefault((repository_id, request.request_id), [])
        self._checks.setdefault((repository_id, request.request_id), [])
        return request

    def add_review(self, repository: str, request_id: str, review: SourceControlReview) -> None:
        repository_id = self.canonical_repository_id(repository)
        key = (repository_id, _text(request_id, "change_request_id"))
        if key not in self._requests:
            raise SourceControlNotFoundError(f"unknown change request {request_id!r}")
        self._reviews.setdefault(key, []).append(review)

    def add_check(self, repository: str, request_id: str, check: SourceControlCheck) -> None:
        repository_id = self.canonical_repository_id(repository)
        key = (repository_id, _text(request_id, "change_request_id"))
        if key not in self._requests:
            raise SourceControlNotFoundError(f"unknown change request {request_id!r}")
        self._checks.setdefault(key, []).append(check)

    def set_ruleset(self, repository: str, ruleset_sha256: str | None) -> None:
        repository_id = self.canonical_repository_id(repository)
        self._rulesets[repository_id] = (
            _sha256_digest(ruleset_sha256, "ruleset_sha256") if ruleset_sha256 else ""
        )

    def fetch_commit(self, repository_id: str, commit_sha: str) -> SourceControlCommit:
        self._require("commit_tree_fetch")
        repository = self.canonical_repository_id(repository_id)
        commit = self._commits.get(repository, {}).get(_sha(commit_sha, "commit_sha"))
        if commit is None:
            raise SourceControlNotFoundError(f"unknown commit {commit_sha!r}")
        return commit

    def compare_commits(
        self, repository_id: str, base_commit: str, head_commit: str, *, root_path: str = ""
    ) -> SourceControlComparison:
        self._require("commit_tree_fetch")
        self._require("commit_reachability")
        repository = self.canonical_repository_id(repository_id)
        base = _sha(base_commit, "base_commit")
        head = _sha(head_commit, "head_commit")
        commits_by_sha = self._commits.get(repository)
        if commits_by_sha is None:
            raise SourceControlNotFoundError(f"unknown repository {repository_id!r}")
        root = _root_path(root_path)
        pending = [head]
        visited: set[str] = set()
        selected: list[SourceControlCommit] = []
        reached_base = False
        while pending:
            current_sha = pending.pop()
            if current_sha == base:
                reached_base = True
                continue
            if current_sha in visited:
                continue
            commit = commits_by_sha.get(current_sha)
            if commit is None:
                raise SourceControlNotFoundError(f"unknown commit {current_sha!r}")
            visited.add(current_sha)
            selected.append(commit)
            pending.extend(commit.parents)
        if not reached_base:
            raise SourceControlVerificationError(
                f"{self.__class__.__name__}: base commit is not reachable from head"
            )
        selected.sort(key=lambda commit: commit.sha)
        changed_paths = tuple(
            sorted(
                {
                    path
                    for commit in selected
                    for path in commit.changed_paths
                    if _in_root(path, root)
                }
            )
        )
        coverage_digest = _digest(
            {
                "base_commit": base,
                "head_commit": head,
                "commits": [commit.sha for commit in selected],
                "changed_paths": changed_paths,
            }
        )
        head_observation = commits_by_sha[head]
        return SourceControlComparison(
            base_commit=base,
            head_commit=head,
            commits=tuple(selected),
            changed_paths=changed_paths,
            head_tree_sha256=head_observation.tree_sha256,
            coverage_digest=coverage_digest,
        )

    def get_change_request(
        self, repository_id: str, change_request_id: str
    ) -> SourceControlChangeRequest:
        self._require("change_request_lookup")
        key = (
            self.canonical_repository_id(repository_id),
            _text(change_request_id, "change_request_id"),
        )
        request = self._requests.get(key)
        if request is None:
            raise SourceControlNotFoundError(f"unknown change request {change_request_id!r}")
        return request

    def verify_change_request(
        self,
        repository_id: str,
        change_request_id: str,
        *,
        policy: SourceControlVerificationPolicy,
        expected_head_commit: str,
        expected_base_commit: str | None = None,
        expected_resulting_commit: str | None = None,
        root_path: str = "",
    ) -> SourceControlVerification:
        self._require("change_request_lookup")
        self._require("exact_head_reviews")
        self._require("required_checks")
        self._require("commit_tree_fetch")
        self._require("commit_reachability")
        repository = self.canonical_repository_id(repository_id)
        request = self.get_change_request(repository, change_request_id)
        comparison = self.compare_commits(
            repository, request.base_commit, request.head_commit, root_path=root_path
        )
        covered_commits, covered_paths = self._coverage[(repository, request.request_id)]
        reviews = tuple(self._reviews[(repository, request.request_id)])
        checks = tuple(self._checks[(repository, request.request_id)])
        ruleset = self._rulesets.get(repository) or None
        return verify_normalized_change_request(
            repository_id=repository,
            request=request,
            comparison=comparison,
            reviews=reviews,
            checks=checks,
            policy=policy,
            expected_head_commit=expected_head_commit,
            expected_base_commit=expected_base_commit,
            expected_resulting_commit=expected_resulting_commit,
            covered_commits=covered_commits,
            covered_paths=covered_paths,
            ruleset_sha256=ruleset,
            adapter_version=self.adapter_version,
        )

    def verify_webhook(
        self, repository_id: str, headers: Mapping[str, str], payload: bytes
    ) -> SourceControlWebhook:
        self._require("webhook_verification")
        if not isinstance(payload, bytes):
            raise SourceControlWebhookError("webhook payload must be bytes")
        repository = self.canonical_repository_id(repository_id)
        delivery_id = _header(headers, "X-Source-Control-Delivery") or _header(
            headers, "X-GitHub-Delivery"
        )
        event_type = _header(headers, "X-Source-Control-Event") or _header(
            headers, "X-GitHub-Event"
        )
        signature = _header(headers, "X-Hub-Signature-256") or _header(
            headers, "X-Source-Control-Signature-256"
        )
        if not delivery_id or not event_type or not signature:
            raise SourceControlWebhookError(
                "webhook delivery, event, and signature headers are required"
            )
        expected = "sha256=" + hmac.new(self._webhook_secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SourceControlWebhookError("webhook signature verification failed")
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceControlWebhookError("webhook payload is not valid JSON") from exc
        if not isinstance(body, dict):
            raise SourceControlWebhookError("webhook payload must be a JSON object")
        body_repository = body.get("repository_id") or body.get("repository")
        if not isinstance(body_repository, str) or not body_repository.strip():
            raise SourceControlWebhookError("webhook repository binding is required")
        if self.canonical_repository_id(body_repository) != repository:
            raise SourceControlWebhookError("webhook repository does not match the source binding")
        head_commit = body.get("head_commit")
        try:
            normalized_head = None if head_commit is None else _sha(str(head_commit), "head_commit")
        except ValueError as exc:
            raise SourceControlWebhookError("webhook head_commit is invalid") from exc
        return SourceControlWebhook(
            delivery_id=delivery_id,
            event_type=event_type,
            repository_id=repository,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            signature_verified=True,
            action=None if body.get("action") is None else str(body["action"]),
            head_commit=normalized_head,
        )

    def publish_status(
        self, repository_id: str, commit_sha: str, *, state: str, description: str
    ) -> SourceControlStatus:
        self._require("status_publication")
        repository = self.canonical_repository_id(repository_id)
        commit = self.fetch_commit(repository, commit_sha)
        normalized_state = _text(state, "state")
        if normalized_state not in _STATUS_STATES:
            raise ValueError(f"unsupported status state: {normalized_state}")
        normalized_description = _text(description, "description")
        status_id = _digest(
            {
                "repository_id": repository,
                "commit_sha": commit.sha,
                "state": normalized_state,
                "description": normalized_description,
            }
        )
        return SourceControlStatus(
            repository_id=repository,
            commit_sha=commit.sha,
            state=normalized_state,
            description=normalized_description,
            status_id=status_id,
        )


__all__ = [
    "FakeSourceControlProvider",
    "SourceControlCapabilities",
    "SourceControlCapabilityError",
    "SourceControlChangeRequest",
    "SourceControlCheck",
    "SourceControlCommit",
    "SourceControlComparison",
    "SourceControlError",
    "SourceControlNotFoundError",
    "SourceControlProvider",
    "SourceControlProviderRegistry",
    "SourceControlReview",
    "SourceControlStatus",
    "SourceControlVerification",
    "SourceControlVerificationError",
    "SourceControlVerificationPolicy",
    "SourceControlWebhook",
    "SourceControlWebhookError",
    "verify_normalized_change_request",
]
