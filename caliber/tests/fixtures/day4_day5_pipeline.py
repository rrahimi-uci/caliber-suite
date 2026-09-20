"""Days 4-5 of the two-week alpha plan: Apply -> release -> the three
outcomes (``applied`` / ``failed`` / ``reconcile_required``) and rollback.

See ``docs/two-week-alpha-plan.md`` (Week 1, Days 4-5) and
``docs/reports/two-week-alpha-day4-5-decision.md`` for the acceptance this
module satisfies and the design rationale below. Days 1 and 2-3 delivered a
committed seed fixture and a real, DSPy-driven pipeline that lands a
``CaliberRefinementJob`` at ``candidate_ready`` with genuine candidate
content (``tests/fixtures/day1_seed_fixture.py`` and
``tests/fixtures/day2_day3_pipeline.py``). This module takes that job the
rest of the way: ``POST /jobs/{id}/apply`` -> the intent-first release state
machine in ``caliber/release_operations.py`` -> a live prompt-alias target,
and ``POST /prompts/{name}/rollback`` back again.

**The tension this module resolves:** the plan's Day 4-5 acceptance demands
"Apply against the seed target succeeds once" against a *real* promotable
target -- not a `FakePromoter` stand-in that never touches
``caliber.release_operations`` at all -- while every other slice of this plan
stays offline and deterministic (no live MLflow Prompt Registry, no
network). ``caliber.promoter.MLflowPromoter`` is the one promoter that
actually drives the ``prepare_prompt_alias_release`` / ``execute_prompt_alias_release``
state machine this plan needs to exercise (confirmed by reading
``apply.py``/``promoter.py`` -- the plain prompt/skill bundle path this
plan's DSPy candidate takes always resolves to ``MLflowPromoter`` once
``promoter_provider="mlflow"`` is configured). Resolution, extending this
repo's own existing precedent (``tests/test_routes_prompts.py::_install_mlflow``,
``tests/test_promoter.py``'s per-call ``sys.modules["mlflow"]`` stubs): stub
the ``mlflow``/``mlflow.genai`` module surface those call sites actually use
(``register_prompt`` / ``set_prompt_alias`` / ``load_prompt``), but back it
with :class:`FakePromptRegistry`, a small *stateful* in-memory registry
(monotonic versions per prompt name, one concrete version per alias) instead
of a static, hand-updated ``load_refs`` dict. A stateful registry is what
this slice specifically needs and the existing per-test static stubs don't
provide: proof that Apply's own registered version and alias rotation is
what a *subsequent, independent* rollback call reads back -- a genuine
round trip through the real state machine, not two independently-scripted
expectations. No live MLflow server, no network call, fully deterministic.

The one committed intake-classifier prompt template
(``tests/fixtures/day1_seed_fixture.py::load_prompt_template``) is
pre-registered as the fixture's "v1" -- the target's cold-start state before
the journey below runs -- via :meth:`FakePromptRegistry.seed_initial_version`,
so Apply's own promotion has a genuine prior version to roll back to (a
brand-new, never-promoted prompt has no ``version_before`` and therefore
nothing to demonstrate rollback with; see ``apply.py::_build_checkpoint``'s
own "v1 promotions... return ``None``" comment).

Callers (the Day 4-5 pytest module) get:

* :class:`FakePromptRegistry` -- the stateful fake MLflow Prompt Registry,
  installed into ``sys.modules`` via :meth:`FakePromptRegistry.install`.
* :func:`mlflow_promoter_app_config` -- an ``app_config`` override that
  points the app's ``Promoter`` at :class:`caliber.promoter.MLflowPromoter`
  instead of the test-default ``FakePromoter``.
"""

from __future__ import annotations

import re
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from caliber.config import CaliberConfig

DEFAULT_ALIAS = "prod"

# Matches the ``prompts:/{name}@{alias}`` refs every call site in
# ``caliber.promoter`` and ``caliber.routes.prompts`` builds (confirmed by
# reading both modules) -- there is no version-pinned ``#N`` suffix on any of
# these refs, only the alias form.
_PROMPT_REF_RE = re.compile(r"^prompts:/(?P<name>[^@/]+)@(?P<alias>.+)$")


@dataclass(frozen=True)
class _PromptVersionRecord:
    """One immutable registered version, mirroring a real MLflow prompt version."""

    version: int
    template: str
    tags: dict[str, str]


