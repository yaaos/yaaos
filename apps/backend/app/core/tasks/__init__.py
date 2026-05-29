"""core/tasks — durable task scheduler over taskiq + Redis.

Public surface:

    @task("route_workflow", queue="workflow", max_retries=3)
    async def route_workflow(exec_id: str, ...): ...

    async with db_session() as s:
        await tasks.enqueue(route_workflow, args={...}, session=s)
        await s.commit()

`enqueue(session=)` writes an `outbox_entries` row in the caller's
session — atomic with everything else the caller commits. The drain
loop (in the worker process) reads those rows post-commit and dispatches
them to the taskiq broker (Redis). Task bodies run under taskiq workers
in the worker process and are invoked with the kwargs the caller passed
to `enqueue`.

The outbox is a private substrate of this module — domain callers only
see `task`, `enqueue`, `TaskRef`.
"""

from app.core.shutdown_registry import (
    ShutdownHook,
    iter_worker_shutdown_hooks,
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)
from app.core.tasks.broker import get_broker
from app.core.tasks.drain import drain_once
from app.core.tasks.service import (
    TaskRef,
    enqueue,
    get_pending_outbox_payloads,
    get_pending_task_names,
    scoped_task_registration,
    shutdown,
    task,
)
from app.core.tasks.types import TaskMetadata

__all__ = [
    "ShutdownHook",
    "TaskMetadata",
    "TaskRef",
    "drain_once",
    "enqueue",
    "get_broker",
    "get_pending_outbox_payloads",
    "get_pending_task_names",
    "iter_worker_shutdown_hooks",
    "register_worker_shutdown_hook",
    "scoped_task_registration",
    "shutdown",
    "task",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
