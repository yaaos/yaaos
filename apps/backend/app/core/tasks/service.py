"""Task decorator + atomic-enqueue API (Phase 0b scaffold).

Phase 0b ships:
- `@task(name, queue=, max_retries=)` decorator that registers a task name +
  the wrapped coroutine in an in-process registry. The actual taskiq broker
  is wired in Phase 1.
- `enqueue(task_ref, args, *, session)` writes a `taskiq_enqueue` outbox row
  via `core/outbox.write`. The drain (Phase 1) pushes it to Redis.
- `TaskContext`: dataclass passed as the first arg of every task body —
  session opened per-task by the wrapper, traceparent, attempt, job_id.

The "atomic-in-session" contract is the headline: if the caller's session
commits, the task is durable; if it rolls back, the task never existed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tasks.drain import write as outbox_write


@dataclass(slots=True)
class TaskContext:
    """First positional arg of every task body. The wrapper opens `session`,
    passes the others through. Task bodies must not commit themselves — the
    wrapper commits at the end on success (or rolls back on raise)."""

    session: AsyncSession
    traceparent: str | None
    attempt: int
    job_id: str


@dataclass(slots=True, frozen=True)
class TaskRef:
    """Stable reference to a registered task. Returned by `@task`. `enqueue`
    accepts a TaskRef so callers can't typo a task name."""

    name: str
    queue: str
    max_retries: int


_REGISTRY: dict[str, tuple[TaskRef, Callable[..., Awaitable[Any]]]] = {}


def task(
    name: str,
    *,
    queue: str = "default",
    max_retries: int = 1,
) -> Callable[[Callable[..., Awaitable[Any]]], TaskRef]:
    """Decorator. Registers a task body under `name`. Returns a `TaskRef` —
    callers `enqueue(my_task, ...)` against that ref."""
    if not name:
        raise ValueError("task name required")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> TaskRef:
        if name in _REGISTRY:
            raise ValueError(f"task '{name}' already registered")
        ref = TaskRef(name=name, queue=queue, max_retries=max_retries)
        _REGISTRY[name] = (ref, fn)
        return ref

    return decorator


def get_registered(name: str) -> Callable[..., Awaitable[Any]] | None:
    """Lookup helper — workers / tests pull task bodies by name."""
    entry = _REGISTRY.get(name)
    return entry[1] if entry else None


def registered_task_names() -> list[str]:
    return sorted(_REGISTRY.keys())


async def enqueue(
    task_ref: TaskRef,
    args: dict[str, Any],
    *,
    session: AsyncSession,
) -> UUID:
    """Atomic-in-session enqueue. Writes a `taskiq_enqueue` outbox row that
    the drain delivers after commit. Returns the outbox row id."""
    payload = {
        "task_name": task_ref.name,
        "queue": task_ref.queue,
        "args": args,
    }
    return await outbox_write(session, kind="taskiq_enqueue", payload=payload)


_REGISTRY_SNAPSHOT: dict[str, tuple[TaskRef, Callable[..., Awaitable[Any]]]] | None = None


def _reset_for_tests() -> None:
    """Save the current registry and clear it — used by tests that register
    synthetic tasks. Pair every call with `_restore_after_tests()` so the
    cross-test invariant (real module-level task registrations) is preserved.
    Idempotent — repeated saves clobber the snapshot, which matches how the
    autouse fixture pattern works (one save+one restore per test)."""
    global _REGISTRY_SNAPSHOT
    _REGISTRY_SNAPSHOT = dict(_REGISTRY)
    _REGISTRY.clear()


def _restore_after_tests() -> None:
    """Counterpart to `_reset_for_tests()`. No-op if nothing was saved."""
    global _REGISTRY_SNAPSHOT
    if _REGISTRY_SNAPSHOT is None:
        return
    _REGISTRY.clear()
    _REGISTRY.update(_REGISTRY_SNAPSHOT)
    _REGISTRY_SNAPSHOT = None
