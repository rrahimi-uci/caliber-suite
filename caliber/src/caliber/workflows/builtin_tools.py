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
                module_path="caliber.workflows.ingestion_tools",
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
                module_path="caliber.workflows.memory_tools",
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


__all__ = [
    "BUILTIN_TOOL_DESCRIPTIONS",
    "BUILTIN_TOOL_MODULE",
    "BUILTIN_TOOL_OWNER",
    "BUILTIN_TOOL_SIDE_EFFECTS",
    "BUILTIN_TOOL_VERSION",
    "register_builtin_tools",
]
