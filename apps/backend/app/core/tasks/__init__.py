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

# Register the daily prune for `scheduled_runs` — this is the first
# `@scheduled` consumer (self-exercising; proves the wiring).
from app.core.tasks import scheduled_runs_prune as _scheduled_runs_prune  # noqa: F401
from app.core.tasks.broker import get_broker, set_broker_for_tests
from app.core.tasks.cron import CronExpr
from app.core.tasks.drain import drain_once
from app.core.tasks.scheduler import (
    schedule_task,
    scheduled,
    scheduler_loop,
    set_scheduler_for_tests,
    tick_once,
)
from app.core.tasks.service import (
    TaskRef,
    enqueue,
    get_pending_outbox_payloads,
    get_pending_task_names,
    shutdown,
    task,
)
from app.core.tasks.types import TaskMetadata

__all__ = [
    "CronExpr",
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
    "schedule_task",
    "scheduled",
    "scheduler_loop",
    "set_broker_for_tests",
    "set_scheduler_for_tests",
    "shutdown",
    "task",
    "tick_once",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
