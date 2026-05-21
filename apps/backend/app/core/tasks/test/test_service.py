"""core/tasks scaffold coverage — registration + atomic enqueue via outbox."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.outbox.models import OutboxEntryRow
from app.core.tasks import TaskContext, enqueue, task
from app.core.tasks.service import (
    _reset_for_tests,
    _restore_after_tests,
    get_registered,
    registered_task_names,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    _reset_for_tests()
    yield
    _restore_after_tests()


def test_task_decorator_registers_name() -> None:
    @task("alpha")
    async def alpha(ctx: TaskContext) -> None:
        del ctx

    assert "alpha" in registered_task_names()
    assert get_registered("alpha") is not None


def test_double_register_raises() -> None:
    @task("dup")
    async def first(ctx: TaskContext) -> None:
        del ctx

    with pytest.raises(ValueError):

        @task("dup")
        async def second(ctx: TaskContext) -> None:
            del ctx


@pytest.mark.asyncio
async def test_enqueue_writes_outbox_row(db_session) -> None:
    @task("beta", queue="workflow", max_retries=3)
    async def beta(ctx: TaskContext, *, hint: str) -> None:
        del ctx, hint

    await enqueue(beta, args={"hint": "ok"}, session=db_session)
    await db_session.commit()

    row = (
        await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.kind == "taskiq_enqueue"))
    ).scalar_one()
    assert row.payload["task_name"] == "beta"
    assert row.payload["queue"] == "workflow"
    assert row.payload["args"] == {"hint": "ok"}