@dataclass
class FakePromptRegistry:
    """Stateful, offline stand-in for the MLflow Prompt Registry surface
    :class:`caliber.promoter.MLflowPromoter` and ``caliber.routes.prompts``
    depend on.

    Versions are monotonically increasing per prompt name (matching real
    MLflow semantics that ``caliber.promoter`` explicitly relies on -- see
    ``MLflowPromoter``'s own "we rely on MLflow's monotonically-increasing
    prompt version numbers" comment); each ``(name, alias)`` pair points at
    exactly one concrete version. ``load_prompt`` resolves the alias form
    every real call site in this codebase uses.

    :attr:`raise_on_next_set_alias` is the forced-failure lever: setting it
    to an exception instance makes the *next* :meth:`set_prompt_alias` call
    raise instead of mutating the alias -- simulating a provider failure
    that begins after the new version is already registered, exactly the
    "indeterminate outcome" ``release_operations.execute_prompt_alias_release``
    exists to make observable as ``reconcile_required`` rather than a false
    ``applied``.
    """

    history: dict[str, list[_PromptVersionRecord]] = field(default_factory=dict)
    aliases: dict[tuple[str, str], int] = field(default_factory=dict)
    register_calls: list[dict[str, Any]] = field(default_factory=list)
    set_alias_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_next_set_alias: BaseException | None = None

    def seed_initial_version(self, *, name: str, template: str, alias: str = DEFAULT_ALIAS) -> int:
        """Pre-register ``template`` as version 1 and point ``alias`` at it.

        This is the "real, promotable MLflow-registered prompt target"
        Day 4-5 needs to exist *before* the journey runs -- without it,
        Apply's own promotion would be a v1 cold start with no prior
        version, and rollback would have nothing to demonstrate.
        """
        version = self._register(name=name, template=template, tags={})
        self.aliases[(name, alias)] = version
        return version

    def current_version(self, name: str, alias: str = DEFAULT_ALIAS) -> int | None:
        """The version currently live on ``(name, alias)``, or ``None``."""
        return self.aliases.get((name, alias))

    def _register(
        self,
        *,
        name: str,
        template: str,
        commit_message: str = "",
        tags: dict[str, str] | None = None,
    ) -> int:
        versions = self.history.setdefault(name, [])
        version = len(versions) + 1
        versions.append(
            _PromptVersionRecord(version=version, template=template, tags=dict(tags or {}))
        )
        return version

    # ------------------------------------------------------------------
    # ``mlflow.genai`` surface -- signatures/kwargs match the real calls in
    # ``caliber.promoter.MLflowPromoter`` and ``caliber.routes.prompts``.
    # ------------------------------------------------------------------

    def register_prompt(
        self,
        *,
        name: str,
        template: str,
        commit_message: str = "",
        tags: dict[str, str] | None = None,
    ) -> types.SimpleNamespace:
        version = self._register(
            name=name, template=template, commit_message=commit_message, tags=tags
        )
        self.register_calls.append({"name": name, "template": template, "tags": dict(tags or {})})
        return types.SimpleNamespace(version=version, uri=f"prompts:/{name}/{version}")

    def set_prompt_alias(
        self, name: str, alias: str = DEFAULT_ALIAS, version: int | str | None = None
    ) -> None:
        """Rotate ``(name, alias)`` to ``version``.

        Accepts both the keyword-call convention ``caliber.promoter`` uses
        and the positional convention
        ``caliber.routes.prompts.set_prompt_alias_version`` uses -- both are
        real call sites this fake stands in for.
        """
        if self.raise_on_next_set_alias is not None:
            exc, self.raise_on_next_set_alias = self.raise_on_next_set_alias, None
            raise exc
        version_num = int(version) if version is not None else None
        if version_num is None:
            raise ValueError("set_prompt_alias requires a concrete version")
        self.aliases[(name, alias)] = version_num
        self.set_alias_calls.append({"name": name, "alias": alias, "version": version_num})

    def load_prompt(self, ref: str, allow_missing: bool = False) -> types.SimpleNamespace | None:
        match = _PROMPT_REF_RE.match(ref)
        if not match:
            raise ValueError(f"FakePromptRegistry.load_prompt: unsupported ref {ref!r}")
        name, alias = match.group("name"), match.group("alias")
        version = self.aliases.get((name, alias))
        if version is None:
            if allow_missing:
                return None
            raise LookupError(f"prompt {name!r} has no version on alias {alias!r}")
        record = next((r for r in self.history.get(name, []) if r.version == version), None)
        if record is None:  # pragma: no cover - defensive; an alias always names a real version
            raise LookupError(f"version {version} missing from history for {name!r}")
        return types.SimpleNamespace(
            version=record.version,
            template=record.template,
            content=record.template,
            name=name,
            tags=dict(record.tags),
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch ``sys.modules`` so every lazy ``import mlflow`` in
        ``caliber.promoter`` / ``caliber.routes.prompts`` resolves to this
        registry for the duration of the test."""
        genai_module = types.ModuleType("mlflow.genai")
        genai_module.register_prompt = self.register_prompt  # type: ignore[attr-defined]
        genai_module.set_prompt_alias = self.set_prompt_alias  # type: ignore[attr-defined]
        genai_module.load_prompt = self.load_prompt  # type: ignore[attr-defined]
        mlflow_module = types.ModuleType("mlflow")
        mlflow_module.genai = genai_module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mlflow", mlflow_module)
        monkeypatch.setitem(sys.modules, "mlflow.genai", genai_module)


def mlflow_promoter_app_config(app_config: CaliberConfig) -> CaliberConfig:
    """``app_config`` with the real ``MLflowPromoter`` wired up.

    The test suite's ``app_config`` fixture defaults ``promoter_provider``
    to ``"fake"`` (see ``CaliberConfig.promoter_provider``'s own docstring)
    so the server boots without a Prompt Registry write path configured --
    exactly right for most of this suite, but not for Day 4-5, which needs
    the real ``_apply_bundle`` -> ``promote_bundle`` -> ``MLflowPromoter`` ->
    ``release_operations`` chain the plan's acceptance criteria are about.
    """
    return app_config.model_copy(update={"promoter_provider": "mlflow"})
