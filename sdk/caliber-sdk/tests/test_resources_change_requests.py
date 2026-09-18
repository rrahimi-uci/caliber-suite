"""Change Request review lifecycle: ``ProjectChangeRequestsAPI``/
``ProjectVersionTagsAPI`` (`P6-B`).

Two cursor-paginated wire shapes appear across this one resource: the
top-level Change Request list wraps its rows in ``{"items": [...]}`` inside
``data`` (like imports/revisions), while every sub-resource list (comments,
reviewers, reviews, attestations, checks) and the version-tag list return a
bare array directly under ``data``. These tests pin both.

Lock-based mutations (``update_head``/``rebase``/``close``/
``assign_reviewer``/``remove_reviewer``) send ``expected_lock_version`` in
the JSON body -- the server's optimistic-concurrency field here, distinct
from :class:`~caliber_sdk.resources.projects.ProjectSourceAPI`'s ``If-Match``
etag.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models.workspace import (
    WorkspaceChangeRequest,
    WorkspaceChangeRequestCheck,
    WorkspaceChangeRequestComment,
    WorkspaceChangeRequestReview,
    WorkspaceChangeRequestReviewer,
    WorkspaceExternalReviewAttestation,
    WorkspaceVersionTag,
)

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, *, next_cursor: str | None = "unset") -> httpx.Response:
    if next_cursor == "unset":
        return httpx.Response(200, json={"data": data})
    return httpx.Response(200, json={"data": data, "next_cursor": next_cursor})


_HEAD: dict[str, Any] = {
    "head_id": "WCH-1",
    "change_request_id": "WCR-1",
    "generation": 1,
    "revision_id": "WSR-1",
    "revision_sha256": "a" * 64,
    "review_policy_version": "v1",
    "review_policy_sha256": "b" * 64,
    "changed_by": "user-1",
    "change_summary": "initial",
}

_CHANGE_REQUEST: dict[str, Any] = {
    "change_request_id": "WCR-1",
    "project_id": "PRJ-1",
    "base_revision_id": None,
    "current_head_revision_id": "WSR-1",
    "created_by": "user-1",
    "title": "Add triage workflow",
    "description": "",
    "head_generation": 1,
    "status": "draft",
    "review_backend": "caliber",
    "lock_version": 1,
    "current_head": _HEAD,
    "active_reviewer_count": 0,
}


# --- change requests: list / get / create / submit / update-head / rebase / close --


def test_list_change_requests_sends_all_filters_and_decodes_the_nested_head() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope({"items": [_CHANGE_REQUEST]}, next_cursor="cursor-2")

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list(
            "PRJ-1",
            status="open",
            created_by="user-1",
            reviewer_user_id="user-2",
            semantic_version="1.0.0",
            limit=10,
            cursor="cursor-1",
        )

    assert seen["path"] == "/projects/PRJ-1/change-requests"
    assert seen["params"] == {
        "status": "open",
        "created_by": "user-1",
        "reviewer_user_id": "user-2",
        "semantic_version": "1.0.0",
        "limit": "10",
        "cursor": "cursor-1",
    }
    assert page.next_cursor == "cursor-2"
    request = page.items[0]
    assert isinstance(request, WorkspaceChangeRequest)
    assert request.current_head.head_id == "WCH-1"


def test_get_change_request_decodes_the_nested_head() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/change-requests/WCR-1")
        return envelope(_CHANGE_REQUEST)

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.get("PRJ-1", "WCR-1")

    assert request.change_request_id == "WCR-1"
    assert request.current_head.revision_id == "WSR-1"


def test_create_sends_the_full_body_including_reviewer_ids() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": _CHANGE_REQUEST})

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.create(
            "PRJ-1",
            title="Add triage workflow",
            head_revision_id="WSR-1",
            semantic_version="1.0.0",
            base_revision_id="WSR-0",
            reviewer_user_ids=["user-2", "user-3"],
        )

    assert seen["body"] == {
        "title": "Add triage workflow",
        "head_revision_id": "WSR-1",
        "semantic_version": "1.0.0",
        "description": "",
        "review_backend": "caliber",
        "reviewer_user_ids": ["user-2", "user-3"],
        "base_revision_id": "WSR-0",
    }
    assert request.title == "Add triage workflow"


def test_create_omits_reviewer_ids_default_and_base_revision_when_not_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": _CHANGE_REQUEST})

    with client_with(handler) as caliber:
        caliber.workspaces.change_requests.create(
            "PRJ-1", title="t", head_revision_id="WSR-1", semantic_version="1.0.0"
        )

    assert seen["body"]["reviewer_user_ids"] == []
    assert "base_revision_id" not in seen["body"]


def test_submit_posts_to_the_submit_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/change-requests/WCR-1:submit")
        assert request.method == "POST"
        return envelope({**_CHANGE_REQUEST, "status": "open"})

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.submit("PRJ-1", "WCR-1")

    assert request.status == "open"


def test_update_head_sends_the_lock_version_and_revision() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["body"] = jsonlib.loads(request.content)
        return envelope({**_CHANGE_REQUEST, "head_generation": 2})

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.update_head(
            "PRJ-1", "WCR-1", revision_id="WSR-2", expected_lock_version=1, change_summary="fix"
        )

    assert seen["path"] == "/projects/PRJ-1/change-requests/WCR-1:update-head"
    assert seen["body"] == {
        "revision_id": "WSR-2",
        "expected_lock_version": 1,
        "change_summary": "fix",
    }
    assert request.head_generation == 2


def test_rebase_sends_the_optional_semantic_version_only_when_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return envelope(_CHANGE_REQUEST)

    with client_with(handler) as caliber:
        caliber.workspaces.change_requests.rebase(
            "PRJ-1", "WCR-1", revision_id="WSR-2", expected_lock_version=2
        )

    assert "semantic_version" not in seen["body"]


def test_close_sends_reason_and_lock_version() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["body"] = jsonlib.loads(request.content)
        return envelope({**_CHANGE_REQUEST, "status": "closed"})

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.close(
            "PRJ-1", "WCR-1", reason="superseded", expected_lock_version=3
        )

    assert seen["path"] == "/projects/PRJ-1/change-requests/WCR-1:close"
    assert seen["body"] == {"reason": "superseded", "expected_lock_version": 3}
    assert request.status == "closed"


# --- comments -----------------------------------------------------------------


def test_list_comments_returns_a_bare_array_page() -> None:
    comment = {
        "comment_id": "WCC-1",
        "change_request_id": "WCR-1",
        "body": "looks good",
        "author": "user-2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/change-requests/WCR-1/comments")
        return envelope([comment], next_cursor=None)

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list_comments("PRJ-1", "WCR-1")

    assert not page.has_more
    assert isinstance(page.items[0], WorkspaceChangeRequestComment)
    assert page.items[0].body == "looks good"


def test_add_comment_sends_only_the_fields_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(
            201,
            json={
                "data": {
                    "comment_id": "WCC-1",
                    "change_request_id": "WCR-1",
                    "body": "looks good",
                    "author": "user-2",
                }
            },
        )

    with client_with(handler) as caliber:
        comment = caliber.workspaces.change_requests.add_comment(
            "PRJ-1", "WCR-1", body="looks good"
        )

    assert seen["body"] == {"body": "looks good"}
    assert comment.comment_id == "WCC-1"


# --- reviewers ------------------------------------------------------------


def test_assign_reviewer_puts_to_the_reviewer_detail_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["method"] = request.method
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(
            201,
            json={
                "data": {
                    "reviewer_id": "WCRV-1",
                    "change_request_id": "WCR-1",
                    "user_id": "user-2",
                    "assigned_by": "user-1",
                    "active": True,
                }
            },
        )

    with client_with(handler) as caliber:
        reviewer = caliber.workspaces.change_requests.assign_reviewer(
            "PRJ-1", "WCR-1", "user-2", expected_lock_version=1
        )

    assert seen["path"] == "/projects/PRJ-1/change-requests/WCR-1/reviewers/user-2"
    assert seen["method"] == "PUT"
    assert seen["body"] == {"expected_lock_version": 1}
    assert isinstance(reviewer, WorkspaceChangeRequestReviewer)
    assert reviewer.active is True


def test_remove_reviewer_deletes_with_a_body_and_returns_the_change_request() -> None:
    """The server's own contract: DELETE returns the *change request*, not
    the reviewer row -- ``active_reviewer_count`` is what a caller checks."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = jsonlib.loads(request.content)
        return envelope({**_CHANGE_REQUEST, "active_reviewer_count": 0})

    with client_with(handler) as caliber:
        request = caliber.workspaces.change_requests.remove_reviewer(
            "PRJ-1", "WCR-1", "user-2", expected_lock_version=2
        )

    assert seen["method"] == "DELETE"
    assert seen["body"] == {"expected_lock_version": 2}
    assert isinstance(request, WorkspaceChangeRequest)
    assert request.active_reviewer_count == 0


