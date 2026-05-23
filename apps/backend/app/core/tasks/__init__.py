"""core/tasks — durable task scheduler over taskiq + Redis.

Public surface:

    @task("route_workflow", queue="workflow", max_retries=3)
    async def route_workflow(ctx: TaskContext, exec_id: str, ...): ...

    async with db_session() as s:
        await tasks.enqueue(route_workflow, args={...}, session=s)
        await s.commit()

`enqueue(session=)` writes an `outbox_entries` row in the caller's
session — atomic with everything else the caller commits. The drain
loop (in the worker process) reads those rows post-commit and dispatches
them to the taskiq broker (Redis). Task bodies run under taskiq workers
in the worker process; they receive a fresh DB session via `TaskContext`.

The outbox is a private substrate of this module — domain callers only
see `task`, `enqueue`, `TaskRef`, `TaskContext`. `OutboxEntryRow` and
`drain_once` are exported for tests and the worker entrypoint.
"""

from app.core.tasks.drain import drain_once, write
from app.core.tasks.models import OutboxEntryRow
from app.core.tasks.service import TaskContext, TaskRef, enqueue, task

__all__ = [
    "OutboxEntryRow",
    "TaskContext",
    "TaskRef",
    "drain_once",
    "enqueue",
    "task",
    "write",
]
