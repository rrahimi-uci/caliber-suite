"""Injectable GitHub App source-control adapter.

The adapter translates GitHub REST payloads into the provider-neutral values
from :mod:`caliber.workspace_source_control`.  It accepts an injected
transport and short-lived token/secret providers, so construction performs no
network I/O and tests never need GitHub credentials.  Connection persistence,
webhook inbox insertion, and CALIBER identity-link policy remain outside this
module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

from caliber.workspace_source_control import (
    SourceControlCapabilities,
    SourceControlChangeRequest,
    SourceControlCheck,
    SourceControlCommit,
    SourceControlComparison,
    SourceControlReview,
    SourceControlStatus,
    SourceControlVerification,
    SourceControlVerificationError,
    SourceControlVerificationPolicy,
    SourceControlWebhook,
    SourceControlWebhookError,
    verify_normalized_change_request,
)


class GitHubSourceControlError(RuntimeError):
    """Base error for normalized GitHub adapter failures."""

    code = "github_source_control_error"


class GitHubAuthenticationError(GitHubSourceControlError):
    """The injected short-lived credential provider could not provide a token."""

    code = "github_authentication_unavailable"


class GitHubTransportError(GitHubSourceControlError):
    """GitHub returned a non-success response or an unusable payload."""

    code = "github_transport_error"


@dataclass(frozen=True)
class GitHubResponse:
    """Minimal transport response; no httpx/requests type crosses the adapter."""

    status_code: int
    payload: object
    headers: Mapping[str, str] = field(default_factory=dict)


class GitHubTransport(Protocol):
    """Small injectable REST transport used by the adapter."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> GitHubResponse: ...


TokenProvider = Callable[[], str]
SecretProvider = Callable[[], bytes]
MIN_SHA_LENGTH = 40
MAX_SHA_LENGTH = 64
REPOSITORY_PART_COUNT = 2
HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _sha(value: str, field: str) -> str:
    result = _text(value, field).lower()
    if (
        len(result) < MIN_SHA_LENGTH
        or len(result) > MAX_SHA_LENGTH
        or any(char not in "0123456789abcdef" for char in result)
    ):
        raise ValueError(f"{field} must be a full hexadecimal commit SHA")
    return result


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubTransportError(f"GitHub {field} payload is not an object")
    return value


