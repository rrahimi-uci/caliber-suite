"""Typed resource modules: request shape, decoding, and forward compatibility."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient, CaliberDecodeError
from caliber_sdk.models import Identity, PersonalAccessToken, decode, decode_list

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


# --- tokens ---------------------------------------------------------------


def test_listing_tokens_decodes_and_never_exposes_a_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/tokens")
        return envelope({"tokens": [{"token_id": "PAT-1", "name": "ci", "active": True}]})

    with client_with(handler) as caliber:
        tokens = caliber.auth.tokens.list()

    assert [t.token_id for t in tokens] == ["PAT-1"]
    assert not hasattr(tokens[0], "token")


def test_creating_a_token_sends_the_ceiling_and_returns_the_secret_once() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.update(_json.loads(request.content))
        return httpx.Response(
            201, json={"data": {"token_id": "PAT-1", "name": "ci", "token": "calpat_secret"}}
        )

    with client_with(handler) as caliber:
        issued = caliber.auth.tokens.create("ci", scopes=["caliber.operator"])

    assert sent == {"name": "ci", "scopes": ["caliber.operator"]}
    assert issued.token == "calpat_secret"


def test_omitting_scopes_sends_no_scopes_key() -> None:
    """Empty means "inherit the owner's scopes"; an empty list would not.

    Sending ``scopes: []`` would ask for a token with no authority at all,
    which is the opposite of the documented default.
    """
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.update(_json.loads(request.content))
        return httpx.Response(201, json={"data": {"token_id": "PAT-1", "token": "x"}})

    with client_with(handler) as caliber:
        caliber.auth.tokens.create("ci")

    assert sent == {"name": "ci"}


def test_revoke_and_rotate_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        if request.url.path.endswith("/rotate"):
            return httpx.Response(201, json={"data": {"token_id": "PAT-2", "token": "new"}})
        return envelope({"token_id": "PAT-1", "revoked": True})

    with client_with(handler) as caliber:
        assert caliber.auth.tokens.revoke("PAT-1") is True
        assert caliber.auth.tokens.rotate("PAT-1").token == "new"

    assert seen == ["DELETE /auth/tokens/PAT-1", "POST /auth/tokens/PAT-1/rotate"]


def test_revoking_an_accounts_sessions_hits_the_real_route() -> None:
    """Regression test: this method previously POSTed to
    ``/auth/accounts/{id}/revoke-sessions``, which no server route serves --
    the real route is ``DELETE /auth/accounts/{id}/sessions``. The SDK<->API
    coverage gate (``test_sdk_api_coverage.py``) is what caught the mismatch;
    this pins the fix so it cannot silently regress.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        return envelope({"user_id": "U-1", "revoked": 3})

    with client_with(handler) as caliber:
        assert caliber.auth.accounts.revoke_sessions("U-1") == 3

    assert seen == ["DELETE /auth/accounts/U-1/sessions"]


# --- identity -------------------------------------------------------------


def test_identity_reports_anonymous_rather_than_raising() -> None:
    """The server answers "who am I", so a bad credential is not an error.

    An SDK that raised here would be describing a different endpoint.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"user_id": "anonymous", "scopes": [], "is_admin": False})

    with client_with(handler) as caliber:
        identity = caliber.me.get()

    assert identity.is_anonymous
    assert identity.scopes == []


def test_a_real_identity_is_not_anonymous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"user_id": "@alice", "scopes": ["caliber.admin"], "is_admin": True})

    with client_with(handler) as caliber:
        identity = caliber.me.get()

    assert not identity.is_anonymous
    assert identity.is_admin


# --- capabilities ---------------------------------------------------------


def test_capabilities_decodes_the_nested_workflow_block() -> None:
    """``decode`` is one level deep, so nesting must be handled explicitly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "workflow_runs": {"queue_enabled": True, "event_backend": "database"},
                "sdk_stability": {"ga": ["prompts"], "beta": ["aria"], "internal": []},
            }
        )

    with client_with(handler) as caliber:
        capabilities = caliber.capabilities_info.get()

    assert capabilities.workflow_runs.queue_enabled is True
    assert capabilities.workflow_runs.event_backend == "database"
    assert capabilities.is_ga("prompts")
    assert capabilities.tier_of("aria") == "beta"
    assert capabilities.tier_of("nonexistent") is None


# --- projects -------------------------------------------------------------


