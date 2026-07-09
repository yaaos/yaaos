"""Shared `_drain` outbox-dispatch helper for `domain/pipelines` service
tests. Every service test in this module that exercises the
ROUTE_RUN/START_STAGE taskiq trio reuses this rather than re-defining its
own dispatcher.

Intra-module test helper — reached via direct submodule import from
sibling test files in this same `test/` directory, per
`apps/backend/docs/patterns.md` § Module boundaries in tests.
"""

from __future__ import annotations

from typing import Any

from app.core.tasks import drain_once, get_broker, get_pending_task_names


async def drain(db_session: Any, *, max_iters: int = 50) -> None:
    """Repeatedly pop pending outbox rows and call the matching task body's
    `original_func` directly (bypassing the real Redis broker) until the
    outbox is empty or `max_iters` is hit."""

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None, f"no task body for {payload['task_name']}"
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return