def _list(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise GitHubTransportError(f"GitHub {field} payload is not a list of objects")
    return list(value)


def _repository_parts(repository_id: str, host: str) -> tuple[str, str]:
    canonical = _text(repository_id, "repository_id")
    prefix = "github:"
    if canonical.lower().startswith(prefix):
        canonical = canonical[len(prefix) :]
    parts = canonical.strip("/").split("/")
    if len(parts) != REPOSITORY_PART_COUNT or any(
        not part or part in {".", ".."} for part in parts
    ):
        raise ValueError(f"repository_id must be github:owner/repository for host {host!r}")
    return parts[0].lower(), parts[1].lower()


def _tree_digest(tree_id: str) -> str:
    """Hash Git's tree object ID into the adapter's SHA-256 digest field."""

    return _digest({"github_tree_object_id": _text(tree_id, "tree object id")})


class GitHubSourceControlProvider:
    """GitHub REST adapter with explicit transport and credential injection."""

    name = "github"

    def __init__(
        self,
        transport: GitHubTransport,
        *,
        token_provider: TokenProvider,
        webhook_secret_provider: SecretProvider,
        host: str = "github.com",
        adapter_version: str = "github-rest/1",
        capabilities: SourceControlCapabilities | None = None,
    ) -> None:
        self._transport = transport
        self._token_provider = token_provider
        self._webhook_secret_provider = webhook_secret_provider
        self.host = _text(host, "host").lower()
        self.adapter_version = _text(adapter_version, "adapter_version")
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

    def capabilities(self) -> SourceControlCapabilities:
        return self._capabilities

    def _require(self, capability: str) -> None:
        self._capabilities.require(capability)

    def canonical_repository_id(self, repository: str) -> str:
        owner, name = _repository_parts(repository, self.host)
        return f"github:{owner}/{name}"

    def _path(self, repository_id: str, suffix: str) -> str:
        owner, name = _repository_parts(repository_id, self.host)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{suffix}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> GitHubResponse:
        token = self._token_provider()
        if not isinstance(token, str) or not token.strip():
            raise GitHubAuthenticationError("GitHub installation token is unavailable")
        response = self._transport.request(
            method,
            path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.strip()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params=params,
            json_body=json_body,
        )
        if not HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX:
            raise GitHubTransportError(f"GitHub request failed with status {response.status_code}")
        return response

    def fetch_commit(self, repository_id: str, commit_sha: str) -> SourceControlCommit:
        self._require("commit_tree_fetch")
        normalized_sha = _sha(commit_sha, "commit_sha")
        payload = _mapping(
            self._request(
                "GET", self._path(repository_id, f"/commits/{quote(normalized_sha)}")
            ).payload,
            "commit",
        )
        observed_sha = _sha(str(payload.get("sha", "")), "commit sha")
        if observed_sha != normalized_sha:
            raise SourceControlVerificationError(
                "GitHub commit response does not match the request"
            )
        commit_data = _mapping(payload.get("commit"), "commit metadata")
        tree_data = _mapping(commit_data.get("tree"), "commit tree")
        parents = tuple(
            _sha(str(parent.get("sha", "")), "parent SHA")
            for parent in _list(payload.get("parents", []), "commit parents")
        )
        files = _list(payload.get("files", []), "commit files")
        author = payload.get("author")
        author_data = _mapping(author, "commit author") if author is not None else {}
        author_type = str(author_data.get("type", "")).lower()
        author_kind = (
            "bot" if author_type == "bot" else "human" if author_type == "user" else "unknown"
        )
        return SourceControlCommit(
            sha=observed_sha,
            parents=parents,
            changed_paths=tuple(str(file.get("filename", "")) for file in files),
            tree_sha256=_tree_digest(str(tree_data.get("sha", ""))),
            author_id=str(author_data.get("login", "")),
            author_kind=author_kind,
        )

    def compare_commits(
        self, repository_id: str, base_commit: str, head_commit: str, *, root_path: str = ""
    ) -> SourceControlComparison:
        self._require("commit_tree_fetch")
        self._require("commit_reachability")
        base = _sha(base_commit, "base_commit")
        head = _sha(head_commit, "head_commit")
        payload = _mapping(
            self._request(
                "GET",
                self._path(repository_id, f"/compare/{quote(base)}...{quote(head)}"),
            ).payload,
            "comparison",
        )
        merge_base = _mapping(payload.get("merge_base_commit"), "merge base")
        if _sha(str(merge_base.get("sha", "")), "merge base SHA") != base:
            raise SourceControlVerificationError(
                "GitHub comparison base is not reachable from head"
            )
        commits = _list(payload.get("commits", []), "comparison commits")
        total = payload.get("total_commits")
        if isinstance(total, int) and total > len(commits):
            raise SourceControlVerificationError("GitHub comparison response is incomplete")
        normalized_commits = tuple(
            SourceControlCommit(
                sha=_sha(str(commit.get("sha", "")), "comparison commit SHA"),
                parents=tuple(
                    _sha(str(parent.get("sha", "")), "parent SHA")
                    for parent in _list(commit.get("parents", []), "comparison parents")
                ),
                tree_sha256=_tree_digest(
                    str(
                        _mapping(
                            _mapping(commit.get("commit"), "commit metadata").get("tree"),
                            "commit tree",
                        ).get("sha", "")
                    )
                ),
            )
            for commit in commits
        )
        files = _list(payload.get("files", []), "comparison files")
        changed_paths = tuple(str(file.get("filename", "")) for file in files)
        root = root_path.strip("/")
        if root:
            changed_paths = tuple(
                path for path in changed_paths if path == root or path.startswith(f"{root}/")
            )
        changed_paths = tuple(sorted(set(changed_paths)))
        comparison_digest = _digest(
            {
                "base_commit": base,
                "head_commit": head,
                "commits": [commit.sha for commit in normalized_commits],
                "changed_paths": changed_paths,
            }
        )
        head_tree = (
            normalized_commits[-1].tree_sha256
            if normalized_commits
            else self.fetch_commit(repository_id, head).tree_sha256
        )
        return SourceControlComparison(
            base_commit=base,
            head_commit=head,
            commits=normalized_commits,
            changed_paths=changed_paths,
            head_tree_sha256=head_tree,
            coverage_digest=comparison_digest,
        )

    def _request_number(self, change_request_id: str) -> str:
        request_id = _text(change_request_id, "change_request_id")
        if not request_id.isdigit() or int(request_id) < 1:
            raise ValueError("GitHub change_request_id must be a positive number")
        return request_id

    def get_change_request(
        self, repository_id: str, change_request_id: str
    ) -> SourceControlChangeRequest:
        self._require("change_request_lookup")
        request_id = self._request_number(change_request_id)
        payload = _mapping(
            self._request("GET", self._path(repository_id, f"/pulls/{request_id}")).payload,
            "pull request",
        )
        base = _mapping(payload.get("base"), "pull request base")
        head = _mapping(payload.get("head"), "pull request head")
        normalized_repository = self.canonical_repository_id(repository_id)
        base_repository = base.get("repo")
        if base_repository is not None:
            base_repository_data = _mapping(base_repository, "pull request base repository")
            base_full_name = base_repository_data.get("full_name")
            if not isinstance(base_full_name, str):
                raise SourceControlVerificationError(
                    "GitHub pull request base repository identity is invalid"
                )
            try:
                normalized_base_repository = self.canonical_repository_id(base_full_name)
            except ValueError as exc:
                raise SourceControlVerificationError(
                    "GitHub pull request base repository identity is invalid"
                ) from exc
            if normalized_base_repository != normalized_repository:
                raise SourceControlVerificationError(
                    "GitHub pull request base repository does not match the source binding"
                )
        merged_at = payload.get("merged_at")
        merged = bool(payload.get("merged", False)) or merged_at is not None
        merger = payload.get("merged_by")
        merger_data = _mapping(merger, "merged_by") if merger is not None else {}
        return SourceControlChangeRequest(
            request_id=request_id,
            repository_id=normalized_repository,
            base_commit=_sha(str(base.get("sha", "")), "base commit"),
            head_commit=_sha(str(head.get("sha", "")), "head commit"),
            resulting_commit=(
                None
                if payload.get("merge_commit_sha") is None
                else _sha(str(payload["merge_commit_sha"]), "resulting commit")
            ),
            merged=merged,
            merge_actor_id=(None if not merger_data.get("login") else str(merger_data["login"])),
        )

    def _reviews(
        self, repository_id: str, request_id: str, head_commit: str
    ) -> tuple[SourceControlReview, ...]:
        payload = _list(
            self._request("GET", self._path(repository_id, f"/pulls/{request_id}/reviews")).payload,
            "pull request reviews",
        )
        decisions = {
            "APPROVED": "approve",
            "CHANGES_REQUESTED": "request_changes",
            "COMMENTED": "comment",
        }
        reviews: list[SourceControlReview] = []
        for review in payload:
            state = str(review.get("state", "COMMENTED")).upper()
            user = _mapping(review.get("user", {}), "review user")
            review_id = str(review.get("id", ""))
            actor_type = str(user.get("type", "")).lower()
            actor_id = str(user.get("login") or f"unknown:{review_id}")
            reviews.append(
                SourceControlReview(
                    review_id=review_id,
                    actor_id=actor_id,
                    actor_kind=(
                        "bot"
                        if actor_type == "bot"
                        else "human"
                        if actor_type == "user"
                        else "unknown"
                    ),
                    decision=decisions.get(state, "comment"),
                    commit_sha=_sha(str(review.get("commit_id", head_commit)), "review commit"),
                )
            )
        return tuple(reviews)

    def _checks(self, repository_id: str, head_commit: str) -> tuple[SourceControlCheck, ...]:
        payload = _mapping(
            self._request(
                "GET", self._path(repository_id, f"/commits/{quote(head_commit)}/check-runs")
            ).payload,
            "check runs",
        )
        checks: list[SourceControlCheck] = []
        for check in _list(payload.get("check_runs", []), "check runs"):
            app = _mapping(check.get("app", {}), "check app")
            conclusion = str(check.get("conclusion") or "pending").lower()
            checks.append(
                SourceControlCheck(
                    name=str(check.get("name", "")),
                    source_id=str(app.get("slug") or app.get("name") or app.get("id") or "unknown"),
                    commit_sha=_sha(str(check.get("head_sha", head_commit)), "check head_sha"),
                    conclusion=conclusion,
                )
            )
        return tuple(checks)

    def _pull_files(self, repository_id: str, request_id: str) -> tuple[str, ...]:
        payload = _list(
            self._request("GET", self._path(repository_id, f"/pulls/{request_id}/files")).payload,
            "pull request files",
        )
        return tuple(sorted({str(file.get("filename", "")) for file in payload}))

    def _ruleset_digest(self, repository_id: str) -> str | None:
        self._require("ruleset_observation")
        payload = self._request("GET", self._path(repository_id, "/rulesets")).payload
        return _digest(payload)

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
        self._require("ruleset_observation")
        request = self.get_change_request(repository_id, change_request_id)
        comparison = self.compare_commits(
            repository_id, request.base_commit, request.head_commit, root_path=root_path
        )
        covered_paths = self._pull_files(repository_id, request.request_id)
        if root_path:
            root = root_path.strip("/")
            covered_paths = tuple(
                path for path in covered_paths if path == root or path.startswith(f"{root}/")
            )
        return verify_normalized_change_request(
            repository_id=self.canonical_repository_id(repository_id),
            request=request,
            comparison=comparison,
            reviews=self._reviews(repository_id, request.request_id, request.head_commit),
            checks=self._checks(repository_id, request.head_commit),
            policy=policy,
            expected_head_commit=expected_head_commit,
            expected_base_commit=expected_base_commit,
            expected_resulting_commit=expected_resulting_commit,
            covered_commits=tuple(commit.sha for commit in comparison.commits),
            covered_paths=covered_paths,
            ruleset_sha256=self._ruleset_digest(repository_id),
            adapter_version=self.adapter_version,
        )

    def verify_webhook(
        self, repository_id: str, headers: Mapping[str, str], payload: bytes
    ) -> SourceControlWebhook:
        self._require("webhook_verification")
        if not isinstance(payload, bytes):
            raise SourceControlWebhookError("webhook payload must be bytes")
        delivery_id = headers.get("X-GitHub-Delivery") or headers.get("x-github-delivery")
        event_type = headers.get("X-GitHub-Event") or headers.get("x-github-event")
        signature = headers.get("X-Hub-Signature-256") or headers.get("x-hub-signature-256")
        if not delivery_id or not event_type or not signature:
            raise SourceControlWebhookError("GitHub webhook headers are incomplete")
        secret = self._webhook_secret_provider()
        if not isinstance(secret, bytes) or not secret:
            raise SourceControlWebhookError("GitHub webhook secret is unavailable")
        expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SourceControlWebhookError("GitHub webhook signature verification failed")
        try:
            body = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceControlWebhookError("GitHub webhook payload is not valid JSON") from exc
        try:
            body_data = _mapping(body, "webhook")
            repository_data = _mapping(body_data.get("repository"), "webhook repository")
        except GitHubTransportError as exc:
            raise SourceControlWebhookError(str(exc)) from exc
        full_name = repository_data.get("full_name") or body_data.get("repository_id")
        if not isinstance(full_name, str):
            raise SourceControlWebhookError("GitHub webhook repository binding is required")
        try:
            normalized_repository = self.canonical_repository_id(full_name)
        except ValueError as exc:
            raise SourceControlWebhookError("GitHub webhook repository binding is invalid") from exc
        if normalized_repository != self.canonical_repository_id(repository_id):
            raise SourceControlWebhookError(
                "GitHub webhook repository does not match the source binding"
            )
        pull_request = body_data.get("pull_request")
        try:
            pull_request_data = (
                _mapping(pull_request, "pull request") if pull_request is not None else {}
            )
            head_data = (
                _mapping(pull_request_data.get("head"), "pull request head")
                if pull_request_data
                else {}
            )
        except GitHubTransportError as exc:
            raise SourceControlWebhookError(str(exc)) from exc
        head_value = head_data.get("sha") or body_data.get("after")
        try:
            normalized_head = None if head_value is None else _sha(str(head_value), "head_commit")
        except ValueError as exc:
            raise SourceControlWebhookError("GitHub webhook head_commit is invalid") from exc
        return SourceControlWebhook(
            delivery_id=delivery_id,
            event_type=event_type,
            repository_id=normalized_repository,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            signature_verified=True,
            action=None if body_data.get("action") is None else str(body_data["action"]),
            head_commit=normalized_head,
        )

    def publish_status(
        self, repository_id: str, commit_sha: str, *, state: str, description: str
    ) -> SourceControlStatus:
        self._require("status_publication")
        normalized_commit = _sha(commit_sha, "commit_sha")
        normalized_state = _text(state, "state")
        if normalized_state not in {"pending", "success", "failure", "error"}:
            raise ValueError(f"unsupported status state: {normalized_state}")
        normalized_description = _text(description, "description")
        response = self._request(
            "POST",
            self._path(repository_id, f"/statuses/{quote(normalized_commit)}"),
            json_body={"state": normalized_state, "description": normalized_description},
        )
        payload = _mapping(response.payload, "status")
        status_id = str(
            payload.get("id")
            or _digest(
                {
                    "commit": normalized_commit,
                    "state": normalized_state,
                    "description": normalized_description,
                }
            )
        )
        return SourceControlStatus(
            repository_id=self.canonical_repository_id(repository_id),
            commit_sha=normalized_commit,
            state=normalized_state,
            description=normalized_description,
            status_id=status_id,
        )


__all__ = [
    "GitHubAuthenticationError",
    "GitHubResponse",
    "GitHubSourceControlError",
    "GitHubSourceControlProvider",
    "GitHubTransport",
    "GitHubTransportError",
]
