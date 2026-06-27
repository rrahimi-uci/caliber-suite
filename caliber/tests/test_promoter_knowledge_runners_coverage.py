"""Targeted branch coverage for ``build_knowledge_runtime_runners``.

These tests drive the two runtime closures returned by
:func:`caliber.workflows.promoter.build_knowledge_runtime_runners`
(``_run_knowledge_query`` and ``_run_knowledge_build``) without a real
``KnowledgeBaseService``: the service class is monkeypatched with a fake that
records calls and returns lightweight stub objects. This exercises the error
paths (``PublishError``), retrieval-mode normalization (incl. the empty-list
default-resolve path), and the build runner's activation / wait-for-completion
branches.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.auth import SCOPE_VIEWER, CaliberIdentity
from caliber.config import CaliberConfig
from caliber.workflows import promoter
from caliber.workflows.promoter import PublishError, build_knowledge_runtime_runners


def _identity() -> CaliberIdentity:
    return CaliberIdentity(
        user_id="@runner",
        scopes=frozenset({SCOPE_VIEWER}),
        active_project_id=None,
    )


class _Dumpable:
    """Minimal stand-in for a pydantic model exposing ``model_dump``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return dict(self._payload)


def _graph_config(
    *,
    default_retrieval_mode: str | None = "graph_hybrid",
    output_target: str = "object_store",
) -> SimpleNamespace:
    return SimpleNamespace(
        default_retrieval_mode=default_retrieval_mode,
        output_target=output_target,
    )


def _version_stub(
    *,
    version_id: str = "kbv-1",
    status: str = "completed",
    summary: Any = None,
    graph_config: SimpleNamespace | None = None,
    error_summary: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        knowledge_base_version_id=version_id,
        status=status,
        version_number=3,
        chunking_strategy="recursive",
        embedding_model="text-embedding-3-small",
        error_summary=error_summary,
        summary=summary,
        graph_config=graph_config or _graph_config(),
        model_dump=lambda *, mode="python": {"id": version_id, "status": status},
    )


def _kb_stub(*, active_version_id: str | None = "kbv-1") -> SimpleNamespace:
    return SimpleNamespace(
        active_version_id=active_version_id,
        model_dump=lambda *, mode="python": {"active_version_id": active_version_id},
    )


def _run_stub(run_id: str = "kbr-1") -> SimpleNamespace:
    return SimpleNamespace(
        knowledge_base_run_id=run_id,
        model_dump=lambda *, mode="python": {"run_id": run_id},
    )


class _FakeKnowledgeService:
    """Configurable fake replacing ``promoter.KnowledgeBaseService``.

    The closures construct this with ``config=`` / ``session_factory=`` only,
    so the real ``__init__`` (which builds an AGE store) is never touched.
    Per-construction overrides come from the ``_script`` class attribute and
    each instance appends itself to ``_instances`` so a test can inspect the
    request objects that reached the service.
    """

    _script: dict[str, Any] = {}
    _instances: list[_FakeKnowledgeService] = []

    def __init__(self, *, config: Any = None, session_factory: Any = None) -> None:
        self.config = config
        self.session_factory = session_factory
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        spec = type(self)._script
        self._kb = spec.get("kb", _kb_stub())
        self._versions = list(spec.get("versions", [_version_stub()]))
        self._query_result = spec.get("query_result", _Dumpable({"answer": "ok"}))
        self._age_enabled = spec.get("age_enabled", False)
        self._runs = list(spec.get("runs", [_run_stub()]))
        self._create_kb = spec.get("create_kb", _kb_stub())
        self._create_version = spec.get("create_version", _version_stub())
        self._create_run = spec.get("create_run", _run_stub())
        self._activated_kb = spec.get("activated_kb", _kb_stub(active_version_id="kbv-1"))
        self._version_index = 0
        type(self)._instances.append(self)

    def get_knowledge_base(self, knowledge_base_id: str, *, identity: Any) -> Any:
        self.calls.append(("get_knowledge_base", (knowledge_base_id,)))
        return self._kb

    def get_version(self, version_id: str, *, identity: Any) -> Any:
        self.calls.append(("get_version", (version_id,)))
        if self._version_index < len(self._versions):
            version = self._versions[self._version_index]
            self._version_index += 1
            return version
        return self._versions[-1]

    def options(self) -> Any:
        return SimpleNamespace(age_enabled=self._age_enabled)

    def query(self, request: Any, *, identity: Any) -> Any:
        self.calls.append(("query", (request,)))
        return self._query_result

    def create_version(
        self,
        knowledge_base_id: str,
        request: Any,
        *,
        identity: Any,
        actor: str,
    ) -> Any:
        self.calls.append(("create_version", (knowledge_base_id, actor)))
        return SimpleNamespace(
            knowledge_base=self._create_kb,
            version=self._create_version,
            run=self._create_run,
        )

    def activate_version(
        self,
        knowledge_base_id: str,
        version_id: str,
        *,
        identity: Any,
        actor: str,
    ) -> Any:
        self.calls.append(("activate_version", (knowledge_base_id, version_id, actor)))
        return self._activated_kb

    def list_runs(self, knowledge_base_id: str, *, identity: Any) -> Any:
        self.calls.append(("list_runs", (knowledge_base_id,)))
        return self._runs

    @classmethod
    def last(cls) -> _FakeKnowledgeService:
        return cls._instances[-1]

    @classmethod
    def last_request(cls, method: str) -> Any:
        last = cls.last()
        return [c for c in last.calls if c[0] == method][-1][1][0]


