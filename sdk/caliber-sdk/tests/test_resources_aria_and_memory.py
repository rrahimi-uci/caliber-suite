"""Wave 3c: Aria's conversational session surface (``client.aria.sessions``),
artifact-draft lifecycle (``client.aria.drafts``), deployment-wide assistant
config, and agent long-term memory (``client.memory``).

Every test pins the exact path and method, per the discipline established for
every prior wave.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- sessions: CRUD + messages + queue ----------------------------------------


def test_session_crud_and_messages_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"session_id": "SESS-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.aria.sessions.list()
        caliber.aria.sessions.create(title="refund triage")
        caliber.aria.sessions.get("SESS-1")
        caliber.aria.sessions.update("SESS-1", title="renamed")
        caliber.aria.sessions.messages("SESS-1")
        caliber.aria.sessions.send_message("SESS-1", "hello")

    assert seen == [
        "GET /assistant/sessions",
        "POST /assistant/sessions",
        "GET /assistant/sessions/SESS-1",
        "PATCH /assistant/sessions/SESS-1",
        "GET /assistant/sessions/SESS-1/messages",
        "POST /assistant/sessions/SESS-1/messages",
    ]


def test_send_message_sends_content_and_extra_params() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({}, status=201)

    with client_with(handler) as caliber:
        caliber.aria.sessions.send_message("SESS-1", "hello", steer=True)

    assert bodies[0] == b'{"content":"hello","steer":true}'


# --- queue: list, enqueue, cancel ----------------------------------------------


def test_queue_enqueue_and_cancel_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.method == "DELETE":
            return httpx.Response(204)
        return envelope({"queue_id": "Q-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.aria.sessions.queue("SESS-1")
        caliber.aria.sessions.enqueue_message("SESS-1", "follow-up")
        cancelled = caliber.aria.sessions.cancel_queued("Q-1")

    assert seen == [
        "GET /assistant/sessions/SESS-1/queue",
        "POST /assistant/sessions/SESS-1/queue",
        "DELETE /assistant/queue/Q-1",
    ]
    assert cancelled is True


# --- attachments: reference, upload (multipart), delete -----------------------


def test_attachment_reference_and_delete_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.method == "DELETE":
            return httpx.Response(204)
        return envelope({"attachment_id": "ATT-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.aria.sessions.attachments("SESS-1")
        caliber.aria.sessions.create_attachment("SESS-1", kind="text_snippet", text="notes")
        deleted = caliber.aria.sessions.delete_attachment("ATT-1")

    assert seen == [
        "GET /assistant/sessions/SESS-1/attachments",
        "POST /assistant/sessions/SESS-1/attachments",
        "DELETE /assistant/attachments/ATT-1",
    ]
    assert deleted is True


def test_upload_attachment_is_multipart_not_json() -> None:
    seen_content_type = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_content_type.append(request.headers.get("content-type", ""))
        assert request.url.path.endswith("/attachments/upload")
        return envelope({"attachment_id": "ATT-2"}, status=201)

    with client_with(handler) as caliber:
        result = caliber.aria.sessions.upload_attachment("SESS-1", "notes.txt", b"raw text")

    assert seen_content_type[0].startswith("multipart/form-data")
    assert result == {"attachment_id": "ATT-2"}


# --- intent, plans, operations, drafts listing ---------------------------------


def test_intent_plans_and_operation_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.aria.sessions.resolve_intent("SESS-1", "create a support prompt")
        caliber.aria.sessions.create_plan("SESS-1", content="create a support prompt")
        caliber.aria.sessions.latest_plan("SESS-1")
        caliber.aria.sessions.execute_plan("SESS-1")
        caliber.aria.sessions.operation("SESS-1", "OP-1")
        caliber.aria.sessions.drafts("SESS-1")

    assert seen == [
        "POST /assistant/sessions/SESS-1/intent/resolve",
        "POST /assistant/sessions/SESS-1/plans",
        "GET /assistant/sessions/SESS-1/plans/latest",
        "POST /assistant/sessions/SESS-1/plans/execute",
        "GET /assistant/sessions/SESS-1/operations/OP-1",
        "GET /assistant/sessions/SESS-1/drafts",
    ]


# --- drafts: validate -> test -> approve -> publish ----------------------------


def test_draft_lifecycle_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.aria.drafts.get("DRAFT-1")
        caliber.aria.drafts.update("DRAFT-1", content="revised")
        caliber.aria.drafts.validate("DRAFT-1")
        caliber.aria.drafts.test("DRAFT-1")
        caliber.aria.drafts.approve("DRAFT-1")
        caliber.aria.drafts.publish("DRAFT-1")

    assert seen == [
        "GET /assistant/drafts/DRAFT-1",
        "PATCH /assistant/drafts/DRAFT-1",
        "POST /assistant/drafts/DRAFT-1/validate",
        "POST /assistant/drafts/DRAFT-1/test",
        "POST /assistant/drafts/DRAFT-1/approve",
        "POST /assistant/drafts/DRAFT-1/publish",
    ]


# --- config, prompt-draft, run --------------------------------------------------


def test_config_prompt_draft_and_run_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.aria.config()
        caliber.aria.update_config(model="gpt-5.6-luna")
        caliber.aria.prompt_draft("draft a support triage prompt")
        caliber.aria.run("RUN-1")

    assert seen == [
        "GET /assistant/config",
        "PATCH /assistant/config",
        "POST /assistant/prompt-draft",
        "GET /assistant/runs/RUN-1",
    ]


# --- memory: add, search, list, delete_all -------------------------------------


def test_memory_add_search_list_and_delete_all_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.memory.add("prefers concise answers", agent_id="AGT-1")
        caliber.memory.search("preferences", agent_id="AGT-1")
        caliber.memory.list(agent_id="AGT-1")
        caliber.memory.delete_all(agent_id="AGT-1")

    assert seen == [
        "POST /memory",
        "POST /memory/search",
        "GET /memory",
        "DELETE /memory",
    ]


def test_memory_list_sends_scope_as_query_params_not_a_body() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.memory.list(agent_id="AGT-1", top_k=5)

    assert captured[0] == {"agent_id": "AGT-1", "top_k": "5"}


def test_memory_add_sends_text_and_scope_as_a_json_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({}, status=201)

    with client_with(handler) as caliber:
        caliber.memory.add("likes short summaries", agent_id="AGT-1")

    assert bodies[0] == b'{"text":"likes short summaries","agent_id":"AGT-1"}'
