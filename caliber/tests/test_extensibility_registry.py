"""The optimizer registry, and what it refuses.

An optimizer writes the artifact CALIBER promotes to production. The registry is
therefore not only a lookup table -- it is the boundary that decides which code
gets that authority, so the tests that matter most here are the ones about
refusal.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

import pytest

from caliber.extensibility import entrypoints
from caliber.extensibility import registry as registry_module
from caliber.extensibility.registry import (
    BUILTIN_OPTIMIZERS,
    OptimizerRegistry,
    OptimizerSpec,
    PluginError,
    optimizer_registry,
)
from caliber.llm.provider import CandidateContext


@pytest.fixture
def clean_registry() -> Any:
    """A registry with only built-ins, restored afterwards.

    The process-wide instance is shared, so a test that installs a fake plugin
    must not leak it into the next one.
    """
    instance = optimizer_registry()
    instance.reset_for_tests()
    yield instance
    instance.reset_for_tests()


def plugin_spec(name: str = "AcmeOptimizer", **overrides: Any) -> OptimizerSpec:
    fields: dict[str, Any] = {
        "name": name,
        "summary": "third-party",
        "artifact_types": frozenset({"prompt"}),
        "source": "plugin",
        "distribution": "acme-caliber-optimizers",
    }
    fields.update(overrides)
    return OptimizerSpec(**fields)


# --- what the registry knows ----------------------------------------------


def test_the_builtins_are_registered_and_scoped_to_artifact_kinds() -> None:
    instance = OptimizerRegistry()
    assert instance.names() == [
        "DSPyBootstrapFewShot",
        "DSPyMIPRO",
        "GEPA",
        "MetaPrompt",
        "SkillMetaPrompt",
    ]
    # The scoping the old dispatch chain could not express: MetaPrompt is a
    # prompt optimizer, and routing a skill job to it silently dropped the
    # skill's allowed_tools.
    assert not instance.get("MetaPrompt").can_target("skill")
    assert instance.get("SkillMetaPrompt").can_target("skill")
    assert instance.get("GEPA").can_target("prompt")
    assert instance.get("GEPA").can_target("skill")


def test_explicit_only_optimizers_are_excluded_from_automatic_selection() -> None:
    """DSPyMIPRO is implemented and deliberately never auto-selected."""
    instance = OptimizerRegistry()
    assert "DSPyMIPRO" in instance.names(artifact_type="prompt")
    assert "DSPyMIPRO" not in instance.selectable("prompt")


def test_an_unknown_name_names_what_is_available() -> None:
    """The old error said "not implemented yet" and left the reader to grep."""
    with pytest.raises(PluginError) as caught:
        OptimizerRegistry().get("TextGrad")
    assert "TextGrad" in str(caught.value)
    assert "MetaPrompt" in str(caught.value)


def test_optional_dependencies_are_declared_rather_than_discovered_at_runtime() -> None:
    """So a UI can say "GEPA needs gepa" before a run silently falls back."""
    instance = OptimizerRegistry()
    assert instance.get("GEPA").requires == "gepa"
    assert instance.get("DSPyBootstrapFewShot").requires == "dspy"
    assert instance.get("MetaPrompt").requires is None


# --- what the registry refuses --------------------------------------------


def test_a_plugin_may_not_redefine_a_builtin(clean_registry: OptimizerRegistry) -> None:
    """The substitution attack with a plausible cover story.

    Every agent configured for ``GEPA`` keeps working, the name in the audit log
    is unchanged, and a different author's code now produces the candidates.
    Nothing in a diff would show it, so the registry refuses the name.
    """
    with pytest.raises(PluginError) as caught:
        clean_registry.register(plugin_spec("GEPA", artifact_types=frozenset({"prompt"})))
    assert "never redefine" in str(caught.value)
    assert clean_registry.get("GEPA").source == "builtin"


def test_two_plugins_may_not_claim_one_name(clean_registry: OptimizerRegistry) -> None:
    """Otherwise entry-point iteration order decides, which nobody chose."""
    clean_registry.register(plugin_spec())
    with pytest.raises(PluginError) as caught:
        clean_registry.register(plugin_spec(distribution="rival-plugin"))
    assert "claimed by both" in str(caught.value)


def test_an_optimizer_that_can_target_nothing_is_rejected() -> None:
    with pytest.raises(PluginError, match="no artifact types"):
        OptimizerRegistry().register(plugin_spec(artifact_types=frozenset()))


def test_re_registering_an_identical_builtin_is_not_an_error() -> None:
    """Test collection can import a module twice; that must not fail."""
    instance = OptimizerRegistry()
    for spec in BUILTIN_OPTIMIZERS:
        instance.register(spec)
    assert len(instance.names()) == len(BUILTIN_OPTIMIZERS)


# --- the allowlist --------------------------------------------------------


def fake_entry_point(name: str, loaded: Any, distribution: str | None) -> Any:
    class _Dist:
        pass

    dist = _Dist()
    if distribution is not None:
        dist.name = distribution  # type: ignore[attr-defined]

    class _EntryPoint:
        def __init__(self) -> None:
            self.name = name
            self.value = f"fake:{name}"
            self.dist = dist if distribution is not None else None

        def load(self) -> Any:
            if isinstance(loaded, Exception):
                raise loaded
            return loaded

    return _EntryPoint()


@pytest.fixture
def fake_entry_points(monkeypatch: pytest.MonkeyPatch) -> Any:
    installed: list[Any] = []

    def install(*entries: Any) -> None:
        installed.clear()
        installed.extend(entries)

    monkeypatch.setattr(entrypoints, "_entry_points", lambda: list(installed))
    return install


def test_an_installed_plugin_does_nothing_until_it_is_allowlisted(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery is not authorization.

    Any installed distribution -- including one pulled in transitively -- can
    advertise into the entry-point group. Loading on discovery would hand a
    dependency authority over production prompts with no review step, so an
    unlisted plugin is visible and inert.
    """
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    fake_entry_points(fake_entry_point("acme", plugin_spec(), "acme-caliber-optimizers"))

    assert "AcmeOptimizer" not in clean_registry.names()
    assert clean_registry.load_errors == {}

    # And it is *reported*, so an operator can enable it rather than wonder why
    # the wheel they installed had no effect.
    listed = entrypoints.available_optimizer_plugins()
    assert listed[0]["distribution"] == "acme-caliber-optimizers"
    assert listed[0]["allowlisted"] is False


