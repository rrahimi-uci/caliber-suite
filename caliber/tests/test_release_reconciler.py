from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberReleaseOperation
from caliber.orchestrator.release_reconciler import ReleaseReconcilerTask
from caliber.release_operations import prepare_prompt_alias_release


def test_tick_settles_incomplete_release(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        operation = prepare_prompt_alias_release(
            session,
            name="p-background",
            alias="prod",
            version_before=1,
            version_after=2,
            actor="@operator",
        )
        operation.status = "applying"
        session.commit()
        operation_id = operation.operation_id

    reconciler = ReleaseReconcilerTask(
        session_factory,
        resolve_alias=lambda _name, _alias: {"version": 2},
    )
    assert reconciler._tick() == 1

    with session_factory() as session:
        row = session.get(CaliberReleaseOperation, operation_id)
        assert row is not None and row.status == "applied"
        assert row.active_lock is None


@pytest.mark.asyncio
async def test_reconciler_lifecycle(session_factory: sessionmaker[Session]) -> None:
    reconciler = ReleaseReconcilerTask(
        session_factory,
        resolve_alias=lambda _name, _alias: None,
        interval_seconds=999,
    )
    await reconciler.start()
    with pytest.raises(RuntimeError, match="already running"):
        await reconciler.start()
    await reconciler.stop()
    await reconciler.stop()
