"""Model-selected tool calls: what a model's choice of a tool may and may not do (B04.6, #768; ADR 0002).

In an agent loop the **model** chooses a tool and its arguments. Both are untrusted input: the arguments can be anything
the model emits, and the model's idea of what a tool does is a claim, never a classification. A call whose effect is
protected must therefore never run because a model asked for it. Two paths exist, and nothing else:

* **A governed step proposes** (``RuntimePlan.governed_agent_steps``, :mod:`caliber.effects.step_proposal`). The adapter is
  handed only the tools the platform classified ``read``; for any other tool the model may only *propose* one action. The
  platform, not the model, binds that proposal to the tool's trusted identity (the registry row, or the MCP server record
  and the administrator's tool policy), validates the model's arguments against the trusted input schema, records one
  ``proposed`` effect with its approval request and parks the run. :class:`ToolProposalBinder` is how the runtime tells
  ``step_proposal`` what a tool's proposal is, using the **same** classification helpers a workflow tool node uses
  (:func:`~caliber.effects.registered_tools.route_registered_tool`,
  :func:`~caliber.effects.registered_tools.route_openapi_tool`, :func:`~caliber.effects.connectors.mcp_tool.prepare`), so
  there is no weaker route for a model-chosen call than for a node-bound one.
* **The loop refuses** (every other plan that is governed: one wired to the registry or to a governed effect context). A
  protected call from an ungoverned loop is refused **before the tool is touched**, with a typed, payload-free code
  (:class:`ModelToolCallRefused`, :func:`refusal_marker`): the model sees ``{"_refused": true, "tool": ..., "code": ...}``.
  A read-class call keeps running inline.

The development profile with no governed context keeps its documented pre-dispatcher behaviour (it is **not** governed); a
plan that is not wired to the registry at all (a standalone export, a caller-built plan) is not governed either. Both are
named in the call-site inventory test, not hidden.

This module holds the codes and the small types; it reads no database and sends nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from caliber.workflows.ir import IRToolBinding

#: A protected call from a loop that is not a governed step: the tool is never run (the model may only propose, in a governed step).
PROTECTED_TOOL_REFUSED: Final = "protected_tool_requires_governed_step"
#: The tool's route is not one the platform can classify or carry (an unsupported path, an unreadable or different registry
#: row, a tool replaced by an injected callable): refused, whichever the profile.
MODEL_TOOL_ROUTE_REFUSED: Final = "model_tool_route_refused"
#: A governed effect context is needed and none is bound.
GOVERNED_CONTEXT_REQUIRED: Final = "governed_context_required"
UNSUPPORTED_EFFECT_PATH: Final = "unsupported_effect_path"
REGISTRY_ROW_MISMATCH: Final = "registry_row_mismatch"
REGISTRY_ROW_UNREADABLE: Final = "registry_row_unreadable"
TOOL_REPLACED: Final = "tool_replaced"
#: A proposal's arguments are not an object the tool's trusted input schema accepts.
TOOL_INPUT_INVALID: Final = "tool_input_invalid"
#: A governed step proposed a tool the platform classifies ``read`` (a read runs inline, it is never proposed).
TOOL_NOT_PROTECTED: Final = "tool_not_protected"

REFUSAL_CODES: Final = (
    PROTECTED_TOOL_REFUSED,
    MODEL_TOOL_ROUTE_REFUSED,
    GOVERNED_CONTEXT_REQUIRED,
    UNSUPPORTED_EFFECT_PATH,
    REGISTRY_ROW_MISMATCH,
    REGISTRY_ROW_UNREADABLE,
    TOOL_REPLACED,
    TOOL_INPUT_INVALID,
    TOOL_NOT_PROTECTED,
)


class ModelToolCallRefused(Exception):  # noqa: N818 - a refusal, named for what it says
    """A model's tool call was refused before the tool ran. ``code`` is a fixed token; the message is the code and nothing else."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def refusal_marker(tool: str, code: str) -> dict[str, Any]:
    """What the model is told when its call is refused: the tool it named and the code. No argument, result or exception text."""
    return {"_refused": True, "tool": tool, "code": code}


@dataclass(frozen=True)
class ToolProposal:
    """What a tool's proposal is, built by the platform from the tool's trusted identity and the model's validated arguments.

    Nothing here comes from the model's claim: the connector, resource and intent arguments are derived from the registry row
    or the MCP server record, and ``args`` embed the model's arguments only after they passed the trusted input schema.
    """

    #: ``caliber.effects.dispatcher.BACKENDS`` key of the backend that would send it (``registered_tool``, ``openapi_http``, ``mcp``).
    backend_kind: str
    connector: str
    resource: str
    args: dict[str, Any] = field(repr=False)
    #: The tool name the protected-effect inventory is asked about.
    tool_name: str
    #: True only for a tool the registry row proved CALIBER's own.
    caliber_defined: bool
    #: The class the route decided; the recorded class is the stricter of this and the inventory's answer.
    effect_floor: str
    expected_resource_version: str | None = None
    #: ``(name, version)`` of the registry row (what ``GovernedStepContext.tool_backends`` is keyed by), or ``None`` for MCP.
    registry: tuple[str, str] | None = None
    #: ``(server_id, tool_name)`` of an MCP tool (what ``GovernedStepContext.mcp_tools`` allows), or ``None``.
    mcp: tuple[str, str] | None = None


class ToolProposalBinder(Protocol):
    """The runtime's side of a model's tool proposal: classify and describe a bound tool from its trusted identity.

    :meth:`bind` raises :class:`~caliber.effects.dispatcher.EffectRefused` with a fixed code (never a value from the model)
    for a tool that cannot be proposed: not protected, refused by the inventory, a registry row that is unreadable or not the
    binding's, arguments that fail the trusted schema, an operation the row does not bind, an egress policy that refuses.
    """

    def connector(self, binding: IRToolBinding) -> str | None:
        """The connector this binding's proposals name, from its kind alone (``None`` for a binding that cannot be proposed)."""
        ...

    def static_resource(self, binding: IRToolBinding) -> str | None:
        """The resource this binding's proposals name when it does not depend on the arguments, else ``None``."""
        ...

    def bind(self, binding: IRToolBinding, arguments: Mapping[str, Any]) -> ToolProposal:
        """The proposal for ``arguments`` (plain JSON the model supplied): classified and described by the trusted identity."""
        ...


__all__ = [
    "GOVERNED_CONTEXT_REQUIRED",
    "MODEL_TOOL_ROUTE_REFUSED",
    "PROTECTED_TOOL_REFUSED",
    "REFUSAL_CODES",
    "REGISTRY_ROW_MISMATCH",
    "REGISTRY_ROW_UNREADABLE",
    "TOOL_INPUT_INVALID",
    "TOOL_NOT_PROTECTED",
    "TOOL_REPLACED",
    "UNSUPPORTED_EFFECT_PATH",
    "ModelToolCallRefused",
    "ToolProposal",
    "ToolProposalBinder",
    "refusal_marker",
]
