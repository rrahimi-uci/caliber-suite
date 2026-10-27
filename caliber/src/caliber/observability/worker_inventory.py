"""Machine-readable inventory of CALIBER's background workers.

Part of `P0-A` (`docs/workspace-plan.md` section 16, Phase 0 item 2): after
the route/scope inventory (`routes/scope_inference.py`), this is the worker
slice. Sibling to :mod:`caliber.observability.worker_registry` -- that
module answers "who's alive right now" (a live, DB-backed heartbeat table);
this one answers "what background workers does this codebase have," derived
from the code rather than declared, so it can't silently drift the way
`server.py::_build_lifespan`'s own docstring admits its startup-order prose
already does: *"This list is prose and drifts;
`paper/scripts/gen_stats.py` derives the loop count from the `await
<task>.start()` calls below rather than from here."*

That script's `_lifespan_loops()` already solves the "which loops does the
lifespan start" half of this problem with a proven `ast` walk over
`_build_lifespan`'s nested `lifespan()` function -- this module ports the
same technique (rather than importing across the `paper`/`caliber`
package boundary, which is unrelated coupling for four lines of logic) and
extends it: `_build_lifespan`'s own typed parameters
(``worker: RefinementWorker``, ``workflow_run_worker: WorkflowRunWorker |
None``, ...) give each loop's *class*, not just its local variable name,
from the same single AST read.

What isn't safely derivable this way: which DB tables a worker's tick
method actually touches. A tick method is an arbitrary, often-large
function; unlike a route's small, bounded `require_scopes(...)` call
pattern, reliably identifying every model a tick body reads or writes via
static analysis risks false confidence -- a wrong claim here is worse than
none. `_WORKER_NOTES` carries that as a small, hand-maintained field
instead (the same weight `_STABILITY`/`_DYNAMIC_SCOPE_NOTES` carry
elsewhere), verified only for each table *name* actually existing on
:mod:`caliber.db.models` (catches a typo or a rename), not for completeness.

``registers_heartbeat`` *is* safely derivable, the same bounded-call-detection
shape `routes/scope_inference.py` uses for authorization calls: does the
worker class's defining module contain a call to
:func:`caliber.observability.worker_registry.record_heartbeat`.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from dataclasses import dataclass
from types import ModuleType
from typing import Literal, get_args

_LIFESPAN_BUILDER = "_build_lifespan"
_LIFESPAN_CALLBACK = "start_owner_loops"


#: Who may run a background loop once the execution-owner lease exists
#: (ADR 0002, #536). ``owner_only``: only the process holding the lease runs
#: it and it stops claiming when the lease is lost; every other replica
#: serves requests only. ``any_process``: safe on every replica at once.
Ownership = Literal["owner_only", "any_process"]

OWNERSHIP_VALUES: tuple[str, ...] = get_args(Ownership)


@dataclass(frozen=True)
class WorkerNote:
    """Hand-maintained detail for one worker, keyed by class name."""

    description: str
    tables: tuple[str, ...]
    ownership: Ownership

    def __post_init__(self) -> None:
        if self.ownership not in OWNERSHIP_VALUES:
            raise ValueError(f"ownership must be one of {OWNERSHIP_VALUES}, got {self.ownership!r}")


#: One entry per background worker, keyed by class name (the stable
#: identity -- `_build_lifespan`'s parameter names are local implementation
#: detail). A class `discover_lifespan_workers()` finds with no entry here,
#: or an entry naming a class no longer found, fails
#: `tests/test_worker_inventory.py` by name.
#:
#: Every worker is ``owner_only`` (ADR 0002: only the execution-owner lease
#: holder runs the loops). Per-row claims (an atomic ``UPDATE ... WHERE
#: status = 'queued'``, a job lease, an idempotency key) make an overlap
#: harmless today for most loops, but none fences a stale process whose lease
#: expired mid-transition, and ``AriaPlanWorker`` and
#: ``ReleaseReconcilerTask`` have no per-row claim at all. ``any_process`` is
#: therefore empty until a loop is shown safe under fencing, and moving one
#: there is an ADR 0002 change.
_WORKER_NOTES: dict[str, WorkerNote] = {
    "RefinementWorker": WorkerNote(
        description="Advances refinement jobs through the 6-stage pipeline.",
        tables=("CaliberRefinementJob", "CaliberAgentConfig", "CaliberAuditLog"),
        ownership="owner_only",
    ),
    "WorkflowRunWorker": WorkerNote(
        description="Executes queued workflow runs and their step checkpoints.",
        tables=(
            "CaliberProject",
            "CaliberWorkflow",
            "CaliberWorkflowVersion",
            "CaliberWorkflowRun",
            "CaliberWorkflowRunCheckpoint",
            "CaliberRuntimeApprovalRequest",
            "CaliberWorkerHeartbeat",
        ),
        ownership="owner_only",
    ),
    "AriaPlanWorker": WorkerNote(
        description="Resumes and advances durable Aria goal-plan execution.",
        tables=("CaliberAriaPlanStep",),
        ownership="owner_only",
    ),
    "KnowledgeBaseWorker": WorkerNote(
        description="Runs knowledge-base build/sync jobs and their events.",
        tables=(
            "CaliberKnowledgeBase",
            "CaliberKnowledgeBaseRun",
            "CaliberKnowledgeBaseRunEvent",
            "CaliberKnowledgeBaseVersion",
        ),
        ownership="owner_only",
    ),
    "WorkflowSchedulerTask": WorkerNote(
        description="Sweeps cron-scheduled workflow deployments and enqueues runs.",
        tables=("CaliberWorkflow", "CaliberWorkflowDeployment", "CaliberWorkflowVersion"),
        ownership="owner_only",
    ),
    "JanitorTask": WorkerNote(
        description="Reclaims stuck refinement jobs and prunes retained workflow runs.",
        tables=("CaliberRefinementJob", "CaliberAuditLog", "CaliberWorkflowRun"),
        ownership="owner_only",
    ),
    "ReleaseReconcilerTask": WorkerNote(
        description="Settles incomplete prompt-alias release operations after missed webhooks.",
        tables=("CaliberReleaseOperation",),
        ownership="owner_only",
    ),
    "CalibrationDrain": WorkerNote(
        description="Drains queued tool-calibration jobs against the live registry.",
        tables=("CaliberCalibrationJob", "CaliberToolRegistry"),
        ownership="owner_only",
    ),
    "WebhookDispatcher": WorkerNote(
        description="Delivers accepted outbound webhook events; retries into the dead-letter queue.",
        tables=("CaliberWebhookAcceptedEvent", "CaliberWebhookDeadLetter"),
        ownership="owner_only",
    ),
    "WorkspaceImportWorker": WorkerNote(
        description=(
            "Claims queued push-mode Workspace import jobs and materializes each into an "
            "immutable WorkspaceRevision."
        ),
        tables=(
            "CaliberWorkspaceImportJob",
            "CaliberWorkspaceSource",
            "CaliberWorkflowFile",
            "CaliberWorkspaceRevision",
            "CaliberWorkspaceRevisionResource",
            "CaliberProject",
        ),
        ownership="owner_only",
    ),
}


@dataclass(frozen=True)
class WorkerDescriptor:
    """One background worker, cross-referencing the derived lifespan
    registration against its hand-maintained note."""

    name: str  # the _build_lifespan parameter name, e.g. "workflow_run_worker"
    cls_name: str  # e.g. "WorkflowRunWorker"
    description: str | None
    tables: tuple[str, ...]
    registers_heartbeat: bool
    ownership: Ownership | None


def _strip_optional(annotation: ast.expr) -> ast.expr:
    """``X | None`` -> ``X``. `_build_lifespan` types every optional worker
    this way (never `Optional[X]`), so only the ``|`` form needs handling."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        for side in (annotation.left, annotation.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                return side
    return annotation