def test_projects_list_and_get_decode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects"):
            return envelope([{"project_id": "PRJ-1", "name": "demo", "file_count": 3}])
        return envelope({"project_id": "PRJ-1", "name": "demo"})

    with client_with(handler) as caliber:
        listed = caliber.projects.list()
        detail = caliber.projects.get("PRJ-1")

    assert listed[0].file_count == 3
    # Absent on the detail response, and None means "not reported here" rather
    # than "zero files".
    assert detail.file_count is None


def test_project_access_members_decode_and_mutate() -> None:
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        path = request.url.path.rsplit("/caliber", 1)[-1]
        body = _json.loads(request.content) if request.content else None
        seen.append((request.method, path, body))
        member = {
            "member_id": "PM-1",
            "project_id": "PRJ-1",
            "user_id": "@bob",
            "role": "editor",
            "status": "active",
            "created_by": "@alice",
        }
        if request.method == "GET":
            return envelope({"members": [member]})
        if request.method == "DELETE":
            return envelope({"project_id": "PRJ-1", "user_id": "@bob", "removed": True})
        return httpx.Response(201 if request.method == "POST" else 200, json={"data": member})

    with client_with(handler) as caliber:
        members = caliber.projects.list_members("PRJ-1")
        added = caliber.projects.add_member("PRJ-1", "@bob", role="editor")
        updated = caliber.projects.update_member("PRJ-1", "@bob", status="inactive")
        removed = caliber.projects.remove_member("PRJ-1", "@bob")

    assert members[0].user_id == "@bob"
    assert added.role == "editor"
    assert updated.status == "active"
    assert removed is True
    assert seen == [
        ("GET", "/projects/PRJ-1/members", None),
        ("POST", "/projects/PRJ-1/members", {"user_id": "@bob", "role": "editor"}),
        ("PATCH", "/projects/PRJ-1/members/@bob", {"status": "inactive"}),
        ("DELETE", "/projects/PRJ-1/members/@bob", None),
    ]


def test_project_files_are_returned_separately_from_directories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "items": [{"file_id": "F-1", "name": "a.txt", "project_id": "PRJ-1"}],
                "directories": [{"path": "datasets", "name": "datasets"}],
                "next_cursor": None,
            }
        )

    with client_with(handler) as caliber:
        files, folders = caliber.projects.files.list("PRJ-1")

    assert [f.file_id for f in files] == ["F-1"]
    assert [d.path for d in folders] == ["datasets"]


def test_upload_sends_multipart_not_json() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(201, json={"data": {"file_id": "F-1", "name": "a.txt"}})

    with client_with(handler) as caliber:
        record = caliber.projects.files.upload(
            "PRJ-1", filename="a.txt", content=b"hello", path="a.txt"
        )

    assert seen["content_type"].startswith("multipart/form-data")
    assert record.file_id == "F-1"


def test_download_returns_raw_bytes_without_envelope_handling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00\x01binary")

    with client_with(handler) as caliber:
        assert caliber.projects.files.download("PRJ-1", "F-1") == b"\x00\x01binary"


# --- forward compatibility ------------------------------------------------


def test_unknown_fields_are_kept_rather_than_dropped() -> None:
    """A newer server must not break an older client.

    Keeping the value in ``extra`` means a caller can still reach a field this
    SDK predates, instead of waiting for a release.
    """
    token = decode(
        PersonalAccessToken, {"token_id": "PAT-1", "name": "ci", "a_field_from_the_future": 42}
    )
    assert token.token_id == "PAT-1"
    assert token.extra["a_field_from_the_future"] == 42


def test_missing_fields_fall_back_to_defaults() -> None:
    """An older server omits newer fields; that must not raise either."""
    identity = decode(Identity, {"user_id": "@alice"})
    assert identity.user_id == "@alice"
    assert identity.scopes == []


@pytest.mark.parametrize("payload", [None, [], "text", 42])
def test_decoding_a_non_object_yields_defaults_rather_than_raising(payload: Any) -> None:
    """A proxy returning something unexpected should not crash the caller."""
    assert decode(Identity, payload).user_id == ""


@pytest.mark.parametrize("payload", [None, {"detail": "not found"}, "text", 42])
def test_decode_list_tolerates_a_non_list_payload_by_default(payload: Any) -> None:
    """The default: a wrong-shaped payload degrades to empty, same as a
    genuinely empty list would -- there is no way to tell them apart from
    the return value alone, which is exactly what ``strict=True`` is for."""
    assert decode_list(PersonalAccessToken, payload) == []


