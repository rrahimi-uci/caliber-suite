"""Identity, capabilities, and settings — the deployment-level surfaces."""

from __future__ import annotations

from typing import Any

from ..models._decode import decode
from ..models.core import (
    Capabilities,
    Identity,
    LlmSetupStatus,
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
        return capabilities


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