def _annotation_name(annotation: ast.expr | None) -> str | None:
    resolved = _strip_optional(annotation) if annotation is not None else None
    if isinstance(resolved, ast.Name):
        return resolved.id
    return None


def discover_lifespan_workers() -> list[tuple[str, str]]:
    """``(param_name, class_name)`` for every ``await <name>.start()`` inside
    ``server.py::_build_lifespan``'s nested ``start_owner_loops()`` function
    (what the lifespan calls, through the execution-role controller, to start the
    owner-only loops), in startup order.

    Ports `paper/scripts/gen_stats.py::_lifespan_loops`'s exact AST-walk
    technique (matching ``await <name>.start()``, deliberately not matching
    the event bus's ``getattr``-handle start -- it's a transport, not a
    drain loop) and extends it to resolve each name's class from
    ``_build_lifespan``'s own parameter annotations, read in the same pass.
    """
    # Deferred: server.py is the app's composition root, importing nearly
    # everything else. A module-level import here would risk a future
    # circular import the moment anything server.py transitively pulls in
    # wants this inventory too.
    from caliber import server as server_module  # noqa: PLC0415

    source = textwrap.dedent(inspect.getsource(server_module))
    tree = ast.parse(source)

    builder = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == _LIFESPAN_BUILDER
        ),
        None,
    )
    if builder is None:
        return []

    annotations: dict[str, str] = {}
    for arg in builder.args.args + builder.args.kwonlyargs:
        name = _annotation_name(arg.annotation)
        if name is not None:
            annotations[arg.arg] = name

    lifespan_fn = next(
        (
            node
            for node in ast.walk(builder)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == _LIFESPAN_CALLBACK
        ),
        None,
    )
    if lifespan_fn is None:
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in ast.walk(lifespan_fn):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "start"
            and isinstance(func.value, ast.Name)
            and func.value.id not in seen
        ):
            param_name = func.value.id
            seen.add(param_name)
            cls_name = annotations.get(param_name, param_name)
            found.append((param_name, cls_name))
    return found


