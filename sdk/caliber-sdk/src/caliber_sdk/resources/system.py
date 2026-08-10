"""Identity, capabilities, and settings — the deployment-level surfaces."""

from __future__ import annotations

from typing import Any

from ..models._decode import decode, decode_list
from ..models.core import (
    Capabilities,
    Extensibility,
    Identity,
    LlmSetupStatus,
    OptimizerPlugin,
    RegisteredOptimizer,
    RuntimeSettings,
    RuntimeSettingsSummary,
    WorkflowRunCapabilities,
)
from ._base import Resource


class MeAPI(Resource):
    """The caller's own identity."""

    def get(self) -> Identity:
        """Resolve this client's identity and scopes.

        Reports rather than requires: an invalid or revoked credential returns
        an anonymous identity instead of raising. Check
        :attr:`Identity.is_anonymous`; do not rely on an exception.
        """
        return decode(Identity, self._get("/me"))


class CapabilitiesAPI(Resource):
    """Runtime feature flags and API stability tiers."""

    def get(self) -> Capabilities:
        payload = self._get("/capabilities")
        capabilities = decode(Capabilities, payload)
        # Nested payloads need decoding of their own; ``decode`` is one level.
        if isinstance(payload, dict):
            capabilities.workflow_runs = decode(
                WorkflowRunCapabilities, payload.get("workflow_runs")
            )
            capabilities.extensibility = self._decode_extensibility(payload.get("extensibility"))
        return capabilities

    @staticmethod
    def _decode_extensibility(payload: Any) -> Extensibility:
        """Two levels down, so decoded by hand rather than by ``decode``.

        Older servers predate the block entirely and send nothing; an empty
        :class:`Extensibility` is the right answer there, not an exception.
        """
        if not isinstance(payload, dict):
            return Extensibility()
        block = decode(Extensibility, payload)
        block.optimizers = decode_list(RegisteredOptimizer, payload.get("optimizers"))
        block.plugins = decode_list(OptimizerPlugin, payload.get("plugins"))
        return block


class SettingsAPI(Resource):
    """Runtime configuration inventory and LLM credential status."""

    def runtime(self) -> RuntimeSettings:
        payload = self._get("/settings/runtime")
        settings = decode(RuntimeSettings, payload)
        if isinstance(payload, dict):
            settings.summary = decode(RuntimeSettingsSummary, payload.get("summary"))
        return settings

    def llm(self) -> LlmSetupStatus:
        """Which LLM credentials are configured.

        Presence flags and masked fingerprints only — the endpoint does not
        disclose key values, deliberately.
        """
        return decode(LlmSetupStatus, self._get("/settings/llm"))

    def update_llm(self, **changes: Any) -> Any:
        """Write LLM provider settings. Secrets are write-only on the server."""
        return self._patch("/settings/llm", json=changes)


__all__ = ["CapabilitiesAPI", "MeAPI", "SettingsAPI"]
