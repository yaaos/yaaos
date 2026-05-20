"""`spawn()` + `active_task_count()` — fire-and-forget background-coroutine helper.

Wraps `asyncio.create_task` with a try/except that logs `spawn.crashed`. Lives
under `core/observability` because the job is exception logging for background
tasks — production logs become unusable without it. Previously in
`core/primitives`; relocated in M04 Phase 6a.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import structlog

log = structlog.get_logger("observability.spawn")


# Module-level set keeps spawned tasks alive (asyncio's standard pitfall —
# without a strong reference, the GC may collect them mid-flight).
_tasks: set[asyncio.Task[Any]] = set()


def spawn(name: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[Any]:
    """Fire-and-forget background work.

    Wraps `coro` in a try/except that logs `spawn.crashed` with a stack trace
    if the coroutine raises. The coroutine itself is expected to mark its own
    domain row failed before raising; spawn() catches as a last-resort safety
    net.
    """

    async def _wrapper() -> None:
        try:
            await coro
        except Exception:
            logging.getLogger("yaaos").exception("spawn.crashed", extra={"spawn_name": name})

    task = asyncio.create_task(_wrapper(), name=f"spawn:{name}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.debug("spawn.started", spawn_name=name)
    return task


def active_task_count() -> int:
    """Test helper — number of pending spawned tasks."""
    return sum(1 for t in _tasks if not t.done())


__all__ = ["active_task_count", "spawn"]
