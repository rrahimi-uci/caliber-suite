"""``/caliber/assistant`` endpoints — Caliber Assistant agentic authoring surface."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.assistant.models import (
    ATTACHMENT_TEXT_MAX_CHARS,
    AttachmentCreateRequest,
    DraftUpdateRequest,
    IntentExecuteRequest,
    IntentPlanRequest,
    IntentResolveRequest,
    MessageSendRequest,
    QueuedMessageCreateRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from caliber.assistant.service import (
    AssistantService,
    ConflictError,
    normalize_disabled_domains,
    normalize_disabled_intents,
)
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_APPROVER,
    SCOPE_OPERATOR,
    current_scopes,
    require_scopes,
    require_user,
    resolve_identity,
    scopes_for_user,
)
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object

logger = logging.getLogger(__name__)
_MISSING = object()
_REAL_ASSISTANT_ENGINES = frozenset({"openai", "anthropic", "ollama"})

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_PFX = "/ajax-api/2.0/mlflow/caliber/assistant"

SESSIONS_LIST_PATH = f"{_PFX}/sessions"
SESSION_DETAIL_PATH = f"{_PFX}/sessions/{{session_id}}"
MESSAGES_PATH = f"{_PFX}/sessions/{{session_id}}/messages"
DRAFTS_LIST_PATH = f"{_PFX}/sessions/{{session_id}}/drafts"
INTENT_RESOLVE_PATH = f"{_PFX}/sessions/{{session_id}}/intent/resolve"
PLAN_CREATE_PATH = f"{_PFX}/sessions/{{session_id}}/plans"
PLAN_LATEST_PATH = f"{_PFX}/sessions/{{session_id}}/plans/latest"
PLAN_EXECUTE_PATH = f"{_PFX}/sessions/{{session_id}}/plans/execute"
OPERATION_DETAIL_PATH = f"{_PFX}/sessions/{{session_id}}/operations/{{operation_id}}"
DRAFT_DETAIL_PATH = f"{_PFX}/drafts/{{draft_id}}"
DRAFT_VALIDATE_PATH = f"{_PFX}/drafts/{{draft_id}}/validate"
DRAFT_TEST_PATH = f"{_PFX}/drafts/{{draft_id}}/test"
DRAFT_APPROVE_PATH = f"{_PFX}/drafts/{{draft_id}}/approve"
DRAFT_PUBLISH_PATH = f"{_PFX}/drafts/{{draft_id}}/publish"
RUN_DETAIL_PATH = f"{_PFX}/runs/{{run_id}}"
CONFIG_PATH = f"{_PFX}/config"
PROMPT_DRAFT_PATH = f"{_PFX}/prompt-draft"
ATTACHMENTS_PATH = f"{_PFX}/sessions/{{session_id}}/attachments"
ATTACHMENT_UPLOAD_PATH = f"{_PFX}/sessions/{{session_id}}/attachments/upload"
ATTACHMENT_DETAIL_PATH = f"{_PFX}/attachments/{{attachment_id}}"
QUEUE_PATH = f"{_PFX}/sessions/{{session_id}}/queue"
QUEUE_DETAIL_PATH = f"{_PFX}/queue/{{queue_id}}"

# Max bytes pulled from the object store (or accepted via upload) per attachment
# before extraction; large files are truncated, not rejected outright.
_ATTACH_READ_MAX_BYTES = 5 * 1024 * 1024


def _get_service(request: Request) -> AssistantService:
    """Retrieve the ``AssistantService`` stashed on ``app.state``."""
    svc: AssistantService | None = getattr(request.app.state, "assistant_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Assistant service not initialised.")
    return svc


def _require_assistant_enabled(request: Request) -> None:
    config = getattr(request.app.state, "config", None)
    enabled = getattr(config, "assistant_enabled", True) if config is not None else True
    if not enabled:
        raise HTTPException(status_code=503, detail="Assistant disabled.")


def _config_disabled_intents(
    config: object | None, overrides: dict[str, object]
) -> tuple[str, ...]:
    return normalize_disabled_intents(
        overrides.get(
            "disabled_intents",
            getattr(config, "assistant_disabled_intents", "") if config is not None else "",
        )
    )


def _config_disabled_domains(
    config: object | None, overrides: dict[str, object]
) -> tuple[str, ...]:
    return normalize_disabled_domains(
        overrides.get(
            "disabled_domains",
            getattr(config, "assistant_disabled_domains", "") if config is not None else "",
        )
    )


def _set_service_rollout_flags(
    app: Starlette,
    *,
    disabled_intents: tuple[str, ...] | None = None,
    disabled_domains: tuple[str, ...] | None = None,
) -> None:
    svc: AssistantService | None = getattr(app.state, "assistant_service", None)
    if svc is None:
        return
    kwargs: dict[str, Any] = {}
    if disabled_intents is not None:
        kwargs["disabled_intents"] = disabled_intents
    if disabled_domains is not None:
        kwargs["disabled_domains"] = disabled_domains
    if kwargs:
        svc._settings = replace(svc._settings, **kwargs)


def _provider_for_model(
    model: object, available_models: list[dict[str, str]] | None = None
) -> str | None:
    if not isinstance(model, str):
        return None
    for option in available_models or _available_models():
        if option["id"] == model:
            return option["provider"]
    return None


def _assistant_reasoning(config: object | None, overrides: dict[str, object]) -> str:
    value = overrides.get(
        "reasoning",
        getattr(config, "assistant_reasoning", "") if config is not None else "",
    )
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


# ---------------------------------------------------------------------------
# Session routes
# ---------------------------------------------------------------------------


async def list_sessions(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    owner = request.query_params.get("owner")
    if owner and owner != user and SCOPE_ADMIN not in current_scopes(request):
        raise HTTPException(status_code=403, detail="admin scope required to list another owner")
    items = svc.list_sessions(
        session_factory=factory,
        user=None if SCOPE_ADMIN in current_scopes(request) and owner is None else user,
        owner=owner,
    )
    return envelope_response(items)


async def create_session(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    data = await parse_json_object(request)
    body = SessionCreateRequest(**data)
    result = svc.create_session(body, session_factory=factory, user=user)
    return envelope_response(result, status_code=201)


async def get_session(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    result = svc.get_session(session_id, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return envelope_response(result)


async def update_session(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = SessionUpdateRequest(**data)
    result = svc.update_session(session_id, body, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return envelope_response(result)


# ---------------------------------------------------------------------------
# Message routes
# ---------------------------------------------------------------------------


async def list_messages(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    if svc.get_session(session_id, session_factory=factory, user=user) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    items = svc.list_messages(session_id, session_factory=factory, user=user)
    return envelope_response(items)


async def send_message(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    identity = resolve_identity(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = MessageSendRequest(**data)
    result = svc.send_message(
        session_id,
        body,
        session_factory=factory,
        user=user,
        project_id=identity.active_project_id,
        scopes=sorted(identity.scopes),
        current_surface="assistant_drawer",
    )
    return envelope_response(result, status_code=201)


async def draft_prompt(request: Request) -> JSONResponse:
    """One-shot prompt draft from a free-text task description.

    Powers the prompt builder's "Describe it" on-ramp: the assistant drafts a
    starting prompt, which the UI then loads into the *manual* builder so the
    user runs the same compose / validate / save / calibrate steps.
    """
    require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    data = await parse_json_object(request)
    description = str(data.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="'description' is required")
    result = svc.draft_prompt_from_description(description)
    return JSONResponse({"data": result})


async def resolve_intent(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = IntentResolveRequest(**data)
    try:
        result = svc.resolve_intent(
            session_id,
            body,
            session_factory=factory,
            user=user,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return envelope_response(result)


async def create_plan(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = IntentPlanRequest(**data)
    try:
        result = svc.create_intent_plan(
            session_id,
            body,
            session_factory=factory,
            user=user,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return envelope_response(result, status_code=201)


async def get_latest_plan(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    result = svc.get_latest_plan(session_id, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    return envelope_response(result)


async def execute_plan(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = IntentExecuteRequest(**data)
    try:
        result = svc.execute_intent_plan(
            session_id,
            body,
            session_factory=factory,
            user=user,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return envelope_response(result, status_code=201)


async def get_operation(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    operation_id = request.path_params["operation_id"]
    result = svc.get_operation_status(
        session_id,
        operation_id,
        session_factory=factory,
        user=user,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Operation not found.")
    return envelope_response(result)


# ---------------------------------------------------------------------------
# Draft routes
# ---------------------------------------------------------------------------


async def list_drafts(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    if svc.get_session(session_id, session_factory=factory, user=user) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    items = svc.list_drafts(session_id, session_factory=factory, user=user)
    return envelope_response(items)


async def get_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    result = svc.get_draft(draft_id, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return envelope_response(result)


async def update_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    data = await parse_json_object(request)
    body = DraftUpdateRequest(**data)
    try:
        result = svc.update_draft(draft_id, body, session_factory=factory, user=user)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return envelope_response(result)


async def validate_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    report = svc.validate_draft(draft_id, session_factory=factory, user=user)
    return envelope_response(report)


async def test_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    report = svc.test_draft(draft_id, session_factory=factory, user=user)
    return envelope_response(report)


async def approve_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    result = svc.approve_draft(draft_id, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return envelope_response(result)


async def publish_draft(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    draft_id = request.path_params["draft_id"]
    report = svc.publish_draft(draft_id, session_factory=factory, user=user)
    status = 200 if report.get("success") else 400
    return JSONResponse({"data": report}, status_code=status)


# ---------------------------------------------------------------------------
# Run routes
# ---------------------------------------------------------------------------


async def get_run(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    run_id = request.path_params["run_id"]
    result = svc.get_run(run_id, session_factory=factory, user=user)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return envelope_response(result)


# ---------------------------------------------------------------------------
# Assistant config routes
# ---------------------------------------------------------------------------

# Available models the UI can choose from.
_BUILTIN_AVAILABLE_MODELS = [
    # GPT-5.x series
    {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "provider": "openai"},
    {"id": "gpt-5.5", "name": "GPT-5.5", "provider": "openai"},
    {"id": "gpt-5.4", "name": "GPT-5.4", "provider": "openai"},
    {"id": "gpt-5.3", "name": "GPT-5.3", "provider": "openai"},
    {"id": "gpt-5.2", "name": "GPT-5.2", "provider": "openai"},
    # GPT-4.x series
    {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai"},
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "provider": "openai"},
    {"id": "gpt-4.1", "name": "GPT-4.1", "provider": "openai"},
    {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "provider": "openai"},
    {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano", "provider": "openai"},
    # Reasoning models
    {"id": "o3", "name": "o3", "provider": "openai"},
    {"id": "o3-pro", "name": "o3 Pro", "provider": "openai"},
    {"id": "o4-mini", "name": "o4 Mini", "provider": "openai"},
    # Anthropic
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "provider": "anthropic"},
    {"id": "claude-opus-4-20250514", "name": "Claude Opus 4", "provider": "anthropic"},
    {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet", "provider": "anthropic"},
    {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", "provider": "anthropic"},
]


# Provider defaults used when the configured model is empty (auto posture).
_DEFAULT_MODEL_BY_PROVIDER = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-4-20250514",
    "ollama": "qwen2.5:7b",
}


def _resolve_auto_engine(engine: str) -> str:
    """Expand ``auto`` to the real provider it would run (OpenAI/Claude by key)."""
    if engine != "auto":
        return engine
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def _list_ollama_models(base_url: str | None = None) -> list[dict[str, str]]:
    resolved_base_url = (
        base_url or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
    ).rstrip("/")
    req = urllib.request.Request(f"{resolved_base_url}/api/tags", method="GET")  # noqa: S310 - fixed Ollama base_url from config/env, not user input
    try:
        with urllib.request.urlopen(req, timeout=1.5) as response:  # noqa: S310 - trusted internal Ollama endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []

    rows = payload.get("models")
    if not isinstance(rows, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("model") or row.get("name")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append({"id": model_id, "name": model_id, "provider": "ollama"})
    return out


def _available_models(
    *, current_model: str | None = None, current_provider: str | None = None
) -> list[dict[str, str]]:
    models = [*_BUILTIN_AVAILABLE_MODELS, *_list_ollama_models()]
    if (
        isinstance(current_model, str)
        and current_model
        and not any(option["id"] == current_model for option in models)
    ):
        provider = (
            current_provider if current_provider in {"openai", "anthropic", "ollama"} else "openai"
        )
        models.append({"id": current_model, "name": current_model, "provider": provider})
    return models


def _autonomy_status(config: Any, engine_name: str) -> dict[str, Any]:
    if config is None:
        return {
            "agent_review_ready": False,
            "full_autonomy_ready": False,
            "reviewer_configured": False,
            "release_configured": False,
        }
    reviewer_user = str(getattr(config, "assistant_reviewer_user", "") or "").strip()
    release_user = str(getattr(config, "assistant_release_user", "") or "").strip()
    reviewer_ready = bool(
        engine_name in _REAL_ASSISTANT_ENGINES
        and reviewer_user
        and SCOPE_APPROVER in scopes_for_user(config, reviewer_user)
    )
    release_ready = bool(
        reviewer_ready
        and release_user
        and release_user != reviewer_user
        and SCOPE_OPERATOR in scopes_for_user(config, release_user)
    )
    return {
        "agent_review_ready": reviewer_ready,
        "full_autonomy_ready": release_ready,
        "reviewer_configured": bool(reviewer_user),
        "release_configured": bool(release_user),
    }


async def get_assistant_config(request: Request) -> JSONResponse:
    require_user(request)

    # Runtime overrides stored on app.state (mutable), falling back to
    # the frozen CaliberConfig for initial values.
    config = getattr(request.app.state, "config", None)
    overrides = getattr(request.app.state, "_assistant_overrides", {})

    engine_name = overrides.get(
        "engine",
        getattr(config, "assistant_engine", "auto") if config else "auto",
    )
    # 'auto' is resolved to the real provider it would actually run.
    engine_name = _resolve_auto_engine(str(engine_name))
    model = overrides.get(
        "model",
        getattr(config, "assistant_model", "") if config else "",
    )
    reasoning = _assistant_reasoning(config, overrides)
    enabled = getattr(config, "assistant_enabled", True) if config else True
    disabled_intents = _config_disabled_intents(config, overrides)
    disabled_domains = _config_disabled_domains(config, overrides)

    # Determine provider from the current model; fall back to the engine.
    available_models = _available_models()
    provider = _provider_for_model(model, available_models) or (
        engine_name if engine_name in {"openai", "anthropic", "ollama"} else "openai"
    )
    # Empty model → report the resolved provider's default so the UI shows it.
    if not model:
        model = _DEFAULT_MODEL_BY_PROVIDER.get(provider, "gpt-5.6-luna")
    available_models = _available_models(current_model=model, current_provider=provider)

    return JSONResponse(
        {
            "data": {
                "engine": engine_name,
                "model": model,
                "provider": provider,
                "reasoning": reasoning,
                "enabled": enabled,
                "disabled_intents": list(disabled_intents),
                "disabled_domains": list(disabled_domains),
                "available_models": available_models,
                "autonomy": _autonomy_status(config, engine_name),
            }
        }
    )


async def update_assistant_config(request: Request) -> JSONResponse:  # noqa: PLR0912, PLR0915
    # Inherently branchy: validates + applies four independent optional fields
    # (model / reasoning / disabled_intents / disabled_domains) whose effects on
    # engine rebuild are interdependent. Splitting it would scatter that coupled
    # logic; the branch/statement-count lints are suppressed deliberately.
    require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    data = await parse_json_object(request)
    available_models = _available_models()

    new_model = data.get("model")
    raw_reasoning = data.get("reasoning", _MISSING)
    if raw_reasoning is _MISSING:
        new_reasoning = _MISSING
    elif raw_reasoning is None:
        new_reasoning = ""
    elif isinstance(raw_reasoning, str):
        new_reasoning = raw_reasoning.strip()
    else:
        raise HTTPException(status_code=400, detail="'reasoning' must be a string.")
    reasoning_present = new_reasoning is not _MISSING
    disabled_intents_present = "disabled_intents" in data
    disabled_domains_present = "disabled_domains" in data
    disabled_intents: tuple[str, ...] | None = None
    disabled_domains: tuple[str, ...] | None = None

    if disabled_intents_present:
        try:
            disabled_intents = normalize_disabled_intents(data.get("disabled_intents"), strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if disabled_domains_present:
        try:
            disabled_domains = normalize_disabled_domains(data.get("disabled_domains"), strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if (
        not new_model
        and not reasoning_present
        and not disabled_intents_present
        and not disabled_domains_present
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "At least one of 'model', 'reasoning', 'disabled_intents', or "
                "'disabled_domains' is required."
            ),
        )

    # Store runtime overrides on app.state (mutable dict, not the frozen config).
    overrides = getattr(request.app.state, "_assistant_overrides", {})
    config = getattr(request.app.state, "config", None)
    engine_name = _resolve_auto_engine(
        str(
            overrides.get(
                "engine",
                getattr(config, "assistant_engine", "fake") if config else "fake",
            )
        )
    )
    current_model = str(
        overrides.get(
            "model",
            getattr(config, "assistant_model", "") if config else "",
        )
    )
    current_reasoning = _assistant_reasoning(config, overrides)
    provider = _provider_for_model(current_model, available_models) or (
        engine_name if engine_name in _REAL_ASSISTANT_ENGINES else "openai"
    )
    if not current_model:
        current_model = _DEFAULT_MODEL_BY_PROVIDER.get(provider, "gpt-5.6-luna")

    response_engine = engine_name
    effective_model = current_model
    effective_reasoning = current_reasoning
    should_rebuild = False

    if new_model:
        resolved_provider = _provider_for_model(new_model, available_models)
        if resolved_provider is None:
            raise HTTPException(status_code=400, detail=f"Unknown model: {new_model}")
        provider = resolved_provider
        effective_model = new_model
        overrides["engine"] = provider
        overrides["model"] = effective_model
        response_engine = provider
        should_rebuild = True

    if reasoning_present:
        effective_reasoning = str(new_reasoning)
        overrides["reasoning"] = effective_reasoning
        if should_rebuild or response_engine in _REAL_ASSISTANT_ENGINES:
            response_engine = provider
            should_rebuild = True

    if should_rebuild:
        _rebuild_engine(request.app, provider, effective_model, effective_reasoning)

    available_models = _available_models(current_model=effective_model, current_provider=provider)

    if disabled_intents is not None:
        overrides["disabled_intents"] = list(disabled_intents)
    if disabled_domains is not None:
        overrides["disabled_domains"] = list(disabled_domains)
    _set_service_rollout_flags(
        request.app,
        disabled_intents=disabled_intents,
        disabled_domains=disabled_domains,
    )
    request.app.state._assistant_overrides = overrides

    return JSONResponse(
        {
            "data": {
                "engine": response_engine,
                "model": effective_model,
                "provider": provider,
                "reasoning": effective_reasoning,
                "enabled": True,
                "disabled_intents": list(_config_disabled_intents(config, overrides)),
                "disabled_domains": list(_config_disabled_domains(config, overrides)),
                "available_models": available_models,
                "autonomy": _autonomy_status(config, response_engine),
            }
        }
    )


def _rebuild_engine(app: Starlette, provider: str, model: str, reasoning: str) -> None:
    """Swap the assistant engine at runtime."""
    from caliber.assistant.engine import AssistantEngine  # noqa: PLC0415
    from caliber.assistant.reviewer import EngineDraftReviewer  # noqa: PLC0415
    from caliber.assistant.service import AssistantService  # noqa: PLC0415

    engine: AssistantEngine

    if provider == "anthropic":
        from caliber.assistant.anthropic_engine import AnthropicAssistantEngine  # noqa: PLC0415

        engine = AnthropicAssistantEngine(model=model)
    elif provider == "ollama":
        from caliber.assistant.ollama_engine import OllamaAssistantEngine  # noqa: PLC0415

        engine = OllamaAssistantEngine(model=model)
    elif provider == "openai":
        from caliber.assistant.openai_engine import OpenAIAssistantEngine  # noqa: PLC0415
        from caliber.assistant.tools import RegistryToolDispatcher  # noqa: PLC0415

        session_factory = getattr(app.state, "session_factory", None)
        dispatcher = (
            RegistryToolDispatcher(session_factory) if session_factory is not None else None
        )
        engine = OpenAIAssistantEngine(model=model, reasoning=reasoning, tool_dispatcher=dispatcher)
    else:
        from caliber.assistant.fake import FakeAssistantEngine  # noqa: PLC0415

        engine = FakeAssistantEngine()

    svc: AssistantService | None = getattr(app.state, "assistant_service", None)
    if svc is not None:
        svc._engine = engine
        svc._reviewer = EngineDraftReviewer(engine)
    else:
        app.state.assistant_service = AssistantService(
            engine=engine,
            reviewer=EngineDraftReviewer(engine),
            runtime_config=getattr(app.state, "config", None),
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Context attachment routes ("+ add files")
# ---------------------------------------------------------------------------


def _extract_text_from_bytes(name: str, content_type: str, data: bytes) -> tuple[str, bool]:
    """Best-effort plain-text extraction for an attachment payload.

    Returns ``(text, truncated)``. Office documents go through the object-store
    extractors; other files are decoded as UTF-8 when they look like text.
    Unsupported binaries yield an empty string (the caller decides what to do).
    """
    from caliber.routes import object_store as _os  # noqa: PLC0415

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if ext in _os._EXTRACT_DOC_EXTS:
            return _os._extract_docx_text(data), False
        if ext in _os._EXTRACT_PPT_EXTS:
            return _os._extract_pptx_text(data), False
        if ext in _os._EXTRACT_SHEET_EXTS:
            sheets, truncated = _os._extract_xlsx_sheets(data)
            return json.dumps(sheets, default=str), truncated
    except Exception:
        # A corrupt/unsupported office doc shouldn't fail the attachment, but a
        # silent empty extraction is a debugging black hole — log it.
        logger.warning("attachment text extraction failed for %r", name, exc_info=True)
        return "", False
    if _os._looks_text(name, content_type, data[:4096]):
        # Decode only enough bytes to satisfy the downstream char cap (UTF-8 is
        # ≤4 bytes/char) instead of materializing the whole multi-MB blob as a
        # string just to slice it to 50K chars later.
        budget = ATTACHMENT_TEXT_MAX_CHARS * 4
        text = data[:budget].decode("utf-8", errors="replace")
        return text, len(data) > budget
    return "", False


def _read_object_text(request: Request, bucket: str, key: str) -> tuple[str, bool, int, str]:
    from caliber.routes import object_store as _os  # noqa: PLC0415

    client = _os._client(request)
    cfg = _os._config(request)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        content_type = str(obj.get("ContentType") or "")
        data = obj["Body"].read(_ATTACH_READ_MAX_BYTES + 1)
    except Exception as exc:
        raise _os._s3_error(exc, cfg.object_store_endpoint_url) from exc
    over = len(data) > _ATTACH_READ_MAX_BYTES
    data = data[:_ATTACH_READ_MAX_BYTES]
    text, truncated = _extract_text_from_bytes(key, content_type, data)
    return text, (truncated or over), len(data), content_type


async def list_attachments(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    if svc.get_session(session_id, session_factory=factory, user=user) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    items = svc.list_attachments(session_id, session_factory=factory, user=user)
    return envelope_response(items)


async def create_attachment(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = AttachmentCreateRequest(**data)
    try:
        if body.kind == "text_snippet":
            if not body.text:
                raise HTTPException(
                    status_code=400, detail="'text' is required for a text snippet."
                )
            result = svc.add_text_attachment(
                session_id, name=body.name, text=body.text, session_factory=factory, user=user
            )
        elif body.kind == "library_resource":
            if not body.resource_type or not body.resource_id:
                raise HTTPException(
                    status_code=400, detail="'resource_type' and 'resource_id' are required."
                )
            result = svc.add_library_attachment(
                session_id,
                resource_type=body.resource_type,
                resource_id=body.resource_id,
                session_factory=factory,
                user=user,
            )
        else:  # object_file
            if not body.bucket or not body.key:
                raise HTTPException(
                    status_code=400, detail="'bucket' and 'key' are required for an object file."
                )
            text, truncated, size, content_type = _read_object_text(request, body.bucket, body.key)
            result = svc.create_attachment_record(
                session_id,
                kind="object_file",
                session_factory=factory,
                user=user,
                ref_type=body.bucket,
                ref_id=body.key,
                name=body.key.rsplit("/", 1)[-1],
                content_text=text,
                bytes_size=size,
                truncated=truncated,
                metadata={"bucket": body.bucket, "key": body.key, "content_type": content_type},
            )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if "not found" in detail.lower() else 400, detail=detail
        ) from exc
    return envelope_response(result, status_code=201)


async def upload_attachment(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    # The form context manager closes the underlying SpooledTemporaryFile(s) on
    # exit; read everything we need inside it, then work with plain values after.
    async with request.form() as form:
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart 'file' field is required.")
        raw = await upload.read()
        if len(raw) > _ATTACH_READ_MAX_BYTES:
            raise HTTPException(
                status_code=413, detail=f"file exceeds {_ATTACH_READ_MAX_BYTES} bytes"
            )
        name = getattr(upload, "filename", None) or "upload"
        content_type = getattr(upload, "content_type", "") or ""
        bucket_field = form.get("bucket")
        bucket = bucket_field.strip() if isinstance(bucket_field, str) else ""
    text, truncated = _extract_text_from_bytes(name, content_type, raw)
    metadata: dict[str, Any] = {"filename": name, "content_type": content_type}
    ref_id = name
    ref_type = ""
    # Optionally persist the raw file to the object store when a target bucket is given.
    if bucket:
        key = f"aria-uploads/{session_id}/{name}"
        try:
            from caliber.routes import object_store as _os  # noqa: PLC0415

            _os._client(request).put_object(
                Bucket=bucket,
                Key=key,
                Body=raw,
                ContentType=content_type or "application/octet-stream",
            )
            metadata.update({"bucket": bucket, "key": key})
            ref_id = key
            ref_type = bucket
        except Exception as exc:
            logger.warning("aria upload object-store persist failed: %s", exc)
    if not text:
        raise HTTPException(
            status_code=400, detail="Could not extract readable text from the uploaded file."
        )
    try:
        result = svc.create_attachment_record(
            session_id,
            kind="upload",
            session_factory=factory,
            user=user,
            ref_type=ref_type,
            ref_id=ref_id,
            name=name,
            content_text=text,
            bytes_size=len(raw),
            truncated=truncated,
            metadata=metadata,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if "not found" in detail.lower() else 400, detail=detail
        ) from exc
    return envelope_response(result, status_code=201)


async def delete_attachment(request: Request) -> Response:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    attachment_id = request.path_params["attachment_id"]
    ok = svc.delete_attachment(attachment_id, session_factory=factory, user=user)
    if not ok:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Message queue routes ("add to queue" + "steer")
# ---------------------------------------------------------------------------


async def list_queue(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    if svc.get_session(session_id, session_factory=factory, user=user) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    items = svc.list_queue(session_id, session_factory=factory, user=user)
    return envelope_response(items)


async def enqueue_message(request: Request) -> JSONResponse:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    session_id = request.path_params["session_id"]
    data = await parse_json_object(request)
    body = QueuedMessageCreateRequest(**data)
    try:
        result = svc.enqueue_message(
            session_id,
            content=body.content,
            mode=body.mode,
            kind=body.kind,
            session_factory=factory,
            user=user,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if "not found" in detail.lower() else 400, detail=detail
        ) from exc
    return envelope_response(result, status_code=201)


async def cancel_queued(request: Request) -> Response:
    user = require_user(request)
    _require_assistant_enabled(request)
    require_scopes(request, [SCOPE_OPERATOR])
    svc = _get_service(request)
    factory = get_session_factory(request)
    queue_id = request.path_params["queue_id"]
    ok = svc.cancel_queued(queue_id, session_factory=factory, user=user)
    if not ok:
        raise HTTPException(status_code=404, detail="Queued message not found.")
    return Response(status_code=204)


def register(app: Starlette) -> None:
    """Mount assistant routes on the application."""
    app.routes.extend(
        [
            Route(SESSIONS_LIST_PATH, list_sessions, methods=["GET"]),
            Route(SESSIONS_LIST_PATH, create_session, methods=["POST"]),
            Route(SESSION_DETAIL_PATH, get_session, methods=["GET"]),
            Route(SESSION_DETAIL_PATH, update_session, methods=["PATCH"]),
            Route(MESSAGES_PATH, list_messages, methods=["GET"]),
            Route(MESSAGES_PATH, send_message, methods=["POST"]),
            Route(PROMPT_DRAFT_PATH, draft_prompt, methods=["POST"]),
            Route(INTENT_RESOLVE_PATH, resolve_intent, methods=["POST"]),
            Route(PLAN_CREATE_PATH, create_plan, methods=["POST"]),
            Route(PLAN_LATEST_PATH, get_latest_plan, methods=["GET"]),
            Route(PLAN_EXECUTE_PATH, execute_plan, methods=["POST"]),
            Route(OPERATION_DETAIL_PATH, get_operation, methods=["GET"]),
            Route(DRAFTS_LIST_PATH, list_drafts, methods=["GET"]),
            Route(DRAFT_DETAIL_PATH, get_draft, methods=["GET"]),
            Route(DRAFT_DETAIL_PATH, update_draft, methods=["PATCH"]),
            Route(DRAFT_VALIDATE_PATH, validate_draft, methods=["POST"]),
            Route(DRAFT_TEST_PATH, test_draft, methods=["POST"]),
            Route(DRAFT_APPROVE_PATH, approve_draft, methods=["POST"]),
            Route(DRAFT_PUBLISH_PATH, publish_draft, methods=["POST"]),
            Route(RUN_DETAIL_PATH, get_run, methods=["GET"]),
            Route(CONFIG_PATH, get_assistant_config, methods=["GET"]),
            Route(CONFIG_PATH, update_assistant_config, methods=["PATCH"]),
            Route(ATTACHMENT_UPLOAD_PATH, upload_attachment, methods=["POST"]),
            Route(ATTACHMENTS_PATH, list_attachments, methods=["GET"]),
            Route(ATTACHMENTS_PATH, create_attachment, methods=["POST"]),
            Route(ATTACHMENT_DETAIL_PATH, delete_attachment, methods=["DELETE"]),
            Route(QUEUE_PATH, list_queue, methods=["GET"]),
            Route(QUEUE_PATH, enqueue_message, methods=["POST"]),
            Route(QUEUE_DETAIL_PATH, cancel_queued, methods=["DELETE"]),
        ],
    )
