"""Days 4-5 of ``docs/two-week-alpha-plan.md``: Apply -> release -> the
three outcomes (``applied`` / ``failed`` / ``reconcile_required``) and
rollback.

This is the CI-enforced proof behind the plan's own Day 4-5 acceptance:
"Apply against the seed target succeeds once; a forced-failure test shows
``reconcile_required``, not a false success; rollback restores prior
state." Builds on the Day 1 seed fixture and the Day 2-3 pipeline
(``tests/fixtures/day1_seed_fixture.py`` / ``tests/fixtures/day2_day3_pipeline.py``)
to get a real, DSPy-generated ``candidate_ready`` job, then drives it
through the real ``POST /jobs/{id}/apply`` and
``POST /prompts/{name}/rollback`` HTTP endpoints against
``caliber.promoter.MLflowPromoter`` -- backed by the offline, deterministic
:class:`~tests.fixtures.day4_day5_pipeline.FakePromptRegistry` -- so the
genuine ``caliber.release_operations`` intent-first state machine is what's
under test, not a promoter that bypasses it.

See ``tests/fixtures/day4_day5_pipeline.py``'s module docstring for why a
stateful fake registry (rather than this repo's existing static
``load_refs``-dict stub convention) is what this slice specifically needs,
and ``docs/reports/two-week-alpha-day4-5-decision.md`` for the full
rationale.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import CaliberRefinementJob, CaliberReleaseOperation
from caliber.server import create_app
from tests.fixtures.day1_seed_fixture import load_prompt_template
from tests.fixtures.day2_day3_pipeline import run_diagnose_candidate_evaluate
from tests.fixtures.day4_day5_pipeline import FakePromptRegistry, mlflow_promoter_app_config

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@pytest.fixture
def mlflow_client(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """Like conftest's ``client`` fixture, but with the real
    ``MLflowPromoter`` wired up instead of the test-default ``FakePromoter``
    (see ``day4_day5_pipeline.mlflow_promoter_app_config``'s docstring for
    why Day 4-5 specifically needs this) -- no background worker/poller
    needed since ``run_diagnose_candidate_evaluate`` already drives the job
    to ``candidate_ready`` directly.
    """
    config = mlflow_promoter_app_config(app_config)
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as test_client:
        yield test_client


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakePromptRegistry:
    """A fresh, installed :class:`FakePromptRegistry` for one test."""
    fake = FakePromptRegistry()
    fake.install(monkeypatch)
    return fake


def _seed_candidate_ready_job(session_factory: sessionmaker[Session], *, agent_id: str) -> str:
    """Run the Day 2-3 pipeline to a real ``candidate_ready`` job.

    Fails loudly (rather than letting a later assertion fail confusingly) if
    the fixture pipeline doesn't reach ``candidate_ready`` -- Day 4-5's own
    Apply work has nothing to promote otherwise.
    """
    with session_factory() as session:
        result = run_diagnose_candidate_evaluate(session, agent_id=agent_id)
    assert result.status == "candidate_ready", (
        f"Day 2-3 fixture pipeline expected to reach candidate_ready for "
        f"apply, got {result.status!r}"
    )
    return result.job_id


class TestApplySucceeds:
    """Apply against the seed target succeeds once (plan acceptance, part 1)."""

    def test_apply_promotes_the_real_candidate_and_release_reaches_applied(
        self,
        mlflow_client: TestClient,
        session_factory: sessionmaker[Session],
        registry: FakePromptRegistry,
    ) -> None:
        agent_id = "day4-apply-success"
        # A real, promotable target must exist *before* Apply -- see
        # day4_day5_pipeline's module docstring for why a cold-start (v1)
        # promotion wouldn't let a later test demonstrate rollback.
        registry.seed_initial_version(name=agent_id, template=load_prompt_template())
        assert registry.current_version(agent_id) == 1

        job_id = _seed_candidate_ready_job(session_factory, agent_id=agent_id)

        resp = mlflow_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "applied"
        assert data["promotion"]["artifact_ref"] == f"prompts:/{agent_id}/2"
        assert data["promotion"]["details"]["version"] == 2
        assert data["promotion"]["details"]["version_before"] == 1

        # The live target genuinely moved -- not just the job row saying so.
        assert registry.current_version(agent_id) == 2
        assert registry.set_alias_calls == [{"name": agent_id, "alias": "prod", "version": 2}]

        job_resp = mlflow_client.get(f"{PREFIX}/jobs/{job_id}")
        assert job_resp.status_code == 200, job_resp.text
        assert job_resp.json()["data"]["status"] == "applied"

        with session_factory() as session:
            operation = (
                session.query(CaliberReleaseOperation)
                .filter(CaliberReleaseOperation.resource_name == agent_id)
                .one()
            )
            assert operation.status == "applied"
            assert operation.version_before == 1
            assert operation.version_after == 2

    def test_applying_the_same_job_twice_is_rejected_not_re_promoted(
        self,
        mlflow_client: TestClient,
        session_factory: sessionmaker[Session],
        registry: FakePromptRegistry,
    ) -> None:
        """Regression guard alongside the success path: once a job is
        ``applied`` it is terminal -- a second Apply must not silently
        promote a second version."""
        agent_id = "day4-apply-twice"
        registry.seed_initial_version(name=agent_id, template=load_prompt_template())
        job_id = _seed_candidate_ready_job(session_factory, agent_id=agent_id)

        first = mlflow_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert first.status_code == 200, first.text

        second = mlflow_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert second.status_code == 409, second.text
        # Still exactly one promoted version -- the rejected retry did not
        # touch the registry at all.
        assert registry.current_version(agent_id) == 2
        assert len(registry.register_calls) == 1


class TestApplyForcedFailureReconcileRequired:
    """A forced-failure Apply shows ``reconcile_required``, never a false
    success (plan acceptance, part 2)."""

    def test_provider_failure_after_registration_is_reconcile_required_not_applied(
        self,
        mlflow_client: TestClient,
        session_factory: sessionmaker[Session],
        registry: FakePromptRegistry,
    ) -> None:
        agent_id = "day4-apply-forced-failure"
        registry.seed_initial_version(name=agent_id, template=load_prompt_template())
        job_id = _seed_candidate_ready_job(session_factory, agent_id=agent_id)

        # Force the provider call to fail *after* the new version is already
        # registered but before the alias rotation lands -- the exact
        # indeterminate window release_operations.execute_prompt_alias_release
        # exists to make observable, not swallow as a false "applied".
        registry.raise_on_next_set_alias = RuntimeError("provider timeout after request")

        resp = mlflow_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert resp.status_code == 502, resp.text
        assert "needs reconciliation" in resp.text
        assert "provider timeout after request" in resp.text

        # Never a false success: the live alias target did not move, even
        # though a new version was registered.
        assert registry.current_version(agent_id) == 1
        assert len(registry.register_calls) == 1

        with session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            assert job is not None
            # Not "applied" and not silently rolled back to "candidate_ready"
            # either -- an honest, non-terminal state pending reconciliation.
            assert job.status == "applying"

            operation = (
                session.query(CaliberReleaseOperation)
                .filter(CaliberReleaseOperation.resource_name == agent_id)
                .one()
            )
            assert operation.status == "reconcile_required"
            assert operation.version_before == 1
            assert operation.version_after == 2
            assert "provider timeout after request" in (operation.last_error or "")

        # Closing the loop: the periodic reconciler (exercised here via its
        # HTTP trigger) observes the alias never moved, so the operation
        # settles "failed" (not "applied") and the job is handed back to
        # candidate_ready for a clean retry -- never stuck, never silently
        # promoted.
        reconcile_resp = mlflow_client.post(f"{PREFIX}/releases/operations/reconcile")
        assert reconcile_resp.status_code == 200, reconcile_resp.text
        reconciled = reconcile_resp.json()["data"]
        assert len(reconciled) == 1
        assert reconciled[0]["status"] == "failed"
        assert reconciled[0]["resource_name"] == agent_id

        job_resp = mlflow_client.get(f"{PREFIX}/jobs/{job_id}")
        assert job_resp.json()["data"]["status"] == "candidate_ready"
        # The registry itself is untouched throughout -- the whole point of
        # "reconcile_required, not a false success".
        assert registry.current_version(agent_id) == 1


class TestRollbackRestoresPriorState:
    """Rollback restores prior state (plan acceptance, part 3)."""

    def test_rollback_restores_the_exact_prior_prompt_version(
        self,
        mlflow_client: TestClient,
        session_factory: sessionmaker[Session],
        registry: FakePromptRegistry,
    ) -> None:
        agent_id = "day4-rollback"
        original_template = load_prompt_template()
        registry.seed_initial_version(name=agent_id, template=original_template)
        job_id = _seed_candidate_ready_job(session_factory, agent_id=agent_id)

        apply_resp = mlflow_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert apply_resp.status_code == 200, apply_resp.text
        assert registry.current_version(agent_id) == 2
        promoted_template = registry.load_prompt(f"prompts:/{agent_id}@prod")
        assert promoted_template is not None and promoted_template.version == 2
        # The DSPy candidate genuinely differs from the original template --
        # otherwise a rollback bug that silently no-ops could hide behind
        # "the content looked the same anyway".
        assert promoted_template.template != original_template

        rollback_resp = mlflow_client.post(f"{PREFIX}/prompts/{agent_id}/rollback", json={})
        assert rollback_resp.status_code == 200, rollback_resp.text
        data = rollback_resp.json()["data"]
        assert data["version"] == 1
        assert data["rolled_back_from"] == 2
        assert data["release_status"] == "applied"

        # Prior state is restored, verified by reading the live target back
        # independently of the rollback response itself.
        assert registry.current_version(agent_id) == 1
        restored = registry.load_prompt(f"prompts:/{agent_id}@prod")
        assert restored is not None
        assert restored.version == 1
        assert restored.template == original_template

        with session_factory() as session:
            operation = (
                session.query(CaliberReleaseOperation)
                .filter(
                    CaliberReleaseOperation.resource_name == agent_id,
                    CaliberReleaseOperation.operation_type == "rollback",
                )
                .one()
            )
            assert operation.status == "applied"
            assert operation.version_before == 2
            assert operation.version_after == 1

    def test_rollback_with_no_prior_promotion_is_refused_not_guessed(
        self,
        mlflow_client: TestClient,
        registry: FakePromptRegistry,
    ) -> None:
        """A prompt that was never promoted through this journey has no
        recorded prior version -- rollback must refuse (409), never guess."""
        agent_id = "day4-rollback-cold-start"
        registry.seed_initial_version(name=agent_id, template=load_prompt_template())

        resp = mlflow_client.post(f"{PREFIX}/prompts/{agent_id}/rollback", json={})
        assert resp.status_code == 409, resp.text
        # Refusing to guess leaves the live target exactly where it was.
        assert registry.current_version(agent_id) == 1


class TestFakePromptRegistry:
    """Direct unit coverage for the fake registry itself.

    The three journey tests above exercise it only through its "happy path"
    call shape (real ``caliber.promoter`` / ``caliber.routes.prompts`` call
    sites). A bug in the fake's own edge-case handling -- an unsupported ref,
    a missing alias, a version-less ``set_prompt_alias`` call -- would
    otherwise silently pass by never being exercised, undermining confidence
    in the journey tests it backs.
    """

    def test_current_version_is_none_before_seeding(self) -> None:
        registry = FakePromptRegistry()
        assert registry.current_version("never-seeded") is None

    def test_load_prompt_allow_missing_returns_none_for_an_unset_alias(self) -> None:
        registry = FakePromptRegistry()
        assert registry.load_prompt("prompts:/no-such-prompt@prod", allow_missing=True) is None

    def test_load_prompt_without_allow_missing_raises_for_an_unset_alias(self) -> None:
        registry = FakePromptRegistry()
        with pytest.raises(LookupError, match="no version on alias"):
            registry.load_prompt("prompts:/no-such-prompt@prod")

    def test_load_prompt_rejects_an_unsupported_ref_shape(self) -> None:
        registry = FakePromptRegistry()
        with pytest.raises(ValueError, match="unsupported ref"):
            registry.load_prompt("prompts:/missing-alias-segment")

    def test_set_prompt_alias_requires_a_concrete_version(self) -> None:
        registry = FakePromptRegistry()
        with pytest.raises(ValueError, match="requires a concrete version"):
            registry.set_prompt_alias("some-prompt", "prod", None)

    def test_raise_on_next_set_alias_fires_exactly_once(self) -> None:
        registry = FakePromptRegistry()
        registry.seed_initial_version(name="p", template="t")
        registry.raise_on_next_set_alias = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            registry.set_prompt_alias("p", "prod", 2)
        assert registry.raise_on_next_set_alias is None

        # The lever is consumed -- the next call succeeds normally.
        registry.set_prompt_alias("p", "prod", 2)
        assert registry.current_version("p") == 2
        assert registry.set_alias_calls == [{"name": "p", "alias": "prod", "version": 2}]

    def test_register_prompt_tracks_calls_and_returns_a_uri(self) -> None:
        registry = FakePromptRegistry()
        version = registry.register_prompt(name="p", template="hello", tags={"k": "v"})
        assert version.version == 1
        assert version.uri == "prompts:/p/1"
        assert registry.register_calls == [{"name": "p", "template": "hello", "tags": {"k": "v"}}]