def test_decode_list_strict_raises_on_a_non_list_payload() -> None:
    """The gap ``strict=True`` closes: a proxy error page, the wrong
    endpoint's envelope, or a malformed response must be distinguishable
    from "the list is empty" for a contract test (or a caller) that needs
    to know which one actually happened."""
    with pytest.raises(CaliberDecodeError) as excinfo:
        decode_list(PersonalAccessToken, {"detail": "not found"}, strict=True)
    assert excinfo.value.payload == {"detail": "not found"}


def test_decode_list_strict_still_accepts_a_genuinely_empty_list() -> None:
    """Strict mode narrows what counts as an error; it must not turn a real
    empty result into one."""
    assert decode_list(PersonalAccessToken, [], strict=True) == []


# --- extensibility --------------------------------------------------------


def test_capabilities_decodes_the_extensibility_block_two_levels_down() -> None:
    """``decode`` is one level, and this block nests lists of dataclasses."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "extensibility": {
                    "allowlist_env_var": "CALIBER_PLUGIN_ALLOWLIST",
                    "optimizers": [
                        {
                            "name": "MetaPrompt",
                            "artifact_types": ["prompt"],
                            "source": "builtin",
                        },
                        {
                            "name": "AcmeOptimizer",
                            "artifact_types": ["prompt", "skill"],
                            "source": "plugin",
                            "distribution": "acme-caliber-optimizers",
                            "experimental": True,
                        },
                    ],
                    "plugins": [
                        {
                            "name": "acme",
                            "distribution": "acme-caliber-optimizers",
                            "allowlisted": True,
                        }
                    ],
                }
            }
        )

    with client_with(handler) as caliber:
        extensibility = caliber.capabilities_info.get().extensibility

    assert [item.name for item in extensibility.optimizers] == ["MetaPrompt", "AcmeOptimizer"]
    # The distinction an operator needs before pinning an agent to one.
    assert not extensibility.optimizer("MetaPrompt").is_third_party  # type: ignore[union-attr]
    assert extensibility.optimizer("AcmeOptimizer").is_third_party  # type: ignore[union-attr]
    assert extensibility.plugins[0].is_active


def test_filtering_optimizers_by_artifact_kind_avoids_a_server_rejection() -> None:
    """A prompt-only optimizer on a skill job is refused by the server."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "extensibility": {
                    "optimizers": [
                        {"name": "MetaPrompt", "artifact_types": ["prompt"]},
                        {"name": "SkillMetaPrompt", "artifact_types": ["skill"]},
                        {"name": "GEPA", "artifact_types": ["prompt", "skill"]},
                    ]
                }
            }
        )

    with client_with(handler) as caliber:
        extensibility = caliber.capabilities_info.get().extensibility

    assert [item.name for item in extensibility.optimizers_for("skill")] == [
        "SkillMetaPrompt",
        "GEPA",
    ]
    assert extensibility.optimizer("NotReal") is None


def test_an_installed_but_unlisted_plugin_is_not_active() -> None:
    """The normal state for a freshly installed plugin, and not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"extensibility": {"plugins": [{"name": "acme", "allowlisted": False}]}})

    with client_with(handler) as caliber:
        plugin = caliber.capabilities_info.get().extensibility.plugins[0]

    assert not plugin.is_active
    assert plugin.error is None


def test_an_allowlisted_plugin_that_failed_to_load_is_not_active() -> None:
    """Enabled and broken must not read the same as enabled and working."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "extensibility": {
                    "plugins": [
                        {"name": "acme", "allowlisted": True, "error": "ImportError: no dspy"}
                    ]
                }
            }
        )

    with client_with(handler) as caliber:
        plugin = caliber.capabilities_info.get().extensibility.plugins[0]

    assert not plugin.is_active
    assert plugin.error


def test_a_server_without_the_extensibility_block_decodes_to_an_empty_one() -> None:
    """An older server predates the field; that is not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"sync_workflow_version_run": True})

    with client_with(handler) as caliber:
        extensibility = caliber.capabilities_info.get().extensibility

    assert extensibility.optimizers == []
    assert extensibility.allowlist_env_var == "CALIBER_PLUGIN_ALLOWLIST"