def _module_source_mentions_heartbeat(cls_name: str) -> bool:
    """Whether the worker class's defining module contains a
    ``record_heartbeat(`` call anywhere -- a coarse but cheap and reliable
    signal, avoiding a full per-method AST walk of an arbitrary class body.
    """
    module = _worker_class_module(cls_name)
    if module is None:
        return False
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        return False
    return "record_heartbeat(" in source


#: Every module that currently defines one of the 9 known workers. A worker
#: added in a new module needs a line here -- `test_worker_inventory.py`'s
#: completeness gate fails loudly (heartbeat derivation silently returns
#: `False`, not an exception) if this list falls behind, so the failure is
#: "wrong answer caught by a test," not "wrong answer no one notices."
_WORKER_MODULE_NAMES = (
    "caliber.orchestrator.worker",
    "caliber.orchestrator.workflow_run_worker",
    "caliber.assistant.plan_worker",
    "caliber.knowledge.worker",
    "caliber.orchestrator.scheduler",
    "caliber.orchestrator.janitor",
    "caliber.orchestrator.release_reconciler",
    "caliber.orchestrator.calibration_drain",
    "caliber.events.webhooks",
)


def _worker_class_module(cls_name: str) -> ModuleType | None:
    """Import each known worker module and return whichever one defines
    ``cls_name`` -- avoids hardcoding a name-to-module map that would
    itself be a second copy of the truth to keep in sync."""
    for module_name in _WORKER_MODULE_NAMES:
        module = importlib.import_module(module_name)
        if hasattr(module, cls_name):
            return module
    return None


def worker_inventory() -> list[WorkerDescriptor]:
    """The full, cross-referenced worker inventory."""
    descriptors: list[WorkerDescriptor] = []
    for param_name, cls_name in discover_lifespan_workers():
        note = _WORKER_NOTES.get(cls_name)
        descriptors.append(
            WorkerDescriptor(
                name=param_name,
                cls_name=cls_name,
                description=note.description if note else None,
                tables=note.tables if note else (),
                registers_heartbeat=_module_source_mentions_heartbeat(cls_name),
                ownership=note.ownership if note else None,
            )
        )
    return descriptors


__all__ = [
    "OWNERSHIP_VALUES",
    "Ownership",
    "WorkerDescriptor",
    "WorkerNote",
    "discover_lifespan_workers",
    "worker_inventory",
]
