"""Provider-neutral resource adapters for Workspace release operations.

Adapters are deliberately smaller than domain CRUD services.  They receive an
immutable revision pin and return a normalized prepared action or provider
outcome.  No provider SDK type crosses this boundary, which lets the release
operation service preserve an ambiguous outcome as ``reconcile_required``.

``apply_release``/``observe_release``/``rollback_release`` all receive the
*caller's own* :class:`~sqlalchemy.orm.Session` (`P5-E`) -- the same session
:func:`caliber.workspace_release_operations.apply_workspace_release_operation`
is already running inside a ``session.begin_nested()`` savepoint on. An
adapter whose "provider" genuinely is the same database (see
:mod:`caliber.workspace_release_workflow_adapter`) should use this session
directly and only ``flush()``, never ``commit()`` or open a second
connection: on SQLite (this project's default engine, including the whole
test suite) a second connection attempting to write while the caller's
transaction is still open blocks on the single-writer file lock for the
caller's *entire* remaining transaction, not just its own query -- an
unconditional wait, not a real timeout, since nothing releases that lock
until the outer transaction commits or rolls back. Sharing the session
avoids that deadlock entirely and is also more correct: the operation's own
state and the actual provider mutation commit or roll back as one atomic
unit, rather than needing ``reconcile_required`` to paper over a window
where one succeeded and the other did not. A genuinely external provider
(a real network call) still cannot share this transaction and keeps the
original two-phase apply/observe contract this session parameter does not
change.

``resolve()`` additionally receives the caller's ``identity`` (`P4-B`/`P4-C`,
added when ``routes/workspace.py::snapshot_revision`` became this Protocol's
first real caller for ``resolve()``/``snapshot()``). Typed as ``object`` here
for the same reason ``session``/``workspace``/``declaration`` are: this
module stays import-light, and a real adapter narrows the type itself (e.g.
``assert isinstance(identity, CaliberIdentity)``). An adapter resolving a
resource that has its own multi-tenant visibility model (a CALIBER prompt's
project/user/public tiers, for one -- see
:mod:`caliber.workspace_release_prompt_adapter`) must check ``identity``
against that model before returning content -- an adapter that only checks
``workspace``/project-id equality is not enough, since a `None` project id
on the target row can mean "public" (open to everyone) or "personal to one
specific owner" (`P4-B`/`P4-C` fixed exactly this gap for the prompt adapter,
the same disclosure `P2-N`/`P2-O` already closed for the prompt *lookup*
routes). :class:`FakeWorkspaceResourceAdapter` and
:class:`~caliber.workspace_release_workflow_adapter.WorkflowWorkspaceResourceAdapter`
still ignore ``identity`` in ``resolve()`` -- that method is an unused
placeholder for both today (see the workflow adapter's own module
docstring: "Not called anywhere yet"), not a resource this Protocol
addition has already audited for a real visibility model of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from caliber.db.models import (
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)

ProviderStatus = Literal["applied", "failed", "reconcile_required"]


class WorkspaceReleaseAdapterError(RuntimeError):
    """Base error for a provider/resource adapter refusal."""


class WorkspaceReleaseAdapterUnavailableError(WorkspaceReleaseAdapterError):
    """No adapter is registered for a revision resource type."""


class WorkspaceProviderTimeoutError(WorkspaceReleaseAdapterError):
    """A provider call timed out with a known effect boundary."""

    def __init__(self, message: str, *, effect_started: bool) -> None:
        super().__init__(message)
        self.effect_started = effect_started


@dataclass(frozen=True)
class PreparedAction:
    """Provider-neutral child intent prepared from one immutable resource pin."""

    resource_pin_id: str
    resource_type: str
    action: str
    target_ref: str
    before_ref: str | None
    after_ref: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("resource_pin_id", "resource_type", "action", "target_ref"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class SnapshotPin:
    """Content-addressed resource pin produced by a real adapter's ``snapshot()``.

    ``POST /projects/{id}/revisions:snapshot`` (`P4-C`,
    ``routes/workspace.py::snapshot_revision``) builds a
    :class:`~caliber.db.models.CaliberWorkspaceRevisionResource` row
    generically from this shape rather than special-casing each resource
    type's own ``snapshot()`` return value. Only an adapter whose
    ``snapshot()`` returns this type can participate in a managed snapshot --
    :class:`~caliber.workspace_release_workflow_adapter.WorkflowWorkspaceResourceAdapter`'s
    ``snapshot()`` still returns its input unchanged (its own documented
    placeholder), so ``workflow`` is not snapshot-eligible yet; the route
    fails closed on that rather than guessing at a shape.
    """

    resource_id: str
    version_ref: str
    content_sha256: str
    provider_ref: str | None = None
    resolution: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("resource_id", "version_ref", "content_sha256"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class ProviderOutcome:
    """Normalized provider result; raw payloads stay inside an adapter."""

    status: ProviderStatus
    provider_operation_ref: str
    provider_result: Mapping[str, object] = field(default_factory=dict)
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"applied", "failed", "reconcile_required"}:
            raise ValueError(f"unsupported provider outcome {self.status!r}")
        if not self.provider_operation_ref.strip():
            raise ValueError("provider_operation_ref must not be empty")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed provider outcomes require error_code")
        if self.status == "reconcile_required" and not self.error_code:
            raise ValueError("ambiguous provider outcomes require error_code")


class WorkspaceResourceAdapter(Protocol):
    """Capability-declaring adapter contract for one Workspace resource type."""

    resource_type: str

    def resolve(
        self, session: object, workspace: object, declaration: object, identity: object
    ) -> object: ...  # pragma: no cover

    def snapshot(self, session: object, resolved_pin: object) -> object: ...  # pragma: no cover

    def validate(
        self,
        session: object,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment | None = None,
    ) -> object: ...  # pragma: no cover

    def prepare_release(
        self,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment,
        *,
        before_ref: str | None,
    ) -> PreparedAction: ...  # pragma: no cover

    def apply_release(
        self, session: object, prepared: PreparedAction
    ) -> ProviderOutcome: ...  # pragma: no cover

    def observe_release(
        self, session: object, prepared: PreparedAction
    ) -> ProviderOutcome: ...  # pragma: no cover

    def rollback_release(
        self, session: object, prepared: PreparedAction
    ) -> ProviderOutcome: ...  # pragma: no cover


class WorkspaceResourceAdapterRegistry:
    """Explicit adapter registry; it never creates network clients implicitly."""

    def __init__(self, adapters: Mapping[str, WorkspaceResourceAdapter] | None = None) -> None:
        self._adapters: dict[str, WorkspaceResourceAdapter] = {}
        for adapter in (adapters or {}).values():
            self.register(adapter)

    def register(self, adapter: WorkspaceResourceAdapter, *, replace: bool = False) -> None:
        name = str(adapter.resource_type).strip().lower()
        if not name:
            raise ValueError("resource adapter type must not be empty")
        if name in self._adapters and not replace:
            raise ValueError(f"resource adapter {name!r} is already registered")
        self._adapters[name] = adapter

    def get(self, resource_type: str) -> WorkspaceResourceAdapter | None:
        return self._adapters.get(str(resource_type).strip().lower())

    def require(self, resource_type: str) -> WorkspaceResourceAdapter:
        adapter = self.get(resource_type)
        if adapter is None:
            raise WorkspaceReleaseAdapterUnavailableError(
                f"no Workspace resource adapter is registered for {resource_type!r}"
            )
        return adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class FakeWorkspaceResourceAdapter:
    """Deterministic adapter for offline release and failure-mode tests."""

    def __init__(self, resource_type: str = "fake") -> None:
        self.resource_type = resource_type
        self.state: dict[str, str | None] = {}
        self.apply_calls: list[PreparedAction] = []
        self.observe_calls: list[PreparedAction] = []
        self.rollback_calls: list[PreparedAction] = []
        self._queued_apply: list[ProviderOutcome | WorkspaceProviderTimeoutError] = []
        self._queued_observe: list[ProviderOutcome] = []

    def resolve(
        self, _session: object, _workspace: object, declaration: object, _identity: object
    ) -> object:
        return declaration

    def snapshot(self, _session: object, resolved_pin: object) -> object:
        return resolved_pin

    def validate(
        self,
        _session: object,
        pin: CaliberWorkspaceRevisionResource,
        _environment: CaliberWorkspaceEnvironment | None = None,
    ) -> object:
        return {"valid": True, "resource_pin_id": pin.resource_pin_id}

    def prepare_release(
        self,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment,
        *,
        before_ref: str | None,
    ) -> PreparedAction:
        target_ref = pin.provider_ref or f"{pin.resource_id}@{pin.version_ref}"
        return PreparedAction(
            resource_pin_id=pin.resource_pin_id,
            resource_type=self.resource_type,
            action="promote",
            target_ref=target_ref,
            before_ref=before_ref,
            after_ref=target_ref,
            metadata={"environment_id": environment.environment_id},
        )

    def queue_apply(self, outcome: ProviderOutcome | WorkspaceProviderTimeoutError) -> None:
        self._queued_apply.append(outcome)

    def queue_observe(self, outcome: ProviderOutcome) -> None:
        self._queued_observe.append(outcome)

    def apply_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        self.apply_calls.append(prepared)
        outcome = self._queued_apply.pop(0) if self._queued_apply else None
        if isinstance(outcome, WorkspaceProviderTimeoutError):
            if outcome.effect_started:
                self.state[prepared.target_ref] = prepared.after_ref
            raise outcome
        if outcome is None:
            outcome = ProviderOutcome("applied", f"fake:{len(self.apply_calls)}")
        if outcome.status == "applied":
            self.state[prepared.target_ref] = prepared.after_ref
        return outcome

    def observe_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        self.observe_calls.append(prepared)
        if self._queued_observe:
            return self._queued_observe.pop(0)
        if self.state.get(prepared.target_ref) == prepared.after_ref:
            return ProviderOutcome("applied", f"fake-observe:{len(self.observe_calls)}")
        return ProviderOutcome(
            "failed",
            f"fake-observe:{len(self.observe_calls)}",
            error_code="target_not_observed",
            error_summary="fake provider target does not match the prepared after_ref",
        )

    def rollback_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        self.rollback_calls.append(prepared)
        self.state[prepared.target_ref] = prepared.after_ref
        return ProviderOutcome("applied", f"fake-rollback:{len(self.rollback_calls)}")


__all__ = [
    "FakeWorkspaceResourceAdapter",
    "PreparedAction",
    "ProviderOutcome",
    "SnapshotPin",
    "WorkspaceProviderTimeoutError",
    "WorkspaceReleaseAdapterError",
    "WorkspaceReleaseAdapterUnavailableError",
    "WorkspaceResourceAdapter",
    "WorkspaceResourceAdapterRegistry",
]
