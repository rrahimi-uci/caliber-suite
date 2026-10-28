"""Built-in CALIBER tool registry rows.

The callables live in :mod:`caliber.workflows.demo_tools` because they are
small, dependency-free implementations used by demos and tests. This module is
the production-facing registry seed: UI pages and workflow validation resolve
registered database rows, so the built-ins must be present even when the demo
workflow scenario has not been seeded.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberToolRegistry
from caliber.ids import new_tool_id

BUILTIN_TOOL_MODULE = "caliber.workflows.demo_tools"
INGESTION_TOOL_MODULE = "caliber.workflows.ingestion_tools"
MEMORY_TOOL_MODULE = "caliber.workflows.memory_tools"
BUILTIN_TOOL_OWNER = "@caliber"
BUILTIN_TOOL_VERSION = "1.0"

# Maps tool family name -> side-effect level. Read tools can run during preview;
# write / external-action tools are approval-gated and mocked by the sandbox.
BUILTIN_TOOL_SIDE_EFFECTS: dict[str, str] = {
    # Default file / sandbox / working-directory tools. The demo-scenario tools
    # (support / travel / orders) are NOT seeded — their callables remain in
    # demo_tools.py purely as test scaffolding.
    "read_text_file": "read",
    "list_folder_files": "read",
    "grep_files": "read",
    "regex_search": "read",
    "grok_parse": "read",
    "sandbox_python": "external_action",
    # Run working-directory file tools (storage doc §4.4). Real run-scoped
    # implementations are injected at execution time via execute(extra_tools=...).
    "list_workdir_files": "read",
    "read_workdir_file": "read",
    "get_file_metadata": "read",
    "write_workdir_file": "write",
    "create_artifact": "write",
}

BUILTIN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_text_file": "Read a bounded local text file for document-processing workflows.",
    "list_folder_files": "List files in a local folder using a glob pattern.",
    "grep_files": "Find literal text matches across files.",
    "regex_search": "Search text or a local file with a Python regular expression.",
    "grok_parse": "Parse log-like text with a small Grok-compatible pattern subset.",
    "sandbox_python": "Run a short Python snippet in an isolated temporary directory.",
    "list_workdir_files": "List files in the workflow run's working directory.",
    "read_workdir_file": "Read a file from the run working directory or File Directory by file reference.",
    "get_file_metadata": "Return size, hash, media type, and provenance for a run or File Directory file.",
    "write_workdir_file": "Write an intermediate file into the run working directory.",
    "create_artifact": "Write and register a final artifact in the run working directory.",
}


def register_builtin_tools(session: Session) -> int:
    """Ensure built-in tool registry rows exist.

    Returns the number of rows created. The operation is idempotent and uses the
    same name/version uniqueness contract as admin-registered tools.
    """
    existing = set(
        session.execute(
            select(CaliberToolRegistry.name).where(
                CaliberToolRegistry.name.in_(BUILTIN_TOOL_SIDE_EFFECTS),
                CaliberToolRegistry.version == BUILTIN_TOOL_VERSION,
            )
        ).scalars()
    )
    created = 0
    for name, side_effect in BUILTIN_TOOL_SIDE_EFFECTS.items():
        if name in existing:
            continue

        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name=name,
                version=BUILTIN_TOOL_VERSION,
                description=BUILTIN_TOOL_DESCRIPTIONS.get(
                    name, f"Built-in {name.replace('_', ' ')} tool."
                ),
                module_path=BUILTIN_TOOL_MODULE,
                callable_name=name,
                side_effect_level=side_effect,
                requires_approval=side_effect != "read",
                allow_in_preview=side_effect == "read",
                owner=BUILTIN_TOOL_OWNER,
                # Built-in tools are infrastructure — visible to every user.
                visibility="public",
                status="active",
            )
        )
        created += 1

    # Multi-format ingestion tool — its callable lives in a dedicated module
    # (real in-process parsing; cannot run in the python_code sandbox), so it is
    # registered separately from the demo-tools family above.
    ingest_exists = (
        session.execute(
            select(CaliberToolRegistry.name).where(
                CaliberToolRegistry.name == "extract_document",
                CaliberToolRegistry.version == BUILTIN_TOOL_VERSION,
            )
        )
        .scalars()
        .first()
    )
    if ingest_exists is None:
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name="extract_document",
                version=BUILTIN_TOOL_VERSION,
                description=(
                    "Extract plain text from a PDF/DOCX/PPTX/XLSX/Markdown/text document "
                    "for ingestion into document-processing workflows."
                ),
                module_path=INGESTION_TOOL_MODULE,
                callable_name="extract_document",
                side_effect_level="read",
                requires_approval=False,
                allow_in_preview=True,
                owner=BUILTIN_TOOL_OWNER,
                visibility="public",
                status="active",
            )
        )
        created += 1

    # Agent memory tools — callables live in ``caliber.workflows.memory_tools``
    # (registry stubs; run-scoped implementations are injected at execution time
    # via ``execute(extra_tools=...)``). Search/list/entity introspection are
    # read-only and preview-safe; ``memory_add`` writes, so it is
    # approval-gated like other write tools.
    memory_tools = {
        "memory_search": (
            "read",
            "Recall relevant long-term memories for this agent (semantic search).",
        ),
        "memory_list": (
            "read",
            "List long-term memories currently stored for this agent scope.",
        ),
        "list_entities": (
            "read",
            "List extracted memory entities and the memories they link to for this agent scope.",
        ),
        "memory_add": ("write", "Persist a fact to this agent's long-term memory for future runs."),
    }
    existing_memory = set(
        session.execute(
            select(CaliberToolRegistry.name).where(
                CaliberToolRegistry.name.in_(memory_tools),
                CaliberToolRegistry.version == BUILTIN_TOOL_VERSION,
            )
        ).scalars()
    )
    for name, (side_effect, description) in memory_tools.items():
        if name in existing_memory:
            continue
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name=name,
                version=BUILTIN_TOOL_VERSION,
                description=description,
                module_path=MEMORY_TOOL_MODULE,
                callable_name=name,
                side_effect_level=side_effect,
                requires_approval=side_effect != "read",
                allow_in_preview=side_effect == "read",
                owner=BUILTIN_TOOL_OWNER,
                visibility="public",
                status="active",
            )
        )
        created += 1

    session.flush()
    return created


#: The tool names CALIBER seeds itself. A built-in is recognised by name *and* owner *and*
#: module, because an owner is free text on registration and a user-registered tool must not
#: be able to pass itself off as one.
MEMORY_TOOL_NAMES = frozenset({"memory_search", "memory_list", "list_entities", "memory_add"})
BUILTIN_TOOL_NAMES = frozenset(BUILTIN_TOOL_SIDE_EFFECTS) | {"extract_document"} | MEMORY_TOOL_NAMES
_BUILTIN_TOOL_MODULES = frozenset({BUILTIN_TOOL_MODULE, INGESTION_TOOL_MODULE, MEMORY_TOOL_MODULE})

MEMORY_UNAVAILABLE_REASON = (
    "Agent memory is off, so this tool answers with an error. "
    "Set CALIBER_MEMORY_ENABLED=true and restart CALIBER to use it."
)


def is_builtin_tool(*, name: str, owner: str, module_path: str) -> bool:
    """Whether a registry row is one CALIBER seeded, as opposed to one an admin registered."""
    return (
        owner == BUILTIN_TOOL_OWNER
        and name in BUILTIN_TOOL_NAMES
        and module_path in _BUILTIN_TOOL_MODULES
    )


#: The refusal code a registration path reports when a tool is named like a built-in.
RESERVED_TOOL_NAME_CODE = "reserved_tool_name"


def reserved_tool_name(name: str) -> str | None:
    """The built-in ``name`` is reserved for, or ``None`` when a tool may take it.

    The protected-effect inventory classifies a built-in by its *name*, and the registry is unique only
    on (name, version), so a tool registered as ``grep_files`` would be indistinguishable by name from the
    one CALIBER seeds. Registration therefore refuses every name a built-in owns, whoever the owner is
    (``owner`` is free text, so :func:`is_builtin_tool` is the check for a row that exists, not a gate for
    a row about to be written). The comparison is deliberately stricter than the resolver's exact match:
    surrounding whitespace and case are ignored, and so are the ``tool.`` prefix and the ``.vN`` suffix a
    manifest reference adds (:func:`caliber.workflows.tools.family_name`), so ``Grep_Files`` and
    ``tool.grep_files.v2`` are the built-in's name too. CALIBER's own seeding writes rows directly and never calls this.
    """
    from caliber.workflows.tools import family_name  # noqa: PLC0415

    folded = name.strip().casefold()
    for candidate in (folded, family_name(folded)):
        if candidate in BUILTIN_TOOL_NAMES:
            return candidate
    return None


def reserved_tool_name_message(name: str, builtin: str) -> str:
    """The sentence a refusal carries: what is reserved, why, and what to do instead."""
    return (
        f"{RESERVED_TOOL_NAME_CODE}: tool name {name!r} is reserved for the built-in CALIBER tool "
        f"{builtin!r}, which the protected-effect inventory classifies by name; register your tool "
        "under a different name"
    )


def tool_availability(
    *, name: str, owner: str, module_path: str, memory_enabled: bool
) -> tuple[str, str | None]:
    """``("available", None)`` or ``("unavailable", reason)`` for the registry list.

    The memory tools are seeded as ``active`` whether or not memory is enabled, but while it
    is off each one only returns an error dict. Listing them as ordinary active tools lets a
    workflow author wire one in and learn at run time that it does nothing.
    """
    if (
        not memory_enabled
        and name in MEMORY_TOOL_NAMES
        and is_builtin_tool(name=name, owner=owner, module_path=module_path)
    ):
        return "unavailable", MEMORY_UNAVAILABLE_REASON
    return "available", None


__all__ = [
    "BUILTIN_TOOL_DESCRIPTIONS",
    "BUILTIN_TOOL_MODULE",
    "BUILTIN_TOOL_NAMES",
    "BUILTIN_TOOL_OWNER",
    "BUILTIN_TOOL_SIDE_EFFECTS",
    "BUILTIN_TOOL_VERSION",
    "INGESTION_TOOL_MODULE",
    "MEMORY_TOOL_MODULE",
    "MEMORY_TOOL_NAMES",
    "MEMORY_UNAVAILABLE_REASON",
    "RESERVED_TOOL_NAME_CODE",
    "is_builtin_tool",
    "register_builtin_tools",
    "reserved_tool_name",
    "reserved_tool_name_message",
    "tool_availability",
]
