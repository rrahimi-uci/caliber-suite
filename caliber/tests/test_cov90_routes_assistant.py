"""Coverage-focused tests for ``caliber.routes.assistant``.

These target specific error branches, config-resolution fallbacks, and
attachment/queue edge cases that ``tests/test_assistant_routes.py`` (and the
other ``test_assistant_*`` service/engine suites) do not exercise. Where a
branch is only reachable by calling a private module-level helper directly
(confirmed by reading the route bodies), this file imports and calls that
helper directly — still a real assertion about the module's behavior.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.assistant as assistant_routes
from caliber.assistant.fake import FakeAssistantEngine
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAssistantAttachment,
    CaliberAssistantQueuedMessage,
    CaliberSkill,
)
from caliber.ids import new_assistant_attachment_id, new_skill_id

PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"


def _create_session(client: TestClient, *, title: str = "cov-session") -> str:
    resp = client.post(f"{PREFIX}/sessions", json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["session_id"]


# ---------------------------------------------------------------------------
# _get_service — 503 when the service isn't initialised (89)
# ---------------------------------------------------------------------------


def test_get_service_returns_503_when_uninitialised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state, "assistant_service", None)
    resp = client.get(f"{PREFIX}/sessions")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Assistant service not initialised."


# ---------------------------------------------------------------------------
# _set_service_rollout_flags — no-op when the service is absent (130)
# ---------------------------------------------------------------------------


def test_set_service_rollout_flags_noop_without_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state, "assistant_service", None)
    resp = client.patch(
        f"{PREFIX}/config",
        json={"disabled_intents": ["generate_test_cases"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["disabled_intents"] == ["generate_test_cases"]


# ---------------------------------------------------------------------------
# _provider_for_model — non-string model short-circuits to None (144)
# ---------------------------------------------------------------------------


def test_get_config_handles_non_string_model_override(client: TestClient) -> None:
    client.app.state._assistant_overrides = {"model": 12345}
    resp = client.get(f"{PREFIX}/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["model"] == 12345
    assert data["provider"] == "openai"


# ---------------------------------------------------------------------------
# _assistant_reasoning — None and non-string coercion (157, 160)
# ---------------------------------------------------------------------------


def test_get_config_reasoning_none_override_reports_empty_string(client: TestClient) -> None:
    client.app.state._assistant_overrides = {"reasoning": None}
    resp = client.get(f"{PREFIX}/config")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["reasoning"] == ""


def test_get_config_reasoning_non_string_override_is_stringified(client: TestClient) -> None:
    client.app.state._assistant_overrides = {"reasoning": 42}
    resp = client.get(f"{PREFIX}/config")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["reasoning"] == "42"


# ---------------------------------------------------------------------------
# update_session — 404 for an unknown session (219)
# ---------------------------------------------------------------------------


def test_update_session_404(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/sessions/ASST-00000000", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found."


# ---------------------------------------------------------------------------
# resolve_intent / create_plan — ValueError -> 404/400 (297-300, 320-323, 335)
# ---------------------------------------------------------------------------


def test_resolve_intent_unknown_session_404(client: TestClient) -> None:
    resp = client.post(
        f"{PREFIX}/sessions/ASST-00000000/intent/resolve",
        json={"content": "optimize prompt support-agent"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_create_plan_unknown_session_404(client: TestClient) -> None:
    resp = client.post(
        f"{PREFIX}/sessions/ASST-00000000/plans", json={"intent_name": "create_prompt"}
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_create_plan_without_intent_context_400(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(f"{PREFIX}/sessions/{sid}/plans", json={})
    assert resp.status_code == 400
    assert "no intent context" in resp.json()["detail"].lower()


def test_get_latest_plan_404_when_none_created(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"{PREFIX}/sessions/{sid}/plans/latest")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Plan not found."


# ---------------------------------------------------------------------------
# approve_draft — 404 for an unknown draft (458)
# ---------------------------------------------------------------------------


def test_approve_draft_404(client: TestClient) -> None:
    resp = client.post(f"{PREFIX}/drafts/ADRF-00000000/approve")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Draft not found."


# ---------------------------------------------------------------------------
# update_draft — success / 404 / version-conflict (410-424)
# ---------------------------------------------------------------------------


def _create_draft(client: TestClient) -> tuple[str, str]:
    sid = _create_session(client)
    client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
    turn2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
    drafts = turn2.json()["data"].get("draft_updates", [])
    if not drafts:
        listed = client.get(f"{PREFIX}/sessions/{sid}/drafts").json()["data"]
        if not listed:
            pytest.skip("FakeEngine did not produce a draft")
        return sid, listed[0]["draft_id"]
    return sid, drafts[0]["draft_id"]


def test_update_draft_success(client: TestClient) -> None:
    _sid, draft_id = _create_draft(client)
    current = client.get(f"{PREFIX}/drafts/{draft_id}").json()["data"]
    resp = client.patch(
        f"{PREFIX}/drafts/{draft_id}",
        json={"title": "Updated title", "version": current["version"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["title"] == "Updated title"
    assert resp.json()["data"]["version"] == current["version"] + 1


def test_update_draft_404(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/drafts/ADRF-00000000", json={"title": "x", "version": 1})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Draft not found."


def test_update_draft_version_conflict_409(client: TestClient) -> None:
    _sid, draft_id = _create_draft(client)
    resp = client.patch(
        f"{PREFIX}/drafts/{draft_id}",
        json={"title": "Updated title", "version": 999},
    )
    assert resp.status_code == 409
    assert "version mismatch" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# _resolve_auto_engine — key-based provider resolution (534-536)
# ---------------------------------------------------------------------------


def test_resolve_auto_engine_prefers_openai_key() -> None:
    import os

    old_openai = os.environ.pop("OPENAI_API_KEY", None)
    old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        os.environ["OPENAI_API_KEY"] = "sk-test"
        assert assistant_routes._resolve_auto_engine("auto") == "openai"
    finally:
        if old_openai is not None:
            os.environ["OPENAI_API_KEY"] = old_openai
        else:
            os.environ.pop("OPENAI_API_KEY", None)
        if old_anthropic is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_anthropic


def test_resolve_auto_engine_falls_back_to_anthropic_key() -> None:
    import os

    old_openai = os.environ.pop("OPENAI_API_KEY", None)
    old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        assert assistant_routes._resolve_auto_engine("auto") == "anthropic"
    finally:
        if old_openai is not None:
            os.environ["OPENAI_API_KEY"] = old_openai
        if old_anthropic is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_anthropic
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)


def test_resolve_auto_engine_defaults_to_openai_without_keys() -> None:
    import os

    old_openai = os.environ.pop("OPENAI_API_KEY", None)
    old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert assistant_routes._resolve_auto_engine("auto") == "openai"
    finally:
        if old_openai is not None:
            os.environ["OPENAI_API_KEY"] = old_openai
        if old_anthropic is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_anthropic


# ---------------------------------------------------------------------------
# _list_ollama_models — network failure + payload-shape branches (547-548,
# 552, 558, 561, 564)
# ---------------------------------------------------------------------------


def test_list_ollama_models_returns_empty_on_connection_failure() -> None:
    # No Ollama server is listening on this port in the test environment, so
    # the real network call fails and the except branch returns [].
    assert assistant_routes._list_ollama_models(base_url="http://127.0.0.1:1") == []


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_list_ollama_models_returns_empty_when_models_key_not_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assistant_routes.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeHttpResponse(b'{"models": "not-a-list"}'),
    )
    assert assistant_routes._list_ollama_models() == []


def test_list_ollama_models_filters_invalid_rows_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        b'{"models": ['
        b'"not-a-dict", '
        b'{"model": 123}, '
        b'{"model": "  "}, '
        b'{"model": "llama3"}, '
        b'{"model": "llama3"}, '
        b'{"name": "mistral"}'
        b"]}"
    )
    monkeypatch.setattr(
        assistant_routes.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeHttpResponse(payload),
    )
    result = assistant_routes._list_ollama_models()
    ids = [row["id"] for row in result]
    assert ids == ["llama3", "mistral"]
    assert all(row["provider"] == "ollama" for row in result)


# ---------------------------------------------------------------------------
# update_assistant_config — reasoning coercion + validation guards
# (651, 655, 680, 720)
# ---------------------------------------------------------------------------


def test_update_config_reasoning_none_becomes_empty_string(client: TestClient) -> None:
    # An explicit ``None`` is a legal, "present" value for ``reasoning`` (it
    # satisfies the "at least one field supplied" guard) and coerces to "".
    resp = client.patch(f"{PREFIX}/config", json={"reasoning": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["reasoning"] == ""


def test_update_config_reasoning_non_string_rejected(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/config", json={"reasoning": 123})
    assert resp.status_code == 400
    assert "'reasoning' must be a string" in resp.json()["detail"]


def test_update_config_requires_at_least_one_field(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/config", json={})
    assert resp.status_code == 400
    assert "at least one of" in resp.json()["detail"].lower()


def test_update_config_rejects_unknown_model(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/config", json={"model": "totally-unknown-model-xyz"})
    assert resp.status_code == 400
    assert "unknown model" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# _rebuild_engine — anthropic / openai construction + service-absent /
# unknown-provider fallback (775, 777, 782-784, 786-787, 790, 792, 794, 800)
# ---------------------------------------------------------------------------


def test_update_config_switches_to_anthropic_model(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/config", json={"model": "claude-sonnet-4-20250514"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["provider"] == "anthropic"
    assert data["engine"] == "anthropic"


def test_update_config_switches_to_openai_model(client: TestClient) -> None:
    resp = client.patch(f"{PREFIX}/config", json={"model": "gpt-4o"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["provider"] == "openai"
    assert data["engine"] == "openai"


def test_update_config_rebuilds_service_when_absent(client: TestClient) -> None:
    client.app.state.assistant_service = None
    resp = client.patch(f"{PREFIX}/config", json={"model": "gpt-4o"})
    assert resp.status_code == 200, resp.text
    assert client.app.state.assistant_service is not None


def test_rebuild_engine_falls_back_to_fake_for_unknown_provider(client: TestClient) -> None:
    assistant_routes._rebuild_engine(client.app, "not-a-real-provider", "model-x", "")
    assert isinstance(client.app.state.assistant_service._engine, FakeAssistantEngine)
    assert (
        client.app.state.assistant_service._reviewer._engine
        is client.app.state.assistant_service._engine
    )


def test_autonomy_status_requires_real_engine_scoped_service_identities() -> None:
    config = CaliberConfig(
        assistant_reviewer_user="@reviewer",
        assistant_release_user="@release",
        approver_users="@reviewer",
        operator_users="@release",
    )
    assert assistant_routes._autonomy_status(config, "openai") == {
        "agent_review_ready": True,
        "full_autonomy_ready": True,
        "reviewer_configured": True,
        "release_configured": True,
    }
    assert assistant_routes._autonomy_status(config, "fake")["agent_review_ready"] is False


# ---------------------------------------------------------------------------
# _extract_text_from_bytes — office-doc branches + corrupt-file fallback
# (828, 830, 832-834, 837-838, 846)
# ---------------------------------------------------------------------------


def test_extract_text_from_bytes_docx_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.routes import object_store

    monkeypatch.setattr(object_store, "_extract_docx_text", lambda data: "docx contents")
    text, truncated = assistant_routes._extract_text_from_bytes(
        "report.docx", "application/msword", b"ignored"
    )
    assert text == "docx contents"
    assert truncated is False


def test_extract_text_from_bytes_pptx_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.routes import object_store

    monkeypatch.setattr(object_store, "_extract_pptx_text", lambda data: "slide contents")
    text, truncated = assistant_routes._extract_text_from_bytes(
        "deck.pptx", "application/vnd.ms-powerpoint", b"ignored"
    )
    assert text == "slide contents"
    assert truncated is False


def test_extract_text_from_bytes_xlsx_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.routes import object_store

    monkeypatch.setattr(
        object_store, "_extract_xlsx_sheets", lambda data: ([{"sheet": "Sheet1"}], True)
    )
    text, truncated = assistant_routes._extract_text_from_bytes(
        "book.xlsx", "application/vnd.ms-excel", b"ignored"
    )
    assert "Sheet1" in text
    assert truncated is True


def test_extract_text_from_bytes_handles_corrupt_office_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.routes import object_store

    def _boom(data: bytes) -> str:
        raise ValueError("corrupt docx")

    monkeypatch.setattr(object_store, "_extract_docx_text", _boom)
    text, truncated = assistant_routes._extract_text_from_bytes(
        "broken.docx", "application/msword", b"not really a docx"
    )
    assert text == ""
    assert truncated is False


def test_extract_text_from_bytes_unsupported_binary_returns_empty() -> None:
    text, truncated = assistant_routes._extract_text_from_bytes(
        "payload.bin", "application/octet-stream", bytes(range(256))
    )
    assert text == ""
    assert truncated is False


# ---------------------------------------------------------------------------
# _read_object_text — success + s3-error mapping (850, 852-863)
# ---------------------------------------------------------------------------


class _FakeS3Client:
    def __init__(
        self,
        *,
        get_object_result: dict[str, object] | None = None,
        get_object_error: Exception | None = None,
    ) -> None:
        self._get_object_result = get_object_result
        self._get_object_error = get_object_error
        self.put_calls: list[dict[str, object]] = []

    def get_object(self, **_kwargs: object) -> dict[str, object]:
        if self._get_object_error is not None:
            raise self._get_object_error
        assert self._get_object_result is not None
        return self._get_object_result

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)


def test_create_object_file_attachment_success(client: TestClient) -> None:
    sid = _create_session(client)
    client.app.state.object_store_client = _FakeS3Client(
        get_object_result={
            "ContentType": "text/plain",
            "Body": io.BytesIO(b"object store contents"),
        }
    )
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "object_file", "bucket": "my-bucket", "key": "docs/notes.txt"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["kind"] == "object_file"
    assert "object store contents" in data["content_text"]
    assert data["name"] == "notes.txt"


def test_create_object_file_attachment_maps_s3_error(client: TestClient) -> None:
    sid = _create_session(client)
    client.app.state.object_store_client = _FakeS3Client(get_object_error=RuntimeError("boom"))
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "object_file", "bucket": "my-bucket", "key": "docs/notes.txt"},
    )
    assert resp.status_code == 502
    assert "boom" in resp.json()["detail"]


def test_create_object_file_attachment_requires_bucket_and_key(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "object_file"},
    )
    assert resp.status_code == 400
    assert "bucket" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# create_attachment — library_resource branches (896-898, 901, 928, 930-931)
# ---------------------------------------------------------------------------


def test_create_library_attachment_requires_resource_fields(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "library_resource"},
    )
    assert resp.status_code == 400
    assert "resource_type" in resp.json()["detail"]


def test_create_library_attachment_success(client: TestClient, db_session: Session) -> None:
    sid = _create_session(client)
    skill_id = new_skill_id()
    db_session.add(
        CaliberSkill(
            skill_id=skill_id,
            name="triage-skill",
            content="Classify tickets.",
            owner="@test",
        )
    )
    db_session.commit()

    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "library_resource", "resource_type": "skill", "resource_id": skill_id},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["ref_type"] == "skill"
    assert data["ref_id"] == skill_id
    assert "triage-skill" in data["content_text"]


def test_create_library_attachment_unknown_resource_404(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "library_resource", "resource_type": "skill", "resource_id": "SKILL-ghost"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_create_attachment_conflict_when_limit_reached(
    client: TestClient, db_session: Session
) -> None:
    sid = _create_session(client)
    now = datetime.now(timezone.utc)
    for i in range(25):
        db_session.add(
            CaliberAssistantAttachment(
                attachment_id=new_assistant_attachment_id(),
                session_id=sid,
                kind="text_snippet",
                ref_type="",
                ref_id="",
                name=f"note-{i}",
                content_text="x",
                bytes_size=1,
                truncated=False,
                created_by="@test",
                created_at=now,
            )
        )
    db_session.commit()

    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments",
        json={"kind": "text_snippet", "text": "one too many"},
    )
    assert resp.status_code == 409
    assert "limit reached" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# upload_attachment — size limit, bucket persistence (success + failure),
# unreadable-text guard, and ConflictError/ValueError mapping
# (949, 952, 965-967, 969, 975-979, 981, 998-1002)
# ---------------------------------------------------------------------------


def test_upload_attachment_requires_file_field(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        data={"bucket": "uploads-bucket"},
    )
    assert resp.status_code == 400
    assert "multipart 'file' field is required" in resp.json()["detail"]


def test_upload_attachment_rejects_oversized_file(client: TestClient) -> None:
    sid = _create_session(client)
    oversized = b"a" * (5 * 1024 * 1024 + 1)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        files={"file": ("big.txt", oversized, "text/plain")},
    )
    assert resp.status_code == 413


def test_upload_attachment_persists_to_object_store_when_bucket_given(
    client: TestClient,
) -> None:
    sid = _create_session(client)
    fake_client = _FakeS3Client()
    client.app.state.object_store_client = fake_client
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        files={"file": ("notes.txt", b"hello upload", "text/plain")},
        data={"bucket": "uploads-bucket"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["metadata_"]["bucket"] == "uploads-bucket"
    assert len(fake_client.put_calls) == 1
    assert fake_client.put_calls[0]["Bucket"] == "uploads-bucket"


def test_upload_attachment_continues_when_object_store_persist_fails(
    client: TestClient,
) -> None:
    sid = _create_session(client)

    class _BoomClient:
        def put_object(self, **_kwargs: object) -> None:
            raise RuntimeError("store unavailable")

    client.app.state.object_store_client = _BoomClient()
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        files={"file": ("notes.txt", b"hello upload", "text/plain")},
        data={"bucket": "uploads-bucket"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert "bucket" not in data["metadata_"]


def test_upload_attachment_rejects_unreadable_binary(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        files={"file": ("payload.bin", bytes(range(256)), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "could not extract readable text" in resp.json()["detail"].lower()


def test_upload_attachment_conflict_when_limit_reached(
    client: TestClient, db_session: Session
) -> None:
    sid = _create_session(client)
    now = datetime.now(timezone.utc)
    for i in range(25):
        db_session.add(
            CaliberAssistantAttachment(
                attachment_id=new_assistant_attachment_id(),
                session_id=sid,
                kind="text_snippet",
                ref_type="",
                ref_id="",
                name=f"note-{i}",
                content_text="x",
                bytes_size=1,
                truncated=False,
                created_by="@test",
                created_at=now,
            )
        )
    db_session.commit()

    resp = client.post(
        f"{PREFIX}/sessions/{sid}/attachments/upload",
        files={"file": ("notes.txt", b"hello upload", "text/plain")},
    )
    assert resp.status_code == 409
    assert "limit reached" in resp.json()["detail"].lower()


def test_upload_attachment_unknown_session_404(client: TestClient) -> None:
    resp = client.post(
        f"{PREFIX}/sessions/ASST-00000000/attachments/upload",
        files={"file": ("notes.txt", b"hello upload", "text/plain")},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# enqueue_message — ConflictError / ValueError mapping (1056-1060)
# ---------------------------------------------------------------------------


def test_enqueue_message_unknown_session_404(client: TestClient) -> None:
    resp = client.post(f"{PREFIX}/sessions/ASST-00000000/queue", json={"content": "steer this"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_enqueue_message_conflict_when_queue_full(client: TestClient, db_session: Session) -> None:
    sid = _create_session(client)
    for i in range(20):
        db_session.add(
            CaliberAssistantQueuedMessage(
                queue_id=f"AQM-{i:08d}",
                session_id=sid,
                content=f"queued {i}",
                mode="ask",
                kind="queued",
                position=i,
                status="pending",
                created_by="@test",
            )
        )
    db_session.commit()

    resp = client.post(f"{PREFIX}/sessions/{sid}/queue", json={"content": "one more"})
    assert resp.status_code == 409
    assert "limit reached" in resp.json()["detail"].lower()
