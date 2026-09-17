"""Offline GitHub adapter tests using an injected REST transport."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

import pytest

from caliber.github_source_control import (
    GitHubAuthenticationError,
    GitHubResponse,
    GitHubSourceControlProvider,
    GitHubTransportError,
)
from caliber.workspace_source_control import (
    SourceControlCapabilities,
    SourceControlCapabilityError,
    SourceControlVerificationError,
    SourceControlVerificationPolicy,
    SourceControlWebhookError,
)

SECRET = b"github-webhook-secret"
REPOSITORY = "owner/workspace"


def _sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class _Transport:
    responses: dict[tuple[str, str], GitHubResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], object | None]] = []

    def request(self, method, path, *, headers, params=None, json_body=None):
        self.calls.append((method, path, dict(headers), json_body))
        try:
            return self.responses[(method, path)]
        except KeyError as exc:
            raise AssertionError(f"unexpected request: {method} {path}") from exc


def _provider(
    responses: dict[tuple[str, str], GitHubResponse],
) -> tuple[GitHubSourceControlProvider, _Transport]:
    transport = _Transport(responses)
    provider = GitHubSourceControlProvider(
        transport,
        token_provider=lambda: "installation-token",
        webhook_secret_provider=lambda: SECRET,
    )
    return provider, transport


def test_github_repository_normalization_and_auth_headers_are_explicit() -> None:
    provider, transport = _provider({})
    assert provider.capabilities().status_publication is True
    assert provider.canonical_repository_id(" GitHub:Owner/Workspace ") == "github:owner/workspace"
    with pytest.raises(ValueError, match="owner/repository"):
        provider.canonical_repository_id("owner/only/three")
    with pytest.raises(ValueError, match="owner/repository"):
        provider.canonical_repository_id("github:")
    with pytest.raises(ValueError, match="host must be a non-empty string"):
        GitHubSourceControlProvider(
            transport,
            token_provider=lambda: "token",
            webhook_secret_provider=lambda: SECRET,
            host=" ",
        )
    with pytest.raises(ValueError, match="full hexadecimal"):
        provider.fetch_commit(REPOSITORY, "not-a-sha")

    unauthenticated = GitHubSourceControlProvider(
        transport,
        token_provider=lambda: " ",
        webhook_secret_provider=lambda: SECRET,
    )
    with pytest.raises(GitHubAuthenticationError, match="token"):
        unauthenticated.fetch_commit("owner/workspace", _sha("head"))


def test_fetch_commit_normalizes_tree_parents_files_and_actor_kind() -> None:
    base = _sha("base")
    head = _sha("head")
    payload = {
        "sha": head,
        "parents": [{"sha": base}],
        "commit": {"tree": {"sha": _sha("tree")}},
        "files": [{"filename": "src/app.py"}, {"filename": "README.md"}],
        "author": {"login": "alice", "type": "User"},
    }
    provider, transport = _provider(
        {("GET", f"/repos/owner/workspace/commits/{head}"): GitHubResponse(200, payload)}
    )

    commit = provider.fetch_commit("github:owner/workspace", head)
    assert commit.sha == head
    assert commit.parents == (base,)
    assert commit.changed_paths == ("README.md", "src/app.py")
    assert commit.author_id == "alice"
    assert commit.author_kind == "human"
    assert transport.calls[0][2]["Authorization"] == "Bearer installation-token"

    bot_payload = dict(payload, author={"login": "github-actions", "type": "Bot"})
    bot_provider, _ = _provider(
        {("GET", f"/repos/owner/workspace/commits/{head}"): GitHubResponse(200, bot_payload)}
    )
    assert bot_provider.fetch_commit(REPOSITORY, head).author_kind == "bot"

    malformed_provider, _ = _provider(
        {
            ("GET", f"/repos/owner/workspace/commits/{head}"): GitHubResponse(
                200, dict(payload, parents={})
            )
        }
    )
    with pytest.raises(GitHubTransportError, match="parents payload is not a list"):
        malformed_provider.fetch_commit(REPOSITORY, head)

    mismatched_provider, _ = _provider(
        {
            ("GET", f"/repos/owner/workspace/commits/{head}"): GitHubResponse(
                200, dict(payload, sha=_sha("different-head"))
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="does not match"):
        mismatched_provider.fetch_commit(REPOSITORY, head)


def test_compare_commits_requires_complete_reachable_response_and_scopes_paths() -> None:
    base = _sha("base")
    head = _sha("head")
    comparison_payload = {
        "merge_base_commit": {"sha": base},
        "total_commits": 1,
        "commits": [
            {
                "sha": head,
                "parents": [{"sha": base}],
                "commit": {"tree": {"sha": _sha("tree")}},
            }
        ],
        "files": [{"filename": "src/app.py"}, {"filename": "docs/readme.md"}],
    }
    provider, _ = _provider(
        {
            ("GET", f"/repos/owner/workspace/compare/{base}...{head}"): GitHubResponse(
                200, comparison_payload
            )
        }
    )
    comparison = provider.compare_commits(REPOSITORY, base, head, root_path="src")
    assert comparison.changed_paths == ("src/app.py",)
    assert comparison.commits[0].sha == head
    assert provider.compare_commits(REPOSITORY, base, head).changed_paths == (
        "docs/readme.md",
        "src/app.py",
    )

    wrong_base = dict(comparison_payload, merge_base_commit={"sha": _sha("other")})
    wrong_provider, _ = _provider(
        {
            ("GET", f"/repos/owner/workspace/compare/{base}...{head}"): GitHubResponse(
                200, wrong_base
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="not reachable"):
        wrong_provider.compare_commits(REPOSITORY, base, head)

    incomplete = dict(comparison_payload, total_commits=2)
    incomplete_provider, _ = _provider(
        {
            ("GET", f"/repos/owner/workspace/compare/{base}...{head}"): GitHubResponse(
                200, incomplete
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="incomplete"):
        incomplete_provider.compare_commits(REPOSITORY, base, head)


def test_github_change_request_verification_uses_exact_head_review_checks_and_ruleset() -> None:
    base = _sha("base")
    head = _sha("head")
    result = _sha("result")
    tree = _sha("tree")
    pull = {
        "base": {"sha": base},
        "head": {"sha": head},
        "merge_commit_sha": result,
        "merged": True,
        "merged_at": "2026-09-17T00:00:00Z",
        "merged_by": {"login": "release-bot"},
    }
    compare = {
        "merge_base_commit": {"sha": base},
        "total_commits": 1,
        "commits": [
            {
                "sha": head,
                "parents": [{"sha": base}],
                "commit": {"tree": {"sha": tree}},
            }
        ],
        "files": [{"filename": "src/app.py"}],
    }
    routes = {
        ("GET", "/repos/owner/workspace/pulls/7"): GitHubResponse(200, pull),
        ("GET", "/repos/owner/workspace/compare/" + base + "..." + head): GitHubResponse(
            200, compare
        ),
        ("GET", "/repos/owner/workspace/pulls/7/files"): GitHubResponse(
            200, [{"filename": "src/app.py"}]
        ),
        ("GET", "/repos/owner/workspace/pulls/7/reviews"): GitHubResponse(
            200,
            [
                {
                    "id": 9,
                    "state": "APPROVED",
                    "commit_id": head,
                    "user": {"login": "alice", "type": "User"},
                },
                {
                    "id": 10,
                    "state": "APPROVED",
                    "commit_id": base,
                    "user": {"login": "bot", "type": "Bot"},
                },
                {
                    "id": 11,
                    "state": "APPROVED",
                    "commit_id": head,
                    "user": {"login": "automation-app", "type": "App"},
                },
            ],
        ),
        ("GET", f"/repos/owner/workspace/commits/{head}/check-runs"): GitHubResponse(
            200,
            {
                "check_runs": [
                    {
                        "name": "workspace / test",
                        "head_sha": head,
                        "conclusion": "success",
                        "app": {"slug": "github-actions"},
                    }
                ]
            },
        ),
        ("GET", "/repos/owner/workspace/rulesets"): GitHubResponse(200, []),
    }
    provider, transport = _provider(routes)
    verification = provider.verify_change_request(
        REPOSITORY,
        "7",
        policy=SourceControlVerificationPolicy(
            required_checks=("workspace / test",),
            trusted_check_sources=("github-actions",),
            eligible_reviewer_ids=("alice",),
            require_merged=True,
            expected_ruleset_sha256=_digest([]),
        ),
        expected_head_commit=head,
        expected_base_commit=base,
        expected_resulting_commit=result,
        root_path="src",
    )
    assert verification.status == "verified"
    assert verification.resulting_commit == result
    assert verification.reviews[0].actor_id == "alice"
    assert verification.reviews[2].actor_kind == "unknown"
    assert len(transport.calls) == 6

    assert (
        provider.verify_change_request(
            REPOSITORY,
            "7",
            policy=SourceControlVerificationPolicy(
                required_checks=("workspace / test",),
                trusted_check_sources=("github-actions",),
                eligible_reviewer_ids=("alice",),
                require_complete_range=False,
                require_merged=True,
                expected_ruleset_sha256=_digest([]),
            ),
            expected_head_commit=head,
            expected_base_commit=base,
            expected_resulting_commit=result,
        ).status
        == "verified"
    )

    with pytest.raises(ValueError, match="positive number"):
        provider.get_change_request(REPOSITORY, "pull-7")
    bad_base_provider, _ = _provider(
        {
            ("GET", "/repos/owner/workspace/pulls/7"): GitHubResponse(
                200,
                dict(
                    pull,
                    base={"sha": base, "repo": {"full_name": "other/repo"}},
                ),
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="base repository"):
        bad_base_provider.get_change_request(REPOSITORY, "7")
    invalid_base_identity_provider, _ = _provider(
        {
            ("GET", "/repos/owner/workspace/pulls/7"): GitHubResponse(
                200,
                dict(pull, base={"sha": base, "repo": {"full_name": 123}}),
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="identity is invalid"):
        invalid_base_identity_provider.get_change_request(REPOSITORY, "7")
    malformed_base_identity_provider, _ = _provider(
        {
            ("GET", "/repos/owner/workspace/pulls/7"): GitHubResponse(
                200,
                dict(pull, base={"sha": base, "repo": {"full_name": "invalid"}}),
            )
        }
    )
    with pytest.raises(SourceControlVerificationError, match="identity is invalid"):
        malformed_base_identity_provider.get_change_request(REPOSITORY, "7")
    matching_base_provider, _ = _provider(
        {
            ("GET", "/repos/owner/workspace/pulls/7"): GitHubResponse(
                200,
                dict(pull, base={"sha": base, "repo": {"full_name": REPOSITORY}}),
            )
        }
    )
    assert matching_base_provider.get_change_request(REPOSITORY, "7").base_commit == base


def test_github_webhook_signature_repository_binding_and_status_publication() -> None:
    head = _sha("head")
    payload = json.dumps(
        {
            "action": "synchronize",
            "repository": {"full_name": "Owner/Workspace"},
            "pull_request": {"head": {"sha": head}},
        }
    ).encode()
    signature = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    status_response = GitHubResponse(201, {"id": 123})
    provider, transport = _provider(
        {("POST", f"/repos/owner/workspace/statuses/{head}"): status_response}
    )
    event = provider.verify_webhook(
        REPOSITORY,
        {
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
        payload,
    )
    assert event.repository_id == "github:owner/workspace"
    assert event.head_commit == head
    status = provider.publish_status(REPOSITORY, head, state="success", description="checks passed")
    assert status.status_id == "123"
    assert transport.calls[-1][3] == {"state": "success", "description": "checks passed"}
    with pytest.raises(ValueError, match="unsupported status state"):
        provider.publish_status(REPOSITORY, head, state="unknown", description="bad")

    bad_signature = {
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": "sha256=" + "0" * 64,
    }
    with pytest.raises(SourceControlWebhookError, match="signature"):
        provider.verify_webhook(REPOSITORY, bad_signature, payload)
    mismatch_payload = json.dumps({"repository": {"full_name": "other/repo"}}).encode()
    mismatch_signature = hmac.new(SECRET, mismatch_payload, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="does not match"):
        provider.verify_webhook(
            REPOSITORY,
            dict(bad_signature, **{"X-Hub-Signature-256": f"sha256={mismatch_signature}"}),
            mismatch_payload,
        )


def test_github_webhook_and_transport_fail_closed() -> None:
    provider, _ = _provider({})
    headers = {
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": "sha256=" + "0" * 64,
    }
    with pytest.raises(SourceControlWebhookError, match="incomplete"):
        provider.verify_webhook(REPOSITORY, {}, b"{}")
    with pytest.raises(SourceControlWebhookError, match="bytes"):
        provider.verify_webhook(REPOSITORY, {}, "{}")  # type: ignore[arg-type]
    unavailable_secret = GitHubSourceControlProvider(
        _Transport({}),
        token_provider=lambda: "token",
        webhook_secret_provider=lambda: b"",
    )
    with pytest.raises(SourceControlWebhookError, match="secret"):
        unavailable_secret.verify_webhook(REPOSITORY, headers, b"{}")
    with pytest.raises(SourceControlWebhookError, match="valid JSON"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{
                    "X-Hub-Signature-256": f"sha256={hmac.new(SECRET, b'bad', hashlib.sha256).hexdigest()}"
                },
            ),
            b"bad",
        )
    scalar = b"[]"
    scalar_signature = hmac.new(SECRET, scalar, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="not an object"):
        provider.verify_webhook(
            REPOSITORY,
            dict(headers, **{"X-Hub-Signature-256": f"sha256={scalar_signature}"}),
            scalar,
        )
    missing_binding = json.dumps({"repository": {}}).encode()
    missing_binding_signature = hmac.new(SECRET, missing_binding, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="binding"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{"X-Hub-Signature-256": f"sha256={missing_binding_signature}"},
            ),
            missing_binding,
        )
    invalid_binding = json.dumps({"repository": {"full_name": "not/a/repository/path"}}).encode()
    invalid_binding_signature = hmac.new(SECRET, invalid_binding, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="binding is invalid"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{"X-Hub-Signature-256": f"sha256={invalid_binding_signature}"},
            ),
            invalid_binding,
        )
    malformed_pull_request = json.dumps(
        {"repository": {"full_name": REPOSITORY}, "pull_request": []}
    ).encode()
    malformed_pull_request_signature = hmac.new(
        SECRET, malformed_pull_request, hashlib.sha256
    ).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="pull request payload is not an object"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{"X-Hub-Signature-256": f"sha256={malformed_pull_request_signature}"},
            ),
            malformed_pull_request,
        )
    invalid_head = json.dumps(
        {"repository": {"full_name": REPOSITORY}, "after": "invalid-head"}
    ).encode()
    invalid_head_signature = hmac.new(SECRET, invalid_head, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="head_commit is invalid"):
        provider.verify_webhook(
            REPOSITORY,
            dict(headers, **{"X-Hub-Signature-256": f"sha256={invalid_head_signature}"}),
            invalid_head,
        )
    push_payload = json.dumps(
        {"repository": {"full_name": REPOSITORY}, "after": _sha("head")}
    ).encode()
    push_signature = hmac.new(SECRET, push_payload, hashlib.sha256).hexdigest()
    event = provider.verify_webhook(
        REPOSITORY,
        dict(headers, **{"X-Hub-Signature-256": f"sha256={push_signature}"}),
        push_payload,
    )
    assert event.head_commit == _sha("head")

    error_provider, _ = _provider(
        {("GET", f"/repos/owner/workspace/commits/{_sha('head')}"): GitHubResponse(403, {})}
    )
    with pytest.raises(GitHubTransportError, match="403"):
        error_provider.fetch_commit(REPOSITORY, _sha("head"))

    no_ruleset = GitHubSourceControlProvider(
        _Transport({}),
        token_provider=lambda: "token",
        webhook_secret_provider=lambda: SECRET,
        capabilities=SourceControlCapabilities(webhook_verification=True),
    )
    with pytest.raises(SourceControlCapabilityError, match="status_publication"):
        no_ruleset.publish_status(REPOSITORY, _sha("head"), state="success", description="x")
