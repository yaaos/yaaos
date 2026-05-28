"""core/tasks scaffold coverage — registration + atomic enqueue via outbox."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.tasks import enqueue, scoped_task_registration, task
from app.core.tasks.broker import get_broker
from app.core.tasks.models import OutboxEntryRow


def test_task_decorator_registers_name() -> None:
    async def _alpha() -> None:
        return None

    ref = task("alpha")(_alpha)
    with scoped_task_registration(ref):
        assert get_broker().find_task("alpha") is not None

    assert get_broker().find_task("alpha") is None


def test_double_register_raises() -> None:
    async def _dup1() -> None:
        return None

    async def _dup2() -> None:
        return None

    ref = task("dup")(_dup1)
    with scoped_task_registration(ref):
        with pytest.raises(ValueError):
            task("dup")(_dup2)


@pytest.mark.asyncio
async def test_enqueue_writes_outbox_row(db_session) -> None:  # type: ignore[no-untyped-def]
    async def _beta(*, hint: str) -> None:
        del hint

    ref = task("beta", queue="workflow", max_retries=3)(_beta)
    with scoped_task_registration(ref):
        await enqueue(ref, args={"hint": "ok"}, session=db_session)
        await db_session.commit()

        row = (
            await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.kind == "taskiq_enqueue"))
        ).scalar_one()
        assert row.payload["task_name"] == "beta"
        assert row.payload["queue"] == "workflow"
        assert row.payload["args"] == {"hint": "ok"}
