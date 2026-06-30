"""``/caliber/skills`` endpoints — list, read, create, update.

A skill is a reusable prompt fragment (instructions for tool use,
safety guardrails, formatting conventions, reasoning rubrics) that
multiple agents compose into their prompts. Refining a skill once
cascades to every agent that references it.

Skills follow the progressive-disclosure pattern from the Anthropic
skill standard:

* **summary** (level 1) — always loaded in the agent context so it
  knows *when* to activate the skill.
* **content** (level 2) — full instructions loaded when the skill is
  relevant.

Names are kebab-case; names starting with ``claude`` or ``anthropic``
are reserved.  Descriptions should contain both *what* the skill does
and *when* to use it (trigger phrases).

For Phase 4 the surface is intentionally narrow:

* ``GET /caliber/skills`` — list (filterable by ``status`` and tag).
* ``GET /caliber/skills/{skill_id}`` — single skill.
* ``POST /caliber/skills`` — create (operator).
* ``PATCH /caliber/skills/{skill_id}`` — partial update (admin).

There's no hard delete. ``status=archived`` is the operator-facing
remove path; archived skills stay in the DB so audit history and
historical agent configs that reference them remain interpretable.

Every write goes through ``audit_record`` in the same transaction as
the mutation, matching the convention used by the verification-queue
and agents endpoints.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.assistant.skill_runtime import score_skill_for_query
from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.builtin_skills import register_builtin_skills
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberSkillTestRun,
    CaliberSkillVersion,
    CaliberVerificationItem,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.ids import (
    new_item_id,
    new_job_id,
    new_skill_id,
    new_skill_test_run_id,
    new_skill_version_id,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    list_limit,
    parse_json_object,
    visibility_param,
)
from caliber.routes.agents import _extract_skill_refs
from caliber.schemas import (
    _RESERVED_PREFIXES,
    RefinementJobSchema,
    SkillBaselineRequest,
    SkillBindRequest,
    SkillCalibrateRequest,
    SkillCreateRequest,
    SkillPackageImportRequest,
    SkillSchema,
    SkillTestRunCreateRequest,
    SkillTestRunDetail,
    SkillTestRunSummary,
    SkillUpdateRequest,
    SkillWorkspaceLastRun,
    SkillWorkspaceResponse,
    VerificationItemSchema,
)
from caliber.skill_packages import (
    build_skill_package,
    build_skill_package_zip,
    merge_openai_package_metadata,
    parse_skill_package,
)
from caliber.skill_targets import (
    ensure_skill_target,
    skill_target_agent_id,
    skill_target_status,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/skills"
# Durable skill-test-run history. Registered BEFORE ``DETAIL_PATH`` so the
# literal ``/skills/test-runs`` segment isn't captured as a ``{skill_id}``.
TEST_RUNS_PATH = "/ajax-api/2.0/mlflow/caliber/skills/test-runs"
TEST_RUN_DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/skills/test-runs/{test_run_id}"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}"
# OpenAI-compatible package surface (see ``caliber.skill_packages``):
# export a skill as a portable folder/ZIP, and import one back as a row.
IMPORT_PACKAGE_PATH = "/ajax-api/2.0/mlflow/caliber/skills/import-package"
PACKAGE_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/package"
PACKAGE_ZIP_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/package.zip"
# Test surfaces: render the skill content with sample variables ({{var}}
# substitution), and test whether the skill would auto-select for a query
# (the deterministic selection scorer). Both are read-only.
TEST_RENDER_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/test-render"
TEST_SELECTION_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/test-selection"
# Workspace facts + lifecycle, baseline pin, bind target, and the agent-free
# calibrate front door.
WORKSPACE_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/workspace"
BASELINE_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/baseline"
BIND_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/bind"
CALIBRATE_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/calibrate"
ROLLBACK_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/rollback"
VERSIONS_PATH = "/ajax-api/2.0/mlflow/caliber/skills/{skill_id}/versions"

# History listing defaults/cap for ``GET /skills/test-runs``.
_TEST_RUNS_DEFAULT_LIMIT = 20
_TEST_RUNS_MAX_LIMIT = 100

# Optimizer the agent-free calibrate front door queues for a skill job (matches
# :func:`caliber.orchestrator.optimizer_select.select_optimizer` for skills).
_DEFAULT_SKILL_OPTIMIZER = "SkillMetaPrompt"
# Scenario/selection runs are what "Has scenarios" reads from (no dedicated
# scenario store yet — see :func:`caliber.skill_targets.skill_target_status`).
_SCENARIO_RUN_KINDS: frozenset[str] = frozenset({"selection", "scenario"})

# Status filter allowlist for the list endpoint. ``all`` is the
# "don't filter" sentinel; ``active`` / ``archived`` map to the same
# values as the DB column. Anything else is a 400 — operators who
# meant ``active`` but typed something else shouldn't silently see an
# empty list (deep-review consistency note #1).
_LIST_STATUS_VALUES: frozenset[str] = frozenset({"active", "archived", "all"})

# XML angle-bracket pattern — forbidden in skill content per security
# restrictions from the Anthropic skill standard (frontmatter appears
# in the agent's system prompt and angle brackets could inject
# instructions).
_XML_TAG_RE = re.compile(r"<[a-zA-Z/]")

# ``{{variable}}`` placeholder in skill content, for the test-render endpoint.
_SKILL_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


async def list_skills(request: Request) -> JSONResponse:
    """Return every skill, optionally filtered by status and tag.

    Filters mirror the verification-queue convention: query-string
    params named after the field they filter on. ``status=active``
    (the default) hides archived skills; ``status=all`` returns
    everything; any other value filters exactly. ``tag=<value>``
    filters to skills whose JSON ``tags`` array contains the value
    (a Python-side check because SQLite's JSON support is uneven —
    skill counts are small enough that this is fine).
    """
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    requested_status = request.query_params.get("status", "active")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    tag_filter = request.query_params.get("tag")
    limit, offset = list_limit(request)
    with factory() as session:
        config = getattr(request.app.state, "config", None)
        if getattr(config, "builtin_skills_auto_seed", False):
            try:
                created = register_builtin_skills(session)
                if created:
                    session.commit()
            except IntegrityError:
                # Multiple first-load requests can race the same idempotent
                # seed. Roll back and continue with whichever request won.
                session.rollback()
        stmt = select(CaliberSkill).order_by(CaliberSkill.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberSkill.status == requested_status)
        stmt = apply_visibility_filter(
            stmt, CaliberSkill, identity, identity.active_project_id, only=visibility_param(request)
        )
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    items = [SkillSchema.model_validate(row) for row in rows]
    if tag_filter:
        items = [item for item in items if tag_filter in item.tags]
    return envelope_response(items)


async def get_skill(request: Request) -> JSONResponse:
    """Return a single skill by ``skill_id``.

    404s with a structured error payload when the skill does not
    exist. Returning the row regardless of ``status`` is intentional
    — an archived skill referenced from an old agent config still
    needs to be inspectable.
    """
    require_user(request)
    skill_id = request.path_params["skill_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberSkill, skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
    return envelope_response(SkillSchema.model_validate(row))


async def test_render_skill(request: Request) -> JSONResponse:
    """Render a skill's content with sample ``{{variable}}`` values.

    Read-only preview: substitutes supplied ``variables`` into the skill content
    and reports which placeholders were detected / resolved / left unresolved.
    """
    require_user(request)
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request, allow_empty=True)
    variables_raw = body.get("variables") or {}
    if not isinstance(variables_raw, dict):
        raise HTTPException(status_code=400, detail="'variables' must be an object")
    variables = {str(k): "" if v is None else str(v) for k, v in variables_raw.items()}
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberSkill, skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

    content = row.content or ""
    detected: list[str] = []
    for match in _SKILL_VAR_RE.finditer(content):
        if match.group(1) not in detected:
            detected.append(match.group(1))
    applied: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            applied[name] = variables[name]
            return variables[name]
        return match.group(0)

    rendered = _SKILL_VAR_RE.sub(_sub, content)
    unresolved = [name for name in detected if name not in applied]
    return envelope_response_dict(
        {
            "skill_id": row.skill_id,
            "skill_name": row.name,
            "rendered_content": rendered,
            "original_content": content,
            "detected_variables": detected,
            "unresolved_variables": unresolved,
            "variables_applied": applied,
            "summary": row.summary or "",
            "word_count": len(rendered.split()),
            "char_count": len(rendered),
        }
    )


async def test_skill_selection(request: Request) -> JSONResponse:
    """Test whether a skill would be auto-selected for a sample query.

    Read-only: runs the deterministic selection scorer for this one skill and
    reports whether it triggers + the matched-signal reason (Wave 3 trigger test).
    """
    require_user(request)
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request)
    query = body.get("user_message") or body.get("query") or ""
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="'user_message' (or 'query') is required")
    artifact_type = body.get("artifact_type") or ""
    session_goal = body.get("session_goal") or ""
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberSkill, skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

    score, reason = score_skill_for_query(
        row,
        user_message=query,
        artifact_type=str(artifact_type),
        session_goal=str(session_goal),
    )
    return envelope_response_dict(
        {
            "skill_id": row.skill_id,
            "skill_name": row.name,
            "is_selected": score > 0,
            "selection_score": score,
            "selection_reason": reason,
        }
    )


async def create_skill(request: Request) -> JSONResponse:
    """Create a new skill.

    Validates:

    * **Reserved names** — names starting with ``claude`` or
      ``anthropic`` are rejected (reserved per the skill standard).
    * **XML injection** — angle brackets in ``description`` or
      ``summary`` are rejected because those fields can appear in an
      agent's system prompt.
    * **Uniqueness** — 409 if the kebab-case ``name`` is already
      taken; agents reference skills by name so duplicating one
      would be ambiguous.
    """
    body = await parse_json_object(request)
    payload = SkillCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    # --- Security validations from the Anthropic skill standard ---
    for prefix in _RESERVED_PREFIXES:
        if payload.name.startswith(prefix):
            raise HTTPException(
                status_code=400,
                detail=f"skill names starting with {prefix!r} are reserved",
            )

    for field_name, field_value in [
        ("description", payload.description),
        ("summary", payload.summary),
    ]:
        if _XML_TAG_RE.search(field_value):
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} must not contain XML angle brackets (security restriction)",
            )

    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(select(CaliberSkill).where(CaliberSkill.name == payload.name))
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"skill name {payload.name!r} is already in use by {existing.skill_id!r}"),
            )

        skill = CaliberSkill(
            skill_id=new_skill_id(),
            name=payload.name,
            description=payload.description,
            summary=payload.summary,
            content=payload.content,
            # Owner is the authenticated actor, never the request body (a client
            # must not be able to create resources owned by someone else).
            # ``payload.owner`` is accepted for backward-compat but ignored.
            owner=actor,
            project_id=identity.active_project_id,
            # Default to project scope when a project is active, else the user's
            # cross-project library (avoids an invisible orphan — see gap C).
            visibility="project" if identity.active_project_id else "user",
            category=payload.category,
            tags=list(payload.tags),
            skill_metadata=dict(payload.skill_metadata),
            allowed_tools=payload.allowed_tools,
            depends_on=list(payload.depends_on),
            status="active",
            version=1,
        )
        session.add(skill)
        session.flush()
        _record_skill_version(session, skill, created_by=actor)

        audit_record(
            session,
            actor=actor,
            action="create_skill",
            entity_type="skill",
            entity_id=skill.skill_id,
            details={
                "name": skill.name,
                "owner": skill.owner,
                "category": skill.category,
                "tags": list(skill.tags),
            },
        )
        session.commit()
        data = SkillSchema.model_validate(skill)

    return envelope_response(data, status_code=201)


_UPDATABLE_FIELDS = (
    "description",
    "summary",
    "content",
    "owner",
    "category",
    "tags",
    "skill_metadata",
    "allowed_tools",
    "depends_on",
    "status",
)


async def update_skill(request: Request) -> JSONResponse:
    """Partial-update a skill.

    Content changes bump ``version`` so external references can
    detect drift without string-comparing. The audit row records the
    diff (which fields changed and to what) plus the old/new version
    pair — handy when an agent's recent refinement chain has
    silently been "this skill changed."
    """
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request)
    payload = SkillUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    # ``exclude_unset`` distinguishes "field omitted" from "field
    # explicitly None" — only mutate the former.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        # Capture pre-edit state so a content change can backfill the prior
        # version's snapshot before mutating (the diff loop edits in place).
        pre_content = skill.content
        pre_summary = skill.summary
        pre_version = skill.version

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(skill, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(skill, field, new_value)

        if not diff:
            return envelope_response(SkillSchema.model_validate(skill))

        # Bump the version only when the content actually changed —
        # a tag tweak or owner reassignment shouldn't invalidate
        # cached references.
        version_changed = False
        if "content" in diff:
            # Backfill the pre-edit content as the prior version's snapshot
            # (no-op when create_skill already recorded it), so rollback always
            # has an exact target, then record the new content as a new version.
            _ensure_skill_version_snapshot(
                session, skill.skill_id, pre_version, pre_content, pre_summary, created_by=actor
            )
            skill.version = pre_version + 1
            diff["version"] = {"from": pre_version, "to": skill.version}
            version_changed = True
            _record_skill_version(session, skill, created_by=actor)

        audit_record(
            session,
            actor=actor,
            action="update_skill",
            entity_type="skill",
            entity_id=skill.skill_id,
            details={
                "changes": diff,
                "version_bumped": version_changed,
            },
        )
        session.commit()
        data = SkillSchema.model_validate(skill)

    return envelope_response(data)


def _record_skill_version(session: Session, skill: CaliberSkill, *, created_by: str) -> None:
    """Snapshot the skill's current content/summary at its current version number.

    Called whenever ``skill.version`` is (re)assigned — on create, on a
    content-changing edit, and on rollback — so the version table is the
    authoritative history the panel lists, diffs, and rolls back against.
    """
    session.add(
        CaliberSkillVersion(
            skill_version_id=new_skill_version_id(),
            skill_id=skill.skill_id,
            version_number=skill.version,
            content=skill.content,
            summary=skill.summary or "",
            created_by=created_by,
        )
    )


def _ensure_skill_version_snapshot(
    session: Session,
    skill_id: str,
    version_number: int,
    content: str,
    summary: str,
    *,
    created_by: str,
) -> None:
    """Record a snapshot for ``(skill_id, version_number)`` if one doesn't exist.

    Backfills the pre-edit content for skills that predate the version table (or
    were inserted without going through ``create_skill``), so the first edit
    leaves a real rollback target rather than orphaning the prior content.
    """
    exists = (
        session.execute(
            select(CaliberSkillVersion.skill_version_id)
            .where(CaliberSkillVersion.skill_id == skill_id)
            .where(CaliberSkillVersion.version_number == version_number)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if exists is None:
        session.add(
            CaliberSkillVersion(
                skill_version_id=new_skill_version_id(),
                skill_id=skill_id,
                version_number=version_number,
                content=content,
                summary=summary or "",
                created_by=created_by,
            )
        )


def _previous_skill_version(session: Session, skill: CaliberSkill) -> CaliberSkillVersion | None:
    """The snapshot with the largest version_number strictly below the current.

    This is the exact content that was live immediately before the current
    version, so rolling back to it (as a new version) restores prior state
    precisely. ``None`` when the skill has no earlier version.
    """
    return (
        session.execute(
            select(CaliberSkillVersion)
            .where(CaliberSkillVersion.skill_id == skill.skill_id)
            .where(CaliberSkillVersion.version_number < skill.version)
            .order_by(CaliberSkillVersion.version_number.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


async def rollback_skill(request: Request) -> JSONResponse:
    """``POST /caliber/skills/{skill_id}/rollback`` — restore the prior content.

    Skills are a single mutable row, so a content edit overwrites the previous
    text. This restores the exact content of the immediately-prior version
    snapshot (from ``caliber_skill_versions``) as a *new* version — keeping the
    forward-only counter monotonic. Returns 409 when there is no earlier version.
    """
    skill_id = request.path_params["skill_id"]
    actor = require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        prior = _previous_skill_version(session, skill)
        if prior is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"no earlier version for skill {skill_id!r}; "
                    "rollback needs a prior content version to restore"
                ),
            )

        current_content = skill.content
        old_version = skill.version
        skill.content = prior.content
        skill.summary = prior.summary
        skill.version = old_version + 1
        _record_skill_version(session, skill, created_by=actor)

        audit_record(
            session,
            actor=actor,
            action="rollback_skill",
            entity_type="skill",
            entity_id=skill.skill_id,
            details={
                "changes": {
                    "content": {"from": current_content, "to": prior.content},
                    "version": {"from": old_version, "to": skill.version},
                },
                "restored_from_version": prior.version_number,
                "version_bumped": True,
            },
        )
        session.commit()
        data = SkillSchema.model_validate(skill)

    return envelope_response(data)


async def list_skill_versions(request: Request) -> JSONResponse:
    """``GET /caliber/skills/{skill_id}/versions`` — content version history, newest first."""
    require_user(request)
    skill_id = request.path_params["skill_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberSkill, skill_id) is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
        rows = (
            session.execute(
                select(CaliberSkillVersion)
                .where(CaliberSkillVersion.skill_id == skill_id)
                .order_by(CaliberSkillVersion.version_number.desc())
            )
            .scalars()
            .all()
        )
        data = [
            {
                "skill_version_id": row.skill_version_id,
                "skill_id": row.skill_id,
                "version_number": row.version_number,
                "content": row.content,
                "summary": row.summary,
                "created_by": row.created_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    return envelope_response_dict(data)


async def get_skill_package(request: Request) -> JSONResponse:
    """Preview the OpenAI-compatible package generated for a skill.

    Read-only: renders ``SKILL.md`` + ``agents/openai.yaml`` + bundled
    resources from the skill row (no DB mutation). Malformed resource
    metadata is surfaced as ``warnings`` with ``is_valid=False`` rather
    than failing the request — the preview should still show what *can*
    be generated.
    """
    require_user(request)
    skill_id = request.path_params["skill_id"]
    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
        package = build_skill_package(skill)
    return envelope_response(package)


async def get_skill_package_zip(request: Request) -> Response:
    """Download the generated package as a ZIP archive."""
    require_user(request)
    skill_id = request.path_params["skill_id"]
    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
        archive = build_skill_package_zip(skill)
        filename = f"{skill.name}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


async def import_skill_package(request: Request) -> JSONResponse:
    """Create a skill row from an uploaded OpenAI-style package.

    The payload is a list of ``{path, content}`` file objects plus
    registry metadata (``owner``, ``category``, ``tags``,
    ``skill_metadata``). ``parse_skill_package`` validates the folder
    shape (exactly one ``SKILL.md`` with kebab-case ``name`` frontmatter,
    resources only under ``scripts/``/``references/``/``assets/``, no
    path traversal) and raises 400 on any violation. Caller
    ``skill_metadata`` is merged *over* the parsed package metadata so
    bundled resources are never lost.
    """
    body = await parse_json_object(request)
    payload = SkillPackageImportRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    imported = parse_skill_package(payload.files)

    for prefix in _RESERVED_PREFIXES:
        if imported.name.startswith(prefix):
            raise HTTPException(
                status_code=400,
                detail=f"skill names starting with {prefix!r} are reserved",
            )

    merged_metadata = merge_openai_package_metadata(dict(payload.skill_metadata), imported)

    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(select(CaliberSkill).where(CaliberSkill.name == imported.name))
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"skill name {imported.name!r} is already in use by {existing.skill_id!r}"),
            )

        skill = CaliberSkill(
            skill_id=new_skill_id(),
            name=imported.name,
            description=imported.description,
            summary=imported.summary,
            content=imported.content,
            owner=payload.owner,
            category=payload.category,
            tags=list(payload.tags),
            skill_metadata=merged_metadata,
            allowed_tools=payload.allowed_tools,
            depends_on=list(payload.depends_on),
            status="active",
            version=1,
        )
        session.add(skill)
        session.flush()
        _record_skill_version(session, skill, created_by=actor)

        audit_record(
            session,
            actor=actor,
            action="import_skill_package",
            entity_type="skill",
            entity_id=skill.skill_id,
            details={
                "name": skill.name,
                "owner": skill.owner,
                "category": skill.category,
                "file_count": len(payload.files),
            },
        )
        session.commit()
        data = SkillSchema.model_validate(skill)

    return envelope_response(data, status_code=201)


# ---------------------------------------------------------------------------
# Durable skill-test runs — run history for the Skills tab (prompt/tool analog).
# ---------------------------------------------------------------------------


def _aggregate_skill_test_run(
    results: list[Any],
) -> tuple[int, int, int, int, float | None]:
    """Recompute (size, passed, failed, partial, overall_score) from results.

    The client never supplies aggregates — they're derived here so a buggy or
    malicious payload can't desync the durable summary from the per-case data.
    """
    size = len(results)
    passed = sum(1 for r in results if r.verdict == "pass")
    failed = sum(1 for r in results if r.verdict == "fail")
    partial = sum(1 for r in results if r.verdict == "partial")
    overall = (sum(r.score for r in results) / size) if size else None
    return size, passed, failed, partial, overall


async def create_skill_test_run(request: Request) -> JSONResponse:
    """``POST /caliber/skills/test-runs`` — persist a completed skill-test run.

    Body: ``skill_id``, optional ``kind``/``skill_version`` snapshot, optional
    ``host_agent_id``, the per-case ``results`` array, and optional
    ``trace_id``/``mlflow_run_id``. The server recomputes the count/score summary
    (never trusting client aggregates) and stores one durable row. 400 on empty
    results; 404 if the skill is unknown.
    """
    body = await parse_json_object(request)
    payload = SkillTestRunCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    if not payload.results:
        raise HTTPException(status_code=400, detail="'results' must not be empty")

    size, passed, failed, partial, overall = _aggregate_skill_test_run(payload.results)
    now = datetime.now(timezone.utc)

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, payload.skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {payload.skill_id!r} not found")

        run = CaliberSkillTestRun(
            test_run_id=new_skill_test_run_id(),
            skill_id=payload.skill_id,
            skill_version=payload.skill_version,
            kind=payload.kind,
            test_set_size=size,
            passed_count=passed,
            failed_count=failed,
            partial_count=partial,
            overall_score=overall,
            results=[r.model_dump(mode="json") for r in payload.results],
            host_agent_id=payload.host_agent_id,
            trace_id=payload.trace_id,
            mlflow_run_id=payload.mlflow_run_id,
            created_by=actor,
            status="completed",
            completed_at=now,
        )
        session.add(run)
        session.flush()

        audit_record(
            session,
            actor=actor,
            action="create_skill_test_run",
            entity_type="skill_test_run",
            entity_id=run.test_run_id,
            details={
                "skill_id": run.skill_id,
                "kind": run.kind,
                "test_set_size": size,
                "passed_count": passed,
                "failed_count": failed,
                "partial_count": partial,
            },
        )
        session.commit()
        summary = SkillTestRunSummary.model_validate(run)

    return JSONResponse({"data": summary.model_dump(mode="json")}, status_code=201)


async def list_skill_test_runs(request: Request) -> JSONResponse:
    """``GET /caliber/skills/test-runs`` — newest-first run history (summaries).

    ``skill_id`` filters to one skill (all skills if omitted). ``kind`` filters
    to one run kind. ``limit`` defaults to 20 and is capped at 100. The heavy
    per-case ``results`` array is omitted.
    """
    require_user(request)
    skill_id = request.query_params.get("skill_id")
    kind = request.query_params.get("kind")

    raw_limit = request.query_params.get("limit")
    limit = _TEST_RUNS_DEFAULT_LIMIT
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
        if limit < 1:
            raise HTTPException(status_code=400, detail="'limit' must be >= 1")
    limit = min(limit, _TEST_RUNS_MAX_LIMIT)

    stmt = select(CaliberSkillTestRun)
    if skill_id:
        stmt = stmt.where(CaliberSkillTestRun.skill_id == skill_id)
    if kind:
        stmt = stmt.where(CaliberSkillTestRun.kind == kind)
    stmt = stmt.order_by(CaliberSkillTestRun.created_at.desc()).limit(limit)

    factory = get_session_factory(request)
    with factory() as session:
        rows = session.execute(stmt).scalars().all()
        summaries = [
            SkillTestRunSummary.model_validate(row).model_dump(mode="json") for row in rows
        ]

    return JSONResponse({"data": summaries})


async def get_skill_test_run(request: Request) -> JSONResponse:
    """``GET /caliber/skills/test-runs/{test_run_id}`` — full run incl. results."""
    require_user(request)
    test_run_id = request.path_params["test_run_id"]

    factory = get_session_factory(request)
    with factory() as session:
        run = session.get(CaliberSkillTestRun, test_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"test run {test_run_id!r} not found")
        detail = SkillTestRunDetail.model_validate(run)

    return JSONResponse({"data": detail.model_dump(mode="json")})


# ---------------------------------------------------------------------------
# Skill workspace + bind + baseline + agent-free calibrate ("pytest for skills")
# ---------------------------------------------------------------------------


async def get_skill_workspace(request: Request) -> JSONResponse:
    """``GET /caliber/skills/{skill_id}/workspace`` — runtime facts + lifecycle.

    Returns the skill's current version, category, the computed lifecycle
    ``status`` (Bound > Calibrated > Tested > Has scenarios > Draft), the latest
    run summary, the pinned baseline (id + summary), and the bind target. The
    signals are read off the hidden skill target (``skill::{name}``): ``bound_to``
    and ``baseline_run_id`` from its ``optimizer_config``; ``Calibrated`` from an
    ``applied`` skill refinement job for that target.
    """
    require_user(request)
    skill_id = request.path_params["skill_id"]

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        target_agent_id = skill_target_agent_id(skill.name)
        target = session.get(CaliberAgentConfig, target_agent_id)
        cfg = target.optimizer_config if target is not None else None
        cfg = cfg if isinstance(cfg, dict) else {}

        latest_run = (
            session.execute(
                select(CaliberSkillTestRun)
                .where(CaliberSkillTestRun.skill_id == skill_id)
                .order_by(CaliberSkillTestRun.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        last_run = (
            SkillWorkspaceLastRun.model_validate(latest_run) if latest_run is not None else None
        )

        has_test_run = latest_run is not None
        # "Has scenarios" has no dedicated store yet → base it on ≥1 run of a
        # selection/scenario kind (a render-only run isn't a scenario set).
        has_scenarios = (
            session.execute(
                select(CaliberSkillTestRun.test_run_id)
                .where(CaliberSkillTestRun.skill_id == skill_id)
                .where(CaliberSkillTestRun.kind.in_(_SCENARIO_RUN_KINDS))
                .limit(1)
            ).first()
            is not None
        )
        has_applied_job = (
            session.execute(
                select(CaliberRefinementJob.job_id)
                .where(CaliberRefinementJob.agent_id == target_agent_id)
                .where(CaliberRefinementJob.status == "applied")
                .limit(1)
            ).first()
            is not None
        )

        status = skill_target_status(
            target=target,
            has_test_run=has_test_run,
            has_applied_job=has_applied_job,
            has_scenarios=has_scenarios,
        )

        # Surface the pinned baseline (if any) plus a cheap summary. A stale id
        # (run since deleted, or no longer belonging to this skill) reads as no
        # baseline.
        raw_baseline_id = cfg.get("baseline_run_id")
        baseline_run_id = raw_baseline_id if isinstance(raw_baseline_id, str) else None
        baseline_run: SkillWorkspaceLastRun | None = None
        if baseline_run_id:
            baseline_row = session.get(CaliberSkillTestRun, baseline_run_id)
            if baseline_row is not None and baseline_row.skill_id == skill_id:
                baseline_run = SkillWorkspaceLastRun.model_validate(baseline_row)
            else:
                baseline_run_id = None

        bound_to = cfg.get("bound_to") if isinstance(cfg.get("bound_to"), dict) else None
        response = SkillWorkspaceResponse(
            version=skill.version,
            category=skill.category,
            status=status,
            # Lifecycle and status share the same precedence here (a skill has no
            # separate registry "published" notion like a tool); expose both keys
            # so the FE can use whichever it expects.
            lifecycle=status,
            last_run=last_run,
            baseline_run_id=baseline_run_id,
            baseline_run=baseline_run,
            bound_to=bound_to,
        )

    return JSONResponse({"data": response.model_dump(mode="json")})


async def set_skill_baseline(request: Request) -> JSONResponse:
    """``POST /caliber/skills/{skill_id}/baseline`` — pin a run as the baseline.

    Validates that ``test_run_id`` refers to an existing skill-test run AND that
    the run belongs to this skill (``skill_id`` matches); 404 if the run is
    missing, 400 if it belongs to a different skill. On success the id is recorded
    on the hidden skill target's ``optimizer_config.baseline_run_id`` (same
    precedent as the prompt target) so the Runs tab can diff against it.
    """
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request)
    payload = SkillBaselineRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        run = session.get(CaliberSkillTestRun, payload.test_run_id)
        if run is None:
            raise HTTPException(
                status_code=404, detail=f"test run {payload.test_run_id!r} not found"
            )
        if run.skill_id != skill_id:
            raise HTTPException(
                status_code=400,
                detail=(f"test run {payload.test_run_id!r} does not belong to skill {skill_id!r}"),
            )

        target = ensure_skill_target(
            session,
            skill.name,
            owner=actor,
            project_id=identity.active_project_id,
        )
        if isinstance(target.optimizer_config, dict):
            target.optimizer_config = {
                **target.optimizer_config,
                "baseline_run_id": payload.test_run_id,
            }

        audit_record(
            session,
            actor=actor,
            action="set_skill_baseline",
            entity_type="skill",
            entity_id=skill_id,
            details={"baseline_run_id": payload.test_run_id},
        )
        session.commit()

    return JSONResponse({"data": {"baseline_run_id": payload.test_run_id}}, status_code=200)


async def bind_skill(request: Request) -> JSONResponse:
    """``POST /caliber/skills/{skill_id}/bind`` — record where a skill is wired in.

    Records ``bound_to`` on the skill's hidden runtime target (created if absent),
    then performs the per-kind wiring:

    * ``agent`` — add this skill's name to the real agent's
      ``optimizer_config.skills`` list (that's how agents reference skills; see
      :func:`caliber.routes.agents._extract_skill_refs`).
    * ``workflow_node`` — record ``bound_to`` (manifest rewrite is left to the
      workflow editor / deploy path, as with prompt binding).
    * ``standalone`` — just record ``bound_to``.
    """
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request)
    payload = SkillBindRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    if payload.kind == "agent" and not payload.agent_id:
        raise HTTPException(status_code=400, detail="kind 'agent' requires 'agent_id'")
    if payload.kind == "workflow_node" and not (payload.workflow_id and payload.node_id):
        raise HTTPException(
            status_code=400,
            detail="kind 'workflow_node' requires 'workflow_id' and 'node_id'",
        )

    bound_to: dict[str, Any] = {"kind": payload.kind}
    if payload.kind == "agent":
        bound_to["agent_id"] = payload.agent_id
    elif payload.kind == "workflow_node":
        bound_to["workflow_id"] = payload.workflow_id
        bound_to["node_id"] = payload.node_id

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        target = ensure_skill_target(
            session,
            skill.name,
            owner=actor,
            project_id=identity.active_project_id,
        )
        if isinstance(target.optimizer_config, dict):
            target.optimizer_config = {**target.optimizer_config, "bound_to": bound_to}

        if payload.kind == "agent":
            agent = session.get(CaliberAgentConfig, payload.agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail=f"agent {payload.agent_id!r} not found")
            # Agents reference skills by name under optimizer_config.skills. Add
            # this skill's name (idempotent) so the runtime composes it.
            cfg = agent.optimizer_config if isinstance(agent.optimizer_config, dict) else {}
            skills_list = _extract_skill_refs(cfg)
            if skill.name not in skills_list:
                skills_list.append(skill.name)
            agent.optimizer_config = {**cfg, "skills": skills_list}

        audit_record(
            session,
            actor=actor,
            action="bind_skill",
            entity_type="skill",
            entity_id=skill_id,
            details={"bound_to": bound_to},
        )
        session.commit()

    return JSONResponse({"data": {"bound_to": bound_to, "status": "Bound"}}, status_code=200)


async def calibrate_skill(request: Request) -> JSONResponse:
    """``POST /caliber/skills/{skill_id}/calibrate`` — agent-free calibrate front door.

    Auto-provisions the hidden skill target (no "select an agent"), creates the
    ``skill_calibration`` verification item, and queues the refinement job using
    that hidden ``agent_id`` — mirroring how
    :func:`caliber.routes.prompts.enqueue_prompt_optimization_run` auto-provisions
    and enqueues. The verification item carries ``artifact_type_hint="skill"`` and
    ``artifact_ref=<skill name>`` so the triage stage routes it to
    ``artifact_type="skill"`` (the same signals the existing verify→job path
    produces). The generic verification-queue path is unchanged; this is the new
    agent-free entry point.
    """
    skill_id = request.path_params["skill_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = SkillCalibrateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    optimizer_type = (payload.optimizer_type or _DEFAULT_SKILL_OPTIMIZER).strip()

    factory = get_session_factory(request)
    with factory() as session:
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")

        # Auto-provision the hidden runtime identity for the skill so the
        # verification-item + refinement-job FKs (both reference
        # caliber_agent_config.agent_id) are satisfied without an operator-managed
        # agent. Idempotent: a second calibrate reuses the same target.
        target = ensure_skill_target(
            session,
            skill.name,
            owner=actor,
            project_id=identity.active_project_id,
        )

        item = CaliberVerificationItem(
            item_id=new_item_id(),
            agent_id=target.agent_id,
            category="skill_calibration",
            free_text=(payload.notes or f"Manual skill calibration run for {skill.name}"),
            severity="standard",
            # These two signals are what triage uses to route to a skill job
            # (see caliber.orchestrator.triage._classify_artifact).
            artifact_type_hint="skill",
            artifact_ref=skill.name,
            submitted_context={
                "source": "skill_calibration",
                "skill_calibration": {
                    "skill_id": skill.skill_id,
                    "skill_name": skill.name,
                    "optimizer_type": optimizer_type,
                },
            },
            status="verified",
            verified_by=actor,
            verified_at=datetime.now(timezone.utc),
            verification_notes=payload.notes,
            refinement_target="skill",
        )
        session.add(item)
        session.flush()

        job = CaliberRefinementJob(
            job_id=new_job_id(),
            agent_id=target.agent_id,
            primary_item_id=item.item_id,
            artifact_type="skill",
            skill_name=skill.name,
            optimizer_type=optimizer_type,
            status="queued",
            current_stage="triage",
            bundle_targets=[],
        )
        session.add(job)
        session.flush()

        audit_record(
            session,
            actor=actor,
            action="create_item",
            entity_type="verification_item",
            entity_id=item.item_id,
            details={
                "source": "skill_calibration",
                "severity": "standard",
                "linked_job_id": job.job_id,
            },
        )
        audit_record(
            session,
            actor=actor,
            action="create_job",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={
                "from_item_id": item.item_id,
                "agent_id": target.agent_id,
                "artifact_type": "skill",
                "skill_name": skill.name,
                "optimizer_type": optimizer_type,
            },
        )
        session.commit()
        item_data = VerificationItemSchema.model_validate(item)
        job_data = RefinementJobSchema.model_validate(job)

    return JSONResponse(
        {
            "data": {
                "item": item_data.model_dump(mode="json"),
                "job": job_data.model_dump(mode="json"),
            }
        },
        status_code=201,
    )


def register(app: Starlette) -> None:
    """Add the skill routes to the given Starlette application."""
    app.routes.append(Route(LIST_PATH, list_skills, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_skill, methods=["POST"]))
    # Register the literal ``/import-package`` POST before the ``{skill_id}``
    # param route so it is never captured as a skill id.
    app.routes.append(Route(IMPORT_PACKAGE_PATH, import_skill_package, methods=["POST"]))
    # Test-run routes must precede the ``{skill_id}`` DETAIL_PATH so
    # ``/skills/test-runs`` resolves here rather than being captured as a skill id.
    app.routes.append(Route(TEST_RUNS_PATH, create_skill_test_run, methods=["POST"]))
    app.routes.append(Route(TEST_RUNS_PATH, list_skill_test_runs, methods=["GET"]))
    app.routes.append(Route(TEST_RUN_DETAIL_PATH, get_skill_test_run, methods=["GET"]))
    app.routes.append(Route(PACKAGE_PATH, get_skill_package, methods=["GET"]))
    app.routes.append(Route(PACKAGE_ZIP_PATH, get_skill_package_zip, methods=["GET"]))
    app.routes.append(Route(TEST_RENDER_PATH, test_render_skill, methods=["POST"]))
    app.routes.append(Route(TEST_SELECTION_PATH, test_skill_selection, methods=["POST"]))
    app.routes.append(Route(WORKSPACE_PATH, get_skill_workspace, methods=["GET"]))
    app.routes.append(Route(BASELINE_PATH, set_skill_baseline, methods=["POST"]))
    app.routes.append(Route(BIND_PATH, bind_skill, methods=["POST"]))
    app.routes.append(Route(CALIBRATE_PATH, calibrate_skill, methods=["POST"]))
    app.routes.append(Route(ROLLBACK_PATH, rollback_skill, methods=["POST"]))
    app.routes.append(Route(VERSIONS_PATH, list_skill_versions, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_skill, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_skill, methods=["PATCH"]))
