"""core/tasks — durable task scheduler over taskiq + Redis (Phase 0b scaffold).

Public surface (illustrative):

    @task("route_workflow", queue="workflow", max_retries=3)
    async def route_workflow(ctx: TaskContext, exec_id: str, ...): ...

    async with db_session() as s:
        await tasks.enqueue(route_workflow, args={...}, session=s)
        await s.commit()

`enqueue(session=)` routes through `core/outbox.write(..., kind='taskiq_enqueue')`
so the task is durable iff the caller's transaction commits. The drain loop
in `apps/backend/bin/worker` pushes outbox rows to Redis.

M05 Phase 0b: scaffold only — the broker, decorator wrapper, and worker
entrypoint are stubbed; Phase 0c/1 wire taskiq + Redis end-to-end.
"""

from app.core.tasks.service import TaskContext, TaskRef, enqueue, task

__all__ = ["TaskContext", "TaskRef", "enqueue", "task"]