def test_listing_available_plugins_does_not_execute_them(
    fake_entry_points: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing is what an operator reads *before* deciding to trust it."""
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    fake_entry_points(
        fake_entry_point("boom", RuntimeError("import side effect ran"), "hostile-plugin")
    )
    assert entrypoints.available_optimizer_plugins()[0]["name"] == "boom"


def test_an_allowlisted_plugin_registers(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, "acme-caliber-optimizers")
    fake_entry_points(fake_entry_point("acme", plugin_spec(), "acme-caliber-optimizers"))

    spec = clean_registry.get("AcmeOptimizer")
    assert spec.source == "plugin"
    assert spec.distribution == "acme-caliber-optimizers"
    # Experimental regardless of what the plugin claimed about itself.
    assert spec.experimental


def test_the_allowlist_matches_normalised_distribution_names(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry that looks right must not silently match nothing.

    Packaging treats ``Acme_Caliber_Optimizers`` and ``acme-caliber-optimizers``
    as one name; an allowlist that did not would fail closed in the most
    confusing possible way.
    """
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, " Acme_Caliber_Optimizers , other ")
    fake_entry_points(fake_entry_point("acme", plugin_spec(), "acme-caliber-optimizers"))
    assert "AcmeOptimizer" in clean_registry.names()


def test_a_plugin_cannot_claim_to_be_builtin(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provenance is taken from the metadata, never from the plugin's claim.

    A plugin that could describe itself as built-in would hide from exactly the
    operator review this whole mechanism exists to enable.
    """
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, "acme-caliber-optimizers")
    fake_entry_points(
        fake_entry_point(
            "acme",
            plugin_spec(source="builtin", distribution="caliber"),
            "acme-caliber-optimizers",
        )
    )
    spec = clean_registry.get("AcmeOptimizer")
    assert spec.source == "plugin"
    assert spec.distribution == "acme-caliber-optimizers"


def test_a_factory_entry_point_is_called(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a plugin can probe for its optional dependency before declaring."""
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, "acme-caliber-optimizers")
    fake_entry_points(
        fake_entry_point("acme", lambda: plugin_spec("FactoryMade"), "acme-caliber-optimizers")
    )
    assert "FactoryMade" in clean_registry.names()


# --- one broken plugin is that plugin's problem ---------------------------


@pytest.mark.parametrize(
    "loaded",
    [
        RuntimeError("plugin blew up on import"),
        "not a spec at all",
        None,
    ],
    ids=["raises", "wrong-type", "returns-none"],
)
def test_a_broken_plugin_is_recorded_and_skipped(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
    loaded: Any,
) -> None:
    """CALIBER must still start, and the failure must still be visible.

    Raising would make one bad wheel a total outage. Swallowing would leave an
    operator who explicitly allowlisted something with no sign it never loaded.
    """
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, "broken-plugin")
    fake_entry_points(fake_entry_point("broken", loaded, "broken-plugin"))

    assert clean_registry.names() == OptimizerRegistry().names()
    assert "broken-plugin" in clean_registry.load_errors


def test_one_broken_plugin_does_not_block_a_working_one(
    clean_registry: OptimizerRegistry,
    fake_entry_points: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, "broken-plugin,acme-caliber-optimizers")
    fake_entry_points(
        fake_entry_point("broken", RuntimeError("nope"), "broken-plugin"),
        fake_entry_point("acme", plugin_spec(), "acme-caliber-optimizers"),
    )
    assert "AcmeOptimizer" in clean_registry.names()
    assert "broken-plugin" in clean_registry.load_errors


