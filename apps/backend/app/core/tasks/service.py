"""Task decorator + atomic-enqueue API.

- `@task(name, queue=, max_retries=)` registers a task body with the taskiq
  broker (single registry — `broker.find_task(name)` is the lookup). Returns
  a `TaskRef` callers `enqueue(my_task, ...)` against.
- `enqueue(task_ref, args, *, session)` writes a `taskiq_enqueue` outbox row
  via `core/outbox.write`. The drain pushes it to Redis.

The "atomic-in-session" contract is the headline: if the caller's session
commits, the task is durable; if it rolls back, the task never existed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_org_id
from app.core.shutdown_registry import (  # noqa: F401
    ShutdownHook,
    iter_worker_shutdown_hooks,
    register_worker_shutdown_hook,
)
from app.core.tasks.broker import get_broker
from app.core.tasks.drain import write as outbox_write
from app.core.tasks.types import TaskMetadata


@dataclass(slots=True, frozen=True)
class TaskRef:
    """Stable reference to a registered task. Returned by `@task`. `enqueue`
    accepts a TaskRef so callers can't typo a task name."""

    name: str
    queue: str
    max_retries: int


def task(
    name: str,
    *,
    queue: str = "default",
    max_retries: int = 1,
) -> Callable[[Callable[..., Awaitable[Any]]], TaskRef]:
    """Decorator. Registers a task body with the taskiq broker under `name`.
    Returns a `TaskRef` — callers `enqueue(my_task, ...)` against that ref.
    `queue` and `max_retries` ride as taskiq labels; the current
    `ListQueueBroker` ignores them, but future brokers / middleware can pick
    them up without API churn."""
    if not name:
        raise ValueError("task name required")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> TaskRef:
        broker = get_broker()
        if broker.find_task(name) is not None:
            raise ValueError(f"task '{name}' already registered")
        broker.task(task_name=name, queue=queue, max_retries=max_retries)(fn)
        return TaskRef(name=name, queue=queue, max_retries=max_retries)

    return decorator


async def enqueue(
    task_ref: TaskRef,
    args: dict[str, Any],
    *,
    metadata: TaskMetadata | dict[str, Any] | None = None,
    session: AsyncSession,
) -> UUID:
    """Atomic-in-session enqueue. Writes a `taskiq_enqueue` outbox row that
    the drain delivers after commit. Returns the outbox row id.

    `metadata` is an optional `TaskMetadata` forwarded through the outbox to
    the taskiq dispatch envelope. A raw dict is accepted for back-compat and
    coerced via `TaskMetadata.model_validate`. When omitted, auto-fills
    `TaskMetadata(org_id=...)` from the `org_id` contextvar if set (HTTP
    handlers don't need to pass it explicitly). Stays absent when no
    contextvar is set and no explicit value is given (system-bootstrap paths
    that run outside any org context)."""
    meta_obj: TaskMetadata | None
    if metadata is None:
        org_id = current_org_id()
        meta_obj = TaskMetadata(org_id=org_id) if org_id is not None else None
    elif isinstance(metadata, TaskMetadata):
        meta_obj = metadata
    else:
        meta_obj = TaskMetadata.model_validate(metadata)
    payload: dict[str, Any] = {
        "task_name": task_ref.name,
        "queue": task_ref.queue,
        "args": args,
        "metadata": meta_obj.model_dump(mode="json") if meta_obj is not None else None,
    }
    return await outbox_write(session, kind="taskiq_enqueue", payload=payload)


async def get_pending_task_names(session: AsyncSession) -> list[str]:
    """Return the `task_name` strings for all un-dispatched outbox entries.

    Service tests use this to assert a specific task was enqueued without
    importing `OutboxEntryRow` directly.
    """
    entries = await get_pending_outbox_payloads(session)
    return [e.get("task_name", "") for e in entries]


async def get_pending_outbox_payloads(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the full payload dicts for all un-dispatched outbox entries.

    Service tests use this to assert task arguments without importing
    `OutboxEntryRow` directly.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.tasks.models import OutboxEntryRow  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(OutboxEntryRow)
                .where(OutboxEntryRow.dispatched_at.is_(None))
                .order_by(OutboxEntryRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [dict(r.payload) for r in rows if r.kind == "taskiq_enqueue"]


@contextmanager
def scoped_task_registration(task_ref: TaskRef) -> Iterator[TaskRef]:
    """Context manager for temporary task registrations in tests.

    Expects `task_ref` to have just been registered with the broker (e.g.
    by calling `@task(...)` inside the test body). On exit, removes the
    named entry from the broker's registry — so the same name can be
    re-registered in a subsequent test without the duplicate-name guard
    firing.
    """
    try:
        yield task_ref
    finally:
        get_broker().local_task_registry.pop(task_ref.name, None)


async def shutdown() -> None:
    """Gracefully shut down the taskiq broker connection.

    Called by the process shutdown registries during web/worker teardown.
    Calls the broker's own async `shutdown()` to close its connections.
    Does NOT drop the `_broker` singleton — keeping the object means task
    registrations (set at import time via `@task`) remain valid; only the
    connection is torn down. Does NOT touch `_REGISTRY_SNAPSHOT` — that is
    managed exclusively by the test isolation helpers.
    """
    import contextlib as _contextlib  # noqa: PLC0415

    from app.core.tasks.broker import get_broker as _get_broker  # noqa: PLC0415

    _broker = _get_broker()
    with _contextlib.suppress(Exception):
        await _broker.shutdown()
