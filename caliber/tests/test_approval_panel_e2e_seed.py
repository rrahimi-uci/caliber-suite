"""The approval panel's Playwright fixtures are what the spec says they are (B06.5, #743).

``scripts/approval_panel_e2e_seed.py`` records parked runs with requests bound to effect proposals through the runtime's own
writers. The browser spec (``caliber-ui/e2e/approval-panel.spec.ts``) names those runs, so a change to the writers or the
script must not silently change what the spec drives. This runs the script against a throwaway SQLite database and checks
each scenario's rows, and that the argument value the spec hunts for really is in the stored proposal (so "never shown" is
a statement about something that exists).
"""

from __future__ import annotations

import runpy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from caliber.config import CaliberConfig
from caliber.db.models import (
    Base,
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "approval_panel_e2e_seed.py"
SPEC = Path(__file__).resolve().parents[1] / "caliber-ui" / "e2e" / "approval-panel.spec.ts"


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'seed.sqlite'}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", url)
    engine = create_engine_from_config(CaliberConfig.load())
    Base.metadata.create_all(engine)
    engine.dispose()
    module = runpy.run_path(str(SCRIPT))
    assert module["main"]() == 0
    engine = create_engine_from_config(CaliberConfig.load())
    try:
        with sessionmaker_from_engine(engine)() as session:
            yield session, module
    finally:
        engine.dispose()


def _requests(session, run_id: str) -> list[CaliberRuntimeApprovalRequest]:  # type: ignore[no-untyped-def]
    return list(
        session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .order_by(
                CaliberRuntimeApprovalRequest.requested_at,
                CaliberRuntimeApprovalRequest.runtime_approval_id,
            )
        ).scalars()
    )


def test_every_run_the_spec_opens_is_parked_at_the_gate_with_bound_requests(seeded) -> None:  # type: ignore[no-untyped-def]
    session, module = seeded
    spec = SPEC.read_text(encoding="utf-8")
    for run_id in (
        "WR-E2E-QUORUM",
        "WR-E2E-KEYBOARD",
        "WR-E2E-EXPIRED",
        "WR-E2E-SUPERSEDED",
        "WR-E2E-SELF",
    ):
        assert run_id in spec, f"{run_id} is not used by the spec"
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert (run.status, run.current_node_id) == ("waiting_approval", module["NODE_ID"])
        rows = _requests(session, run_id)
        assert rows and all(row.proposal_id and row.intent_hash for row in rows)


def test_the_scenarios_have_the_policy_and_window_the_spec_relies_on(seeded) -> None:  # type: ignore[no-untyped-def]
    session, _module = seeded
    quorum = _requests(session, "WR-E2E-QUORUM")[0]
    assert quorum.policy_snapshot is not None and quorum.policy_snapshot["approval_count"] == 2
    keyboard = _requests(session, "WR-E2E-KEYBOARD")[0]
    assert keyboard.policy_snapshot is not None and keyboard.policy_snapshot["approval_count"] == 1
    assert keyboard.status == "pending"

    expired = _requests(session, "WR-E2E-EXPIRED")[0]
    assert expired.expires_at is not None
    assert expired.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)

    old, new = sorted(
        _requests(session, "WR-E2E-SUPERSEDED"), key=lambda row: row.status != "superseded"
    )
    assert (old.status, old.superseded_by) == ("superseded", new.runtime_approval_id)
    assert (new.status, new.superseded_by) == ("pending", None)

    own = _requests(session, "WR-E2E-SELF")[0]
    assert own.requested_by == "admin"
    assert own.policy_snapshot is not None and own.policy_snapshot["allow_self_approval"] is False
    run = session.get(CaliberWorkflowRun, "WR-E2E-SELF")
    assert run is not None and run.created_by == "admin"


def test_the_secret_the_spec_hunts_for_is_really_in_the_stored_arguments(seeded) -> None:  # type: ignore[no-untyped-def]
    session, module = seeded
    secret = module["ARGUMENT_CANARY"]
    assert f'"{secret}"' in SPEC.read_text(encoding="utf-8"), (
        "the spec and the seed name different secrets"
    )
    proposal = session.execute(select(CaliberEffectProposal)).scalars().first()
    assert proposal is not None
    assert secret in repr(proposal.args)


def test_the_script_refuses_to_seed_twice(seeded, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    _session, module = seeded
    assert module["main"]() == 1
    assert "already seeded" in capsys.readouterr().err