def test_list_reviewers_returns_a_bare_array_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            [
                {
                    "reviewer_id": "WCRV-1",
                    "change_request_id": "WCR-1",
                    "user_id": "user-2",
                    "assigned_by": "user-1",
                    "active": True,
                }
            ],
            next_cursor=None,
        )

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list_reviewers("PRJ-1", "WCR-1")

    assert page.items[0].user_id == "user-2"


# --- reviews ----------------------------------------------------------------


def test_submit_review_sends_head_id_decision_and_rationale() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(
            201,
            json={
                "data": {
                    "review_id": "WCRR-1",
                    "change_request_id": "WCR-1",
                    "head_id": "WCH-1",
                    "reviewer_id": "WCRV-1",
                    "decision": "approve",
                    "rationale": "lgtm",
                    "actor_role": "reviewer",
                }
            },
        )

    with client_with(handler) as caliber:
        review = caliber.workspaces.change_requests.submit_review(
            "PRJ-1", "WCR-1", head_id="WCH-1", decision="approve", rationale="lgtm"
        )

    assert seen["body"] == {"head_id": "WCH-1", "decision": "approve", "rationale": "lgtm"}
    assert isinstance(review, WorkspaceChangeRequestReview)
    assert review.decision == "approve"


def test_list_reviews_returns_a_bare_array_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            [
                {
                    "review_id": "WCRR-1",
                    "change_request_id": "WCR-1",
                    "head_id": "WCH-1",
                    "reviewer_id": "WCRV-1",
                    "decision": "approve",
                    "rationale": "",
                    "actor_role": "reviewer",
                }
            ],
            next_cursor=None,
        )

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list_reviews("PRJ-1", "WCR-1")

    assert page.items[0].decision == "approve"


