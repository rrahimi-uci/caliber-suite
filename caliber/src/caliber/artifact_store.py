"""Artifact store abstraction.

CALIBER needs to read the *current* content of an agent's active artifact in
several places: the candidate stage feeds it into the LLM as the prompt being
rewritten, the eval stage runs both old and new through ``mlflow.genai.evaluate``
to compute regression deltas, and the approval-detail UI renders the diff.

We hide that read behind :class:`ArtifactStore` so each call site doesn't
need to know whether the value comes from the MLflow Prompt Registry, a
filesystem snapshot, or a test fake. Same dependency-injection shape as
:class:`caliber.mlflow_client.MLflowAssessmentClient` and
:class:`caliber.llm.provider.LLMProvider`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("caliber.artifact_store")


class ArtifactStore(Protocol):
    """The reads CALIBER actually depends on.

    ``get_active_prompt`` returns the content currently deployed for the
    named agent — typically the prompt aliased to ``@prod`` in the MLflow
    Prompt Registry. Returns ``None`` when nothing is registered yet
    (cold-start path; the LLM provider handles this gracefully).

    ``get_active_skill`` returns the content of an active skill by name.
    Returns ``None`` when the skill doesn't exist or is archived.
    """

    def get_active_prompt(self, agent_id: str) -> str | None: ...

    def get_active_skill(self, skill_name: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class MLflowArtifactStore:
    """Production store backed by the MLflow Prompt Registry.

    The naming convention is ``{agent_id}@prod`` — the same convention the
    demo code uses (``mlflow.load_prompt("support-agent@prod")``).

    A missing prompt or any other MLflow error is logged and returns
    ``None`` rather than raising. The orchestrator's stages tolerate
    ``None`` so a fresh CALIBER deployment can still run its first
    refinement cycle.
    """

    def __init__(self, alias: str = "prod", session_factory: Any | None = None) -> None:
        self._alias = alias
        self._session_factory = session_factory

    def get_active_prompt(self, agent_id: str) -> str | None:
        # Lazy import: see caliber.mlflow_client for the rationale.
        try:
            import mlflow  # noqa: PLC0415
        except ImportError:
            logger.warning("mlflow not installed; artifact_store returns None")
            return None
        # MLflow 3.13+ moved the prompt-registry surface into
        # ``mlflow.genai``; the top-level alias still works but emits
        # ``FutureWarning``. Prefer the new namespace.
        load_prompt = getattr(getattr(mlflow, "genai", None), "load_prompt", None) or getattr(
            mlflow, "load_prompt", None
        )
        if load_prompt is None:
            logger.warning("mlflow has no load_prompt API; artifact_store returns None")
            return None
        # The URI must be ``prompts:/<name>@<alias>``. A bare ``<name>@<alias>``
        # is *not* parsed as an alias ref by MLflow 3.13+ — it tries to look
        # up a prompt literally named ``<name>@<alias>`` and fails. The
        # ``prompts:/`` prefix is the explicit URI form documented for the
        # prompt registry.
        ref = f"prompts:/{agent_id}@{self._alias}"
        try:
            prompt = load_prompt(ref, allow_missing=True)
        except Exception:
            logger.exception("failed to load prompt %s", ref)
            return None
        if prompt is None:
            # ``allow_missing=True`` returns None for nonexistent prompts
            # rather than raising — exactly the cold-start path we want.
            return None
        # MLflow 3.12 ``PromptVersion.template`` is the canonical attribute.
        # Older builds exposed ``content`` — accept both so a minor SDK
        # version drift doesn't surface as "prompt has no string content."
        content = getattr(prompt, "template", None) or getattr(prompt, "content", None)
        if not isinstance(content, str):
            logger.warning("prompt %s has no string template; got %r", ref, type(content))
            return None
        return content

    def get_active_skill(self, skill_name: str) -> str | None:
        """Read skill content from the CALIBER database.

        Skills live in the local DB (not MLflow), so this queries the
        ``caliber_skills`` table directly. Returns ``None`` when the skill
        is missing or archived.
        """
        if self._session_factory is None:
            return None

        # Lazy import to avoid circular dependency at module load time.
        from caliber.db.models import CaliberSkill  # noqa: PLC0415

        try:
            with self._session_factory() as session:
                skill = (
                    session.query(CaliberSkill)
                    .filter(
                        CaliberSkill.name == skill_name,
                        CaliberSkill.status == "active",
                    )
                    .first()
                )
                return skill.content if skill is not None else None
        except Exception:
            logger.exception("failed to load skill %s from DB", skill_name)
            return None


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeArtifactStore:
    """In-memory store for tests.

    Holds a plain ``agent_id → str`` mapping for prompts and a
    ``skill_name → str`` mapping for skills. Returns ``None`` for
    unknown keys.
    """

    def __init__(
        self,
        prompts: dict[str, str] | None = None,
        skills: dict[str, str] | None = None,
    ) -> None:
        self._prompts: dict[str, str] = dict(prompts or {})
        self._skills: dict[str, str] = dict(skills or {})

    def set(self, agent_id: str, content: str) -> None:
        self._prompts[agent_id] = content

    def set_skill(self, skill_name: str, content: str) -> None:
        self._skills[skill_name] = content

    def get_active_prompt(self, agent_id: str) -> str | None:
        return self._prompts.get(agent_id)

    def get_active_skill(self, skill_name: str) -> str | None:
        return self._skills.get(skill_name)


def build_store(provider: str, session_factory: Any | None = None) -> ArtifactStore:
    """Factory keyed off ``CaliberConfig.artifact_store_provider``."""
    provider_norm = provider.lower()
    if provider_norm == "fake":
        return FakeArtifactStore()
    if provider_norm == "mlflow":
        return MLflowArtifactStore(session_factory=session_factory)
    raise ValueError(f"unknown artifact_store_provider {provider!r}")