def test_plugins_load_once_rather_than_on_every_lookup(
    clean_registry: OptimizerRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every lookup re-importing third-party code would be both slow and unsafe."""
    calls = 0

    def counting() -> Any:
        nonlocal calls
        calls += 1
        return iter(())

    monkeypatch.setattr(entrypoints, "discover_optimizer_plugins", counting)
    clean_registry.names()
    clean_registry.names()
    clean_registry.get("MetaPrompt")
    assert calls == 1


def test_discovery_survives_a_broken_metadata_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """No plugins is a working deployment; a metadata error must not change that."""

    def explode(**_: Any) -> Any:
        raise ValueError("corrupt dist-info")

    monkeypatch.setattr(metadata, "entry_points", explode)
    assert entrypoints._entry_points() == []


def test_the_process_wide_registry_is_a_single_instance() -> None:
    """Two registries could disagree about what is installed."""
    assert optimizer_registry() is registry_module.optimizer_registry()


# --- the provider dispatches through the registry --------------------------


def test_the_provider_refuses_a_skill_job_on_a_prompt_only_optimizer() -> None:
    """The check the old dispatch chain could not make.

    ``MetaPrompt`` accepted a skill job and ran the prompt formatter on it,
    which ignores ``allowed_tools`` entirely -- so a skill could come back with
    its tool restrictions dropped and nothing in the candidate would say so.
    The registry knows MetaPrompt targets prompts, so this is a configuration
    error and now reads as one.
    """
    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
    from caliber.llm.provider import Diagnosis, LLMProviderError

    provider = OpenAIAgentsLLMProvider(api_key="sk-test", diagnosis_model="gpt-4o-mini")
    context = CandidateContext(
        agent_id="AGT-1",
        job_id="RFN-1",
        artifact_type="skill",
        optimizer_type="MetaPrompt",
        diagnosis=Diagnosis(
            root_cause="tool call missing",
            affected_components=["skill"],
            confidence=0.9,
            alternatives=[],
        ),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider.generate_candidate(context)
    message = str(caught.value)
    assert "cannot target artifact_type 'skill'" in message
    # And it names the optimizers that *can*, so the fix is in the error.
    assert "SkillMetaPrompt" in message


def test_a_registered_plugin_without_an_engine_is_refused_rather_than_faked(
    clean_registry: OptimizerRegistry,
) -> None:
    """Running MetaPrompt under a plugin's name would misattribute the result.

    The candidate is stored with its optimizer name and promoted from there, so
    a silent substitution would put a claim in the audit trail about code that
    never ran.
    """
    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
    from caliber.llm.provider import Diagnosis, LLMProviderError

    clean_registry.register(plugin_spec())
    provider = OpenAIAgentsLLMProvider(api_key="sk-test", diagnosis_model="gpt-4o-mini")
    context = CandidateContext(
        agent_id="AGT-1",
        job_id="RFN-1",
        artifact_type="prompt",
        optimizer_type="AcmeOptimizer",
        diagnosis=Diagnosis(
            root_cause="x", affected_components=[], confidence=0.9, alternatives=[]
        ),
    )

    with pytest.raises(LLMProviderError, match="no engine for it"):
        provider.generate_candidate(context)


# --- the selection rule and the routes agree with the registry -------------


def test_every_name_the_selection_rule_can_return_is_registered() -> None:
    """The rule and the registry are separate modules and must not drift.

    ``select_optimizer`` returns bare strings. A name it can return that the
    registry does not carry would fail at candidate generation -- after the job
    was created, queued, and picked up -- rather than at selection.
    """
    import re
    from pathlib import Path

    source = Path(registry_module.__file__).parents[1] / "orchestrator" / "optimizer_select.py"
    returned = set(re.findall(r'return "([A-Za-z]+)"', source.read_text(encoding="utf-8")))
    assert returned, "the selection rule returns no literal names; this test needs updating"

    known = set(OptimizerRegistry().names())
    assert returned <= known, f"select_optimizer can return unregistered names: {returned - known}"


def test_the_manual_prompt_form_offers_only_registered_optimizers() -> None:
    """The route's tuple is narrower than the registry on purpose, not by drift.

    Narrower because the DSPy engines sit behind a runtime advisory flag. But
    every name it does offer must exist and must be able to target a prompt, so
    a rename in the registry cannot leave the form advertising a ghost.
    """
    from caliber.routes.prompts import _SUPPORTED_PROMPT_OPTIMIZERS

    prompt_capable = set(OptimizerRegistry().names(artifact_type="prompt"))
    assert set(_SUPPORTED_PROMPT_OPTIMIZERS) <= prompt_capable, (
        f"the manual form offers optimizers the registry does not: "
        f"{set(_SUPPORTED_PROMPT_OPTIMIZERS) - prompt_capable}"
    )