# --- attestations / external review refresh / checks -----------------------


def test_list_attestations_hits_the_right_path_and_decodes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/external-review-attestations")
        return envelope(
            [
                {
                    "attestation_id": "WEA-1",
                    "change_request_id": "WCR-1",
                    "head_id": "WCH-1",
                    "source_id": "SRC-1",
                    "provider_change_request_id": "42",
                    "provider_head_commit": "a" * 40,
                    "provider_resulting_commit": "b" * 40,
                    "source_tree_sha256": "c" * 64,
                    "workspace_revision_sha256": "d" * 64,
                    "policy_version": "v1",
                    "policy_sha256": "e" * 64,
                    "adapter_version": "1",
                    "status": "verified",
                    "reason": "",
                    "verification_input_digest": "f" * 64,
                }
            ],
            next_cursor=None,
        )

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list_attestations("PRJ-1", "WCR-1")

    assert isinstance(page.items[0], WorkspaceExternalReviewAttestation)
    assert page.items[0].status == "verified"


def test_refresh_external_review_posts_and_returns_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":refresh-external-review")
        assert request.method == "POST"
        return httpx.Response(202, json={"data": {"status": "queued"}})

    with client_with(handler) as caliber:
        caliber.workspaces.change_requests.refresh_external_review("PRJ-1", "WCR-1")


def test_list_checks_hits_the_right_path_and_decodes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/change-requests/WCR-1/checks")
        return envelope(
            [
                {
                    "check_id": "WCK-1",
                    "head_id": "WCH-1",
                    "check_name": "lint",
                    "attempt_number": 1,
                    "implementation_version": "1",
                    "input_digest": "a" * 64,
                    "status": "passed",
                }
            ],
            next_cursor=None,
        )

    with client_with(handler) as caliber:
        page = caliber.workspaces.change_requests.list_checks("PRJ-1", "WCR-1")

    assert isinstance(page.items[0], WorkspaceChangeRequestCheck)
    assert page.items[0].status == "passed"


# --- version tags -----------------------------------------------------------


_TAG: dict[str, Any] = {
    "tag_id": "WVT-1",
    "project_id": "PRJ-1",
    "revision_id": "WSR-1",
    "change_request_id": "WCR-1",
    "tag": "1.0.0",
    "kind": "accepted",
    "created_by": "user-1",
}


def test_list_version_tags_sends_limit_and_cursor() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope([_TAG], next_cursor=None)

    with client_with(handler) as caliber:
        page = caliber.workspaces.version_tags.list("PRJ-1", limit=5, cursor="cursor-1")

    assert seen["path"] == "/projects/PRJ-1/version-tags"
    assert seen["params"] == {"limit": "5", "cursor": "cursor-1"}
    assert isinstance(page.items[0], WorkspaceVersionTag)
    assert page.items[0].tag == "1.0.0"


def test_get_version_tag_hits_the_detail_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/version-tags/1.0.0")
        return envelope(_TAG)

    with client_with(handler) as caliber:
        tag = caliber.workspaces.version_tags.get("PRJ-1", "1.0.0")

    assert tag.kind == "accepted"