@pytest.fixture
def install_fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install ``_FakeKnowledgeService`` as ``promoter.KnowledgeBaseService``.

    Returns a setter that seeds the per-construction script and resets the
    instance registry. All mutations go through ``monkeypatch`` so the class
    attributes are restored on teardown (no cross-test leakage).
    """

    monkeypatch.setattr(_FakeKnowledgeService, "_instances", [])

    def _install(**script: Any) -> type[_FakeKnowledgeService]:
        monkeypatch.setattr(_FakeKnowledgeService, "_script", dict(script))
        monkeypatch.setattr(promoter, "KnowledgeBaseService", _FakeKnowledgeService)
        return _FakeKnowledgeService

    return _install


def _runners(
    db_session: Session,
    session_factory: sessionmaker[Session],
    *,
    config: CaliberConfig | None = None,
    actor: str | None = None,
) -> Any:
    return build_knowledge_runtime_runners(
        db_session,
        identity=_identity(),
        config=config,
        session_factory=session_factory,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# _run_knowledge_query
# ---------------------------------------------------------------------------


def test_query_resolves_active_version_from_knowledge_base_id(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service()
    query, _build = _runners(db_session, session_factory)

    result = query({"knowledge_base_id": "kb-1", "question": "what?"})

    assert result == {"answer": "ok"}
    assert fake.last_request("query").version_ids == ["kbv-1"]


def test_query_raises_when_knowledge_base_has_no_active_version(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service(kb=_kb_stub(active_version_id=None))
    query, _build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="no active version"):
        query({"knowledge_base_id": "kb-1", "question": "q"})


def test_query_raises_when_no_version_id_or_knowledge_base_id(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service()
    query, _build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="at least one version_id or knowledge_base_id"):
        query({"question": "q"})


def test_query_normalizes_string_and_dedupes_retrieval_modes(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service()
    query, _build = _runners(db_session, session_factory)

    # ``"  hybrid  "`` (str input) is wrapped into a list and stripped.
    query({"version_ids": ["kbv-9"], "question": "q", "retrieval_modes": "  hybrid  "})

    sent = fake.last_request("query")
    assert sent.retrieval_modes == ["hybrid"]
    assert sent.version_ids == ["kbv-9"]


def test_query_empty_retrieval_modes_resolves_default_graph_hybrid(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service(
        versions=[_version_stub(graph_config=_graph_config(default_retrieval_mode="hybrid"))]
    )
    query, _build = _runners(db_session, session_factory)

    query({"version_ids": ["kbv-1"], "question": "q", "retrieval_modes": []})

    assert fake.last_request("query").retrieval_modes == ["hybrid"]


def test_query_empty_modes_age_graph_downgrades_when_age_not_ready(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    # default mode is age_graph but AGE is disabled -> downgrade to graph_hybrid.
    fake = install_fake_service(
        age_enabled=False,
        versions=[
            _version_stub(
                graph_config=_graph_config(
                    default_retrieval_mode="age_graph",
                    output_target="object_store_and_age",
                ),
                summary={"age_sync_status": "synced"},
            )
        ],
    )
    query, _build = _runners(db_session, session_factory)

    query({"version_ids": ["kbv-1"], "question": "q", "retrieval_modes": []})

    assert fake.last_request("query").retrieval_modes == ["graph_hybrid"]


def test_query_empty_modes_age_graph_kept_when_age_ready(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service(
        age_enabled=True,
        versions=[
            _version_stub(
                graph_config=_graph_config(
                    default_retrieval_mode="age_graph",
                    output_target="object_store_and_age",
                ),
                summary={"age_sync_status": "synced"},
            )
        ],
    )
    query, _build = _runners(db_session, session_factory)

    query({"version_ids": ["kbv-1"], "question": "q", "retrieval_modes": []})

    assert fake.last_request("query").retrieval_modes == ["age_graph"]


def test_query_default_modes_when_summary_not_a_dict(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    # summary is not a dict -> the ``isinstance`` guard falls back to {} so the
    # age_sync lookup misses and age_graph downgrades to graph_hybrid.
    fake = install_fake_service(
        age_enabled=True,
        versions=[
            _version_stub(
                graph_config=_graph_config(
                    default_retrieval_mode="age_graph",
                    output_target="object_store_and_age",
                ),
                summary="not-a-dict",
            )
        ],
    )
    query, _build = _runners(db_session, session_factory)

    query({"version_ids": ["kbv-1"], "question": "q", "retrieval_modes": []})

    assert fake.last_request("query").retrieval_modes == ["graph_hybrid"]


def test_query_none_retrieval_modes_defaults_to_dense(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service()
    query, _build = _runners(db_session, session_factory)

    query({"version_ids": ["kbv-1"], "question": "q"})

    assert fake.last_request("query").retrieval_modes == ["dense"]


# ---------------------------------------------------------------------------
# _run_knowledge_build
# ---------------------------------------------------------------------------


def test_build_raises_without_knowledge_base_id(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service()
    _query, build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="requires knowledge_base_id"):
        build({"chunking_strategy": "recursive", "embedding_model": "m"})


def test_build_raises_without_chunking_strategy(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service()
    _query, build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="requires chunking_strategy"):
        build({"knowledge_base_id": "kb-1", "embedding_model": "m"})


def test_build_raises_without_embedding_model(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service()
    _query, build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="requires embedding_model"):
        build({"knowledge_base_id": "kb-1", "chunking_strategy": "recursive"})


def test_build_completed_without_wait_or_activation(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    install_fake_service(create_version=_version_stub(status="completed"))
    _query, build = _runners(db_session, session_factory)

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "text-embedding-3-small",
        }
    )

    assert out["status"] == "completed"
    assert out["await_completion"]["status"] == "not_requested"
    assert out["activation"] == {"requested": False, "status": "skipped"}
    assert "Knowledge build completed" in out["summary"]


def test_build_activates_when_complete(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service(
        create_version=_version_stub(status="completed"),
        activated_kb=_kb_stub(active_version_id="kbv-1"),
    )
    _query, build = _runners(db_session, session_factory)

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "m",
            "activate_when_complete": True,
        }
    )

    assert out["activation"] == {
        "requested": True,
        "status": "activated",
        "active_version_id": "kbv-1",
    }
    assert "Activated as the knowledge base default." in out["summary"]
    assert any(c[0] == "activate_version" for c in fake.last().calls)


def test_build_activation_pending_when_not_completed(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    fake = install_fake_service(create_version=_version_stub(status="queued"))
    _query, build = _runners(db_session, session_factory)

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "m",
            "activate_when_complete": True,
        }
    )

    assert out["activation"]["status"] == "pending"
    assert out["activation"]["wait_status"] == "not_requested"
    assert not any(c[0] == "activate_version" for c in fake.last().calls)


def test_build_wait_for_completion_succeeds(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Avoid any real sleeping; the create returns processing and the poll then
    # returns completed.
    monkeypatch.setattr(promoter.time, "sleep", lambda _s: None)
    install_fake_service(
        create_version=_version_stub(status="processing"),
        versions=[_version_stub(status="completed")],
        runs=[_run_stub("kbr-1")],
    )
    _query, build = _runners(db_session, session_factory)

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "m",
            "wait_for_completion": True,
            "wait_timeout_seconds": 5,
        }
    )

    assert out["await_completion"]["requested"] is True
    assert out["await_completion"]["status"] == "completed"
    assert out["status"] == "completed"


def test_build_wait_times_out(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Poll always returns processing; advance the monotonic clock past the
    # deadline so the while-loop exits via timeout without real sleeping.
    monkeypatch.setattr(promoter.time, "sleep", lambda _s: None)
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(promoter.time, "monotonic", lambda: next(ticks, 100.0))

    install_fake_service(
        create_version=_version_stub(status="processing"),
        versions=[_version_stub(status="processing")],
    )
    _query, build = _runners(db_session, session_factory)

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "m",
            "wait_for_completion": True,
            "wait_timeout_seconds": 0.5,
            "activate_when_complete": True,
        }
    )

    assert out["await_completion"]["status"] == "timeout"
    assert out["activation"]["status"] == "pending"
    assert out["activation"]["wait_status"] == "timeout"
    assert "stopped waiting" in out["summary"]


def test_build_wait_raises_on_failed_status(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promoter.time, "sleep", lambda _s: None)
    # create returns processing so the poll loop runs; get_version then yields a
    # terminal ``failed`` status which trips the PublishError after the wait.
    install_fake_service(
        create_version=_version_stub(status="processing"),
        versions=[_version_stub(status="failed", error_summary="boom")],
    )
    _query, build = _runners(db_session, session_factory)

    with pytest.raises(PublishError, match="failed: boom"):
        build(
            {
                "knowledge_base_id": "kb-1",
                "chunking_strategy": "recursive",
                "embedding_model": "m",
                "wait_for_completion": True,
                "wait_timeout_seconds": 5,
            }
        )


def test_build_defaults_config_and_actor_to_identity(
    db_session: Session,
    session_factory: sessionmaker[Session],
    install_fake_service: Any,
) -> None:
    # No explicit config / actor -> resolved_config = CaliberConfig(),
    # resolved_actor = identity.user_id.
    fake = install_fake_service(create_version=_version_stub(status="completed"))
    _query, build = build_knowledge_runtime_runners(
        db_session,
        identity=_identity(),
    )

    out = build(
        {
            "knowledge_base_id": "kb-1",
            "chunking_strategy": "recursive",
            "embedding_model": "m",
        }
    )

    create_call = [c for c in fake.last().calls if c[0] == "create_version"][-1]
    # create_version records (knowledge_base_id, actor).
    assert create_call[1][1] == "@runner"
    assert out["status"] == "completed"
