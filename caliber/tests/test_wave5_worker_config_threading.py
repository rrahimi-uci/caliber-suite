"""Wave 5: the refinement worker threads its ``config`` to the eval / candidate
stage functions, so workflow calibration/refinement eval can use the real
executor (``build_executor(config)``) instead of always the fake one.
"""

from __future__ import annotations

from caliber.orchestrator import worker as worker_mod


def test_refinement_worker_stores_config() -> None:
    sentinel = object()
    w = worker_mod.RefinementWorker(
        session_factory=lambda: None,  # type: ignore[arg-type]
        llm_provider=object(),  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
        eval_provider=object(),  # type: ignore[arg-type]
        config=sentinel,  # type: ignore[arg-type]
    )
    assert w._config is sentinel


def test_dispatch_eval_and_candidate_thread_config(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_eval(
        session, job_id, eval_provider, *, artifact_store=None, actor="system", config=None
    ):
        captured["eval_config"] = config

    def _fake_run_candidate(session, job_id, llm, artifact_store, *, actor="system", config=None):
        captured["candidate_config"] = config

    monkeypatch.setattr(worker_mod, "run_eval", _fake_run_eval)
    monkeypatch.setattr(worker_mod, "run_candidate", _fake_run_candidate)

    sentinel = object()
    w = worker_mod.RefinementWorker(
        session_factory=lambda: None,  # type: ignore[arg-type]
        llm_provider=object(),  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
        eval_provider=object(),  # type: ignore[arg-type]
        config=sentinel,  # type: ignore[arg-type]
    )

    worker_mod._dispatch_eval(None, "job-1", w)  # type: ignore[arg-type]
    worker_mod._dispatch_candidate(None, "job-1", w)  # type: ignore[arg-type]

    assert captured["eval_config"] is sentinel
    assert captured["candidate_config"] is sentinel
