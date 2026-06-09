"""Recurring-task scheduler — cluster-safe via per-tick atomic claim.

Public surface (re-exported from `core.tasks.__init__`):

    @scheduled(name="prune_scheduled_runs", cron="0 0 * * *")
    async def _prune() -> None: ...

    schedule_task("daily_X", cron="0 0 * * *", task_ref=my_task_ref)

Schedules are **static / declarative** — registered at import time into a
process-local registry (no runtime registration race). Each worker runs
the tick loop alongside the outbox drain; the tick evaluates every
registered schedule against the floored-minute slot. For each schedule
whose cron matches the slot it attempts an `INSERT … ON CONFLICT DO
NOTHING` on `scheduled_runs(schedule_id, fire_time)`. **The row insert
is the cluster-safe gate**: only the worker whose insert wins
(rowcount==1) calls `enqueue(...)`. The losers see `rowcount==0` and
skip. No leader election, no SPOF, self-healing — mirrors the
`github_webhook_events` `ON CONFLICT DO NOTHING` dedup precedent.

Task bodies remain idempotent (already mandated by `core/tasks`); the
claim is the strong guarantee that exactly one enqueue happens per slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session as db_session
from app.core.tasks.cron import CronExpr, floor_to_minute

log = structlog.get_logger("core.tasks.scheduler")

_SCHEDULES: dict[str, _Schedule] = {}

# Upper bound on the scheduler-loop error-backoff sleep. Caps the retry
# cadence during a persistent outage so the log rate stays O(log(duration)).
_MAX_SLEEP_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class _Schedule:
    """One registered schedule. `task_ref` is the underlying `TaskRef`
    the scheduler enqueues; the cron-matched slot drives whether to
    enqueue for the current minute."""

    schedule_id: str
    cron: CronExpr
    task_ref: Any  # TaskRef — typed as Any to avoid an import cycle.


def schedule_task(name: str, cron: str, *, task_ref: Any) -> None:
    """Register a static schedule. `name` is the durable `schedule_id`
    (the PK part on `scheduled_runs`); `cron` is a 5-field expression
    parsed by `CronExpr.parse`; `task_ref` is the `TaskRef` returned by
    `@task(...)`.

    Idempotent re-registration of the same `(name, task_ref)` is fine
    (replaces silently); registering two different bodies under the same
    name raises so tests catch the collision."""
    if not name:
        raise ValueError("schedule name required")
    if not task_ref:
        raise ValueError("task_ref required")
    expr = CronExpr.parse(cron)
    existing = _SCHEDULES.get(name)
    if existing is not None and existing.task_ref is not task_ref:
        raise ValueError(f"schedule '{name}' already registered with a different task body")
    _SCHEDULES[name] = _Schedule(schedule_id=name, cron=expr, task_ref=task_ref)


def scheduled(
    name: str, cron: str, *, queue: str = "default", max_retries: int = 1
) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
    """Decorator: registers a `@task` body AND a schedule in one move.
    The decorated function becomes a regular task body; calling
    `enqueue(returned_ref, ...)` works exactly as for `@task`. The cron
    fires the same task at every matching slot — the body must be
    idempotent (same rule as every `core/tasks` body).

    Returns the `TaskRef` so test code can inspect the registration."""
    # Lazy import to avoid a circular dep at module-load time.
    from app.core.tasks.service import task as _task  # noqa: PLC0415

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Any:
        ref = _task(name, queue=queue, max_retries=max_retries)(fn)
        schedule_task(name, cron, task_ref=ref)
        return ref

    return decorator


def registered_schedule_ids() -> list[str]:
    """Test introspection — names registered in this process."""
    return sorted(_SCHEDULES.keys())


def _reset_schedules_for_tests() -> None:
    """Intra-module helper — clears the static registry between tests.

    Not in `__all__`, not surfaced cross-module; the autouse fixture in
    `app/testing/isolation.py` calls this to keep test bodies clean.
    """
    _SCHEDULES.clear()


async def tick_once(*, session: AsyncSession, now: datetime | None = None) -> list[str]:
    """Evaluate every registered schedule for the floored-minute slot of
    `now`. For each matching schedule, attempt the `ON CONFLICT DO
    NOTHING` claim; on win, enqueue. Returns the list of schedule_ids
    that won the claim and were enqueued this tick.

    `now` defaults to `datetime.now(UTC)`. The session is taken as a
    parameter — the caller commits. Tests drive this directly with
    concurrent sessions to assert exactly-once.
    """
    when = now if now is not None else datetime.now(UTC)
    slot = floor_to_minute(when)
    fired: list[str] = []
    # Snapshot so iteration is stable against concurrent late-binds.
    schedules = list(_SCHEDULES.values())
    # Lazy import — `enqueue` lives in service.py and imports the broker.
    from app.core.tasks.service import enqueue as _enqueue  # noqa: PLC0415

    for sched in schedules:
        if not sched.cron.matches(slot):
            continue
        if not await _try_claim(session, schedule_id=sched.schedule_id, fire_time=slot):
            continue
        # We won the slot — enqueue the task body. Failure here is
        # surfaced loudly; the claim row stays so a retry on the same
        # slot does NOT re-enqueue (the strong invariant of exactly-once
        # at the claim).
        await _enqueue(sched.task_ref, args={}, session=session)
        fired.append(sched.schedule_id)
    return fired


async def _try_claim(
    session: AsyncSession,
    *,
    schedule_id: str,
    fire_time: datetime,
) -> bool:
    """Atomic per-tick claim. Returns True iff this caller inserted the
    row (i.e. won the slot); False if another worker already inserted.

    `INSERT … ON CONFLICT DO NOTHING` on the composite PK
    `(schedule_id, fire_time)` is the sole gate — see module docstring.
    """
    result = await session.execute(
        sa_text(
            "INSERT INTO scheduled_runs (schedule_id, fire_time) "
            "VALUES (:schedule_id, :fire_time) "
            "ON CONFLICT (schedule_id, fire_time) DO NOTHING"
        ),
        {"schedule_id": schedule_id, "fire_time": fire_time},
    )
    # rowcount is 1 if INSERT succeeded, 0 if the conflict skipped it.
    return result.rowcount == 1


def _backoff_sleep(
    consecutive_failures: int,
    tick_interval: float,
    max_sleep: float = _MAX_SLEEP_SECONDS,
) -> float:
    """Exponential backoff sleep for the scheduler loop's error path.

    `consecutive_failures` is the count of ticks that failed in a row
    *before* this sleep (1 on the first failure). The sleep grows as
    `tick_interval * 2**consecutive_failures`, capped at `max_sleep`, so a
    persistent outage produces a log rate of O(log(duration)) rather than
    a fixed cadence.
    """
    return min(tick_interval * 2**consecutive_failures, max_sleep)


async def scheduler_loop(*, tick_interval_seconds: float = 20.0) -> None:
    """Long-running tick loop. Runs in the worker alongside the drain.

    Tick cadence is sub-minute (default 20 s) so each fire slot gets
    multiple convergence passes, but the `floor_to_minute` normalization
    means every pass within a minute races the same `(schedule_id,
    fire_time)` row — no double-enqueues from multiple passes within one
    slot.

    Failures in `tick_once` are logged + swallowed; the loop never exits
    on a transient DB or broker hiccup. On a persistent failure the sleep
    grows by exponential backoff (`tick_interval_seconds * 2**failures`,
    capped at 120 s) so the error-log rate stays bounded; a successful
    tick resets the counter and restores the normal cadence.
    """
    consecutive_failures = 0
    while True:
        try:
            async with db_session() as s:
                fired = await tick_once(session=s)
                await s.commit()
            if fired:
                log.info("tasks.scheduler.fired", schedule_ids=fired)
            consecutive_failures = 0
            sleep_seconds = tick_interval_seconds
        except Exception:
            consecutive_failures += 1
            sleep_seconds = _backoff_sleep(consecutive_failures, tick_interval_seconds)
            log.exception(
                "tasks.scheduler.tick_failed",
                consecutive_failures=consecutive_failures,
                backoff_seconds=sleep_seconds,
            )
        await asyncio.sleep(sleep_seconds)
