"""Offline contract tests for the provider-neutral P4-E source boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from caliber.workspace_source_control import (
    FakeSourceControlProvider,
    SourceControlCapabilities,
    SourceControlCapabilityError,
    SourceControlChangeRequest,
    SourceControlCheck,
    SourceControlCommit,
    SourceControlComparison,
    SourceControlError,
    SourceControlNotFoundError,
    SourceControlProviderRegistry,
    SourceControlReview,
    SourceControlStatus,
    SourceControlVerificationError,
    SourceControlVerificationPolicy,
    SourceControlWebhook,
    SourceControlWebhookError,
)

REPOSITORY = "owner/workspace"
SECRET = b"webhook-secret"


def _sha(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _provider() -> tuple[FakeSourceControlProvider, dict[str, str]]:
    provider = FakeSourceControlProvider(name="github", webhook_secret=SECRET)
    base = _sha("base")
    first = _sha("first")
    head = _sha("head")
    result = _sha("result")
    provider.add_commit(REPOSITORY, base)
    provider.add_commit(
        REPOSITORY,
        first,
        parents=(base,),
        changed_paths=("src/one.py", "docs/readme.md"),
    )
    provider.add_commit(REPOSITORY, head, parents=(first,), changed_paths=("src/two.py",))
    provider.add_commit(REPOSITORY, result, parents=(head,), changed_paths=("src/two.py",))
    provider.add_change_request(
        REPOSITORY,
        "42",
        base_commit=base,
        head_commit=head,
        resulting_commit=result,
        merged=True,
        merge_method="squash",
        merge_actor_id="release-bot",
        provider_event_ids=("delivery-1",),
    )
    return provider, {"base": base, "first": first, "head": head, "result": result}


def test_capabilities_are_closed_and_registry_is_explicit() -> None:
    provider, _ = _provider()
    registry = SourceControlProviderRegistry({"ignored-key": provider})

    assert registry.names() == ("github",)
    assert registry.get(" GITHUB ") is provider
    assert registry.require("github") is provider
    assert registry.capabilities("github") == provider.capabilities()
    assert registry.capabilities("missing") is None

    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)
    registry.register(provider, replace=True)
    with pytest.raises(SourceControlCapabilityError, match="no adapter"):
        registry.require("gitlab")

    capabilities = SourceControlCapabilities(commit_tree_fetch=True)
    assert capabilities.as_dict()["commit_tree_fetch"] is True
    with pytest.raises(ValueError, match="unknown source-control capability"):
        capabilities.require("not-a-capability")
    with pytest.raises(SourceControlCapabilityError, match="unavailable"):
        capabilities.require("commit_reachability")
    with pytest.raises(ValueError, match="must be a string"):
        capabilities.require(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be empty"):
        capabilities.require("")
    with pytest.raises(ValueError, match="must be a boolean"):
        SourceControlCapabilities(commit_tree_fetch="yes")  # type: ignore[arg-type]

    class BrokenProvider:
        name = "broken"
        adapter_version = "test"

        def capabilities(self) -> SourceControlCapabilities:
            raise SourceControlError("provider unavailable")

    broken = SourceControlProviderRegistry({"broken": BrokenProvider()})  # type: ignore[arg-type]
    assert broken.capabilities("broken") is None


def test_fake_commit_range_is_reachable_deterministic_and_root_scoped() -> None:
    provider, ids = _provider()

    assert provider.canonical_repository_id("GITHUB:Owner/Workspace") == "github:owner/workspace"
    assert provider.fetch_commit("github:owner/workspace", ids["head"]).sha == ids["head"]
    comparison = provider.compare_commits(
        "github:owner/workspace", ids["base"], ids["head"], root_path="src"
    )

    assert isinstance(comparison, SourceControlComparison)
    assert [commit.sha for commit in comparison.commits] == sorted([ids["first"], ids["head"]])
    assert comparison.changed_paths == ("src/one.py", "src/two.py")
    assert len(comparison.coverage_digest) == 64

    with pytest.raises(SourceControlNotFoundError, match="unknown commit"):
        provider.fetch_commit(REPOSITORY, _sha("missing"))
    with pytest.raises(SourceControlVerificationError, match="not reachable"):
        provider.compare_commits(REPOSITORY, ids["result"], ids["head"])
    with pytest.raises(SourceControlNotFoundError, match="unknown repository"):
        provider.compare_commits("other/repository", ids["base"], ids["head"])

    duplicate_parent = _sha("duplicate-parent")
    provider.add_commit(
        REPOSITORY, duplicate_parent, parents=(ids["base"],), changed_paths=("src/dup.py",)
    )
    duplicate_head = _sha("duplicate-head")
    provider.add_commit(
        REPOSITORY,
        duplicate_head,
        parents=(duplicate_parent, duplicate_parent),
        changed_paths=("src/dup-head.py",),
    )
    duplicate_comparison = provider.compare_commits(REPOSITORY, ids["base"], duplicate_head)
    assert duplicate_comparison.head_commit == duplicate_head

    missing_parent = _sha("missing-parent")
    missing_head = _sha("missing-head")
    provider.add_commit(REPOSITORY, missing_head, parents=(missing_parent,))
    with pytest.raises(SourceControlNotFoundError, match="unknown commit"):
        provider.compare_commits(REPOSITORY, ids["base"], missing_head)


def test_verify_change_request_accepts_complete_exact_head_evidence() -> None:
    provider, ids = _provider()
    provider.add_review(
        REPOSITORY,
        "42",
        SourceControlReview("review-1", "alice", "human", "approve", ids["head"]),
    )
    provider.add_review(
        REPOSITORY,
        "42",
        SourceControlReview("review-bot", "ci", "bot", "approve", ids["head"]),
    )
    provider.add_check(
        REPOSITORY,
        "42",
        SourceControlCheck("workspace / test", "github-actions", ids["head"], "success"),
    )
    ruleset = _digest("ruleset")
    provider.set_ruleset(REPOSITORY, ruleset)
    policy = SourceControlVerificationPolicy(
        required_checks=("workspace / test",),
        trusted_check_sources=("github-actions",),
        required_approvals=1,
        eligible_reviewer_ids=("alice",),
        require_merged=True,
        expected_ruleset_sha256=ruleset,
    )

    verification = provider.verify_change_request(
        REPOSITORY,
        "42",
        policy=policy,
        expected_head_commit=ids["head"],
        expected_base_commit=ids["base"],
        expected_resulting_commit=ids["result"],
        root_path="src",
    )

    assert verification.status == "verified"
    assert verification.reason == ""
    assert verification.uncovered_commits == ()
    assert verification.uncovered_paths == ()
    assert verification.check_conclusions == {"workspace / test": "success"}
    assert verification.provider_event_ids == ("delivery-1",)
    assert len(verification.verification_input_digest) == 64
    attestation = verification.as_external_attestation(
        source_id="WSS-source",
        head_id="WSCRH-head",
        workspace_revision_sha256=_digest("revision"),
        policy_version="v1",
        policy_sha256=_digest("policy"),
        provider_url="https://github.com/owner/workspace/pull/42",
    )
    assert attestation["status"] == "verified"
    assert attestation["review_actors"][0]["actor_id"] == "alice"  # type: ignore[index]
    assert "secret" not in json.dumps(attestation).lower()


def test_verification_fails_closed_for_incomplete_or_stale_evidence() -> None:
    provider, ids = _provider()
    provider.add_change_request(
        REPOSITORY,
        "43",
        base_commit=ids["base"],
        head_commit=ids["head"],
        covered_commit_shas=(ids["head"],),
        covered_paths=("src/two.py",),
    )
    provider.add_review(
        REPOSITORY,
        "43",
        SourceControlReview("review-unknown", "unknown-user", "unknown", "approve", ids["head"]),
    )
    provider.add_check(
        REPOSITORY,
        "43",
        SourceControlCheck("required", "untrusted", ids["first"], "success"),
    )
    verification = provider.verify_change_request(
        REPOSITORY,
        "43",
        policy=SourceControlVerificationPolicy(
            required_checks=("required",),
            trusted_check_sources=("trusted",),
            eligible_reviewer_ids=("alice",),
            expected_ruleset_sha256=_digest("different-ruleset"),
        ),
        expected_head_commit=ids["head"],
    )
    assert verification.status == "insufficient"
    assert ids["first"] in verification.uncovered_commits
    assert "src/one.py" in verification.uncovered_paths
    assert "required eligible human approvals" in verification.reason
    assert "required check 'required'" in verification.reason
    assert "ruleset" in verification.reason

    stale = provider.verify_change_request(
        REPOSITORY,
        "43",
        policy=SourceControlVerificationPolicy(required_approvals=0, require_complete_range=False),
        expected_head_commit=ids["result"],
    )
    assert stale.status == "stale"


def test_verification_policy_and_normalized_values_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="full hexadecimal"):
        SourceControlCommit("not-a-sha")
    with pytest.raises(ValueError, match="normalized repository-relative"):
        SourceControlCommit(_sha("commit"), changed_paths=("../secret",))
    with pytest.raises(ValueError, match="non-negative"):
        SourceControlVerificationPolicy(required_approvals=-1)
    with pytest.raises(ValueError, match="64-character"):
        SourceControlVerificationPolicy(expected_ruleset_sha256="bad")
    with pytest.raises(ValueError, match="unsupported review decision"):
        SourceControlReview("r", "a", "human", "approve-ish", _sha("commit"))
    with pytest.raises(ValueError, match="actor_kind"):
        SourceControlReview("r", "a", "service", "comment", _sha("commit"))
    with pytest.raises(ValueError, match="unsupported check conclusion"):
        SourceControlCheck("check", "source", _sha("commit"), "green")
    with pytest.raises(ValueError, match="tree_sha256"):
        SourceControlCommit(_sha("commit"), tree_sha256="bad")
    with pytest.raises(ValueError, match="author_kind"):
        SourceControlCommit(_sha("commit"), tree_sha256=_digest("tree"), author_kind="service")
    with pytest.raises(ValueError, match="merged must"):
        SourceControlChangeRequest(  # type: ignore[arg-type]
            "request", "repo", _sha("base"), _sha("head"), merged="yes"
        )
    with pytest.raises(ValueError, match="require_complete_range"):
        SourceControlVerificationPolicy(require_complete_range="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="require_merged"):
        SourceControlVerificationPolicy(require_merged="yes")  # type: ignore[arg-type]

    verification = _provider()[0].verify_change_request(
        REPOSITORY,
        "42",
        policy=SourceControlVerificationPolicy(required_approvals=0),
        expected_head_commit=_provider()[1]["head"],
    )
    with pytest.raises(ValueError, match="head_tree_sha256"):
        SourceControlComparison(
            verification.base_commit,
            verification.head_commit,
            (),
            (),
            "bad",
            verification.coverage_digest,
        )
    with pytest.raises(ValueError, match="coverage_digest"):
        SourceControlComparison(
            verification.base_commit,
            verification.head_commit,
            (),
            (),
            verification.source_tree_sha256,
            "bad",
        )
    with pytest.raises(ValueError, match="verification status"):
        replace(verification, status="unknown")
    with pytest.raises(ValueError, match="source_tree_sha256"):
        replace(verification, source_tree_sha256="bad")
    with pytest.raises(ValueError, match="ruleset_sha256"):
        replace(verification, ruleset_sha256="bad")
    with pytest.raises(ValueError, match="verification_input_digest"):
        replace(verification, verification_input_digest="bad")
    with pytest.raises(ValueError, match="64-character"):
        SourceControlWebhook("delivery", "push", "repo", "bad", True)
    with pytest.raises(ValueError, match="signature_verified"):
        SourceControlWebhook(  # type: ignore[arg-type]
            "delivery", "push", "repo", _digest("payload"), "yes"
        )
    with pytest.raises(ValueError, match="unsupported status state"):
        SourceControlStatus("repo", _sha("commit"), "green", "bad", "status")


def test_missing_fake_capabilities_are_typed_refusals() -> None:
    provider = FakeSourceControlProvider(
        name="github",
        webhook_secret=SECRET,
        capabilities=SourceControlCapabilities(webhook_verification=True),
    )
    with pytest.raises(SourceControlCapabilityError, match="commit_tree_fetch"):
        provider.fetch_commit(REPOSITORY, _sha("missing"))
    payload = b'{"repository_id":"owner/workspace"}'
    signature = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    event = provider.verify_webhook(
        REPOSITORY,
        {
            "X-Source-Control-Delivery": "delivery-1",
            "X-Source-Control-Event": "push",
            "X-Source-Control-Signature-256": f"sha256={signature}",
        },
        payload,
    )
    assert event.signature_verified is True
    with pytest.raises(SourceControlCapabilityError, match="status_publication"):
        provider.publish_status(REPOSITORY, _sha("missing"), state="success", description="done")
    with pytest.raises(ValueError, match="canonical path"):
        provider.canonical_repository_id("github:")
    with pytest.raises(ValueError, match="webhook_secret"):
        FakeSourceControlProvider(name="github", webhook_secret=b"")


def test_webhook_verification_is_signed_repository_bound_and_normalized() -> None:
    provider, ids = _provider()
    payload = json.dumps(
        {"repository_id": "owner/workspace", "action": "synchronize", "head_commit": ids["head"]}
    ).encode()
    signature = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    headers = {
        "x-github-delivery": "delivery-2",
        "x-github-event": "pull_request",
        "x-hub-signature-256": f"sha256={signature}",
    }

    event = provider.verify_webhook(REPOSITORY, headers, payload)
    assert event.delivery_id == "delivery-2"
    assert event.event_type == "pull_request"
    assert event.head_commit == ids["head"]
    assert event.payload_sha256 == hashlib.sha256(payload).hexdigest()

    bad_signature = dict(headers, **{"x-hub-signature-256": "sha256=" + "0" * 64})
    with pytest.raises(SourceControlWebhookError, match="signature"):
        provider.verify_webhook(REPOSITORY, bad_signature, payload)
    with pytest.raises(SourceControlWebhookError, match="required"):
        provider.verify_webhook(REPOSITORY, {}, payload)
    with pytest.raises(SourceControlWebhookError, match="valid JSON"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{
                    "x-hub-signature-256": "sha256="
                    + hmac.new(SECRET, b"no", hashlib.sha256).hexdigest()
                },
            ),
            b"no",
        )

    other_payload = json.dumps({"repository_id": "other/repo"}).encode()
    other_signature = hmac.new(SECRET, other_payload, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="does not match"):
        provider.verify_webhook(
            REPOSITORY,
            dict(headers, **{"x-hub-signature-256": f"sha256={other_signature}"}),
            other_payload,
        )

    with pytest.raises(SourceControlWebhookError, match="payload must be bytes"):
        provider.verify_webhook(REPOSITORY, headers, "not-bytes")  # type: ignore[arg-type]
    list_payload = b"[]"
    list_signature = hmac.new(SECRET, list_payload, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="JSON object"):
        provider.verify_webhook(
            REPOSITORY,
            dict(headers, **{"x-hub-signature-256": f"sha256={list_signature}"}),
            list_payload,
        )
    missing_repository = b"{}"
    missing_repository_signature = hmac.new(SECRET, missing_repository, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="repository binding"):
        provider.verify_webhook(
            REPOSITORY,
            dict(
                headers,
                **{"x-hub-signature-256": f"sha256={missing_repository_signature}"},
            ),
            missing_repository,
        )
    invalid_head = b'{"repository_id":"owner/workspace","head_commit":"not-a-sha"}'
    invalid_head_signature = hmac.new(SECRET, invalid_head, hashlib.sha256).hexdigest()
    with pytest.raises(SourceControlWebhookError, match="head_commit"):
        provider.verify_webhook(
            REPOSITORY,
            dict(headers, **{"x-hub-signature-256": f"sha256={invalid_head_signature}"}),
            invalid_head,
        )


def test_change_request_lookup_and_merged_requirements_fail_closed() -> None:
    provider, ids = _provider()
    with pytest.raises(SourceControlNotFoundError, match="change request"):
        provider.get_change_request(REPOSITORY, "missing")
    with pytest.raises(SourceControlNotFoundError, match="change request"):
        provider.add_review(
            REPOSITORY, "missing", SourceControlReview("r", "a", "human", "comment", ids["head"])
        )
    with pytest.raises(SourceControlNotFoundError, match="change request"):
        provider.add_check(
            REPOSITORY, "missing", SourceControlCheck("c", "s", ids["head"], "success")
        )

    provider.add_change_request(
        REPOSITORY, "44", base_commit=ids["base"], head_commit=ids["head"], merged=False
    )
    not_merged = provider.verify_change_request(
        REPOSITORY,
        "44",
        policy=SourceControlVerificationPolicy(required_approvals=0, require_merged=True),
        expected_head_commit=ids["head"],
    )
    assert not_merged.status == "insufficient"
    assert "not merged" in not_merged.reason

    provider.add_change_request(
        REPOSITORY, "45", base_commit=ids["base"], head_commit=ids["head"], merged=True
    )
    missing_result = provider.verify_change_request(
        REPOSITORY,
        "45",
        policy=SourceControlVerificationPolicy(required_approvals=0, require_merged=True),
        expected_head_commit=ids["head"],
    )
    assert "no resulting commit" in missing_result.reason


def test_status_publication_is_deterministic_and_bound_to_commit() -> None:
    provider, ids = _provider()
    first = provider.publish_status(
        REPOSITORY, ids["head"], state="success", description="Workspace checks passed"
    )
    second = provider.publish_status(
        "github:owner/workspace",
        ids["head"],
        state="success",
        description="Workspace checks passed",
    )
    assert first == second
    assert first.status_id == _digest(
        json.dumps(
            {
                "repository_id": "github:owner/workspace",
                "commit_sha": ids["head"],
                "state": "success",
                "description": "Workspace checks passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    with pytest.raises(ValueError, match="unsupported status state"):
        provider.publish_status(REPOSITORY, ids["head"], state="green", description="bad")
