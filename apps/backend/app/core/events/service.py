"""In-process pub/sub: domain modules publish typed events; SSE subscribers consume."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

log = structlog.get_logger("events")


class Event(BaseModel):
    """Base event envelope. Domain modules subclass with their own `kind` literal."""

    kind: str
    source_module: str
    ts: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    ticket_id: UUID | None = None


class EventFilter(BaseModel):
    ticket_id: UUID | None = None
    kinds: list[str] | None = None

    def matches(self, event: Event) -> bool:
        if self.ticket_id is not None and event.ticket_id != self.ticket_id:
            return False
        if self.kinds is not None and event.kind not in self.kinds:
            return False
        return True


# subscriber_id -> (filter, queue)
_subscribers: dict[str, tuple[EventFilter, asyncio.Queue[Event]]] = {}


async def publish(event: Event) -> None:
    """Dispatch to matching subscribers. Slow subscribers don't block fast ones —
    each has its own bounded queue; overflow drops with a log line."""
    for sub_id, (filt, queue) in list(_subscribers.items()):
        if filt.matches(event):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event.dropped", subscriber=sub_id, kind=event.kind)


async def subscribe(filter: EventFilter) -> AsyncIterator[Event]:
    """Yield events matching `filter`. Unregisters on consumer exit."""
    sub_id = str(uuid4())
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
    _subscribers[sub_id] = (filter, queue)
    try:
        while True:
            yield await queue.get()
    finally:
        _subscribers.pop(sub_id, None)


async def shutdown() -> None:
    """Clear the subscriber registry. Called by the process shutdown registries."""
    _subscribers.clear()


def subscriber_count() -> int:
    return len(_subscribers)


def serialize_for_sse(event: Event) -> str:
    """Serialize an Event for `text/event-stream` output."""
    return f"data: {event.model_dump_json()}\n\n"


# Helper for endpoint handler — keeps web.py simple and lets us reuse in tests.
async def stream_events_for_filter(filter: EventFilter) -> AsyncIterator[str]:
    async for event in subscribe(filter):
        yield serialize_for_sse(event)


# Re-export common typing alias for callers
EventDict = dict[str, Any]


# --- publish-after-commit ---------------------------------------------------
#
# Domain code that owns a write transaction needs to publish an event tied to
# the transaction's outcome: fire if the caller commits, discard if the caller
# rolls back. Stashing the event on `session.info` and flushing it from a
# SQLAlchemy `after_commit` listener gives us exactly that semantics, with no
# ceremony at the call site beyond `publish_after_commit(session, evt)`.
#
# The listener is sync (SQLAlchemy event), so it schedules `publish()` (async)
# onto the running loop via `create_task`.

_PENDING_EVENTS_KEY = "yaaos_pending_events"

# Strong refs to in-flight publish() tasks so asyncio doesn't GC them mid-fan-out
# (Python's event loop only holds weak refs to tasks created via create_task).
_inflight_publish_tasks: set[asyncio.Task[None]] = set()


def publish_after_commit(session: AsyncSession, evt: Event) -> None:
    """Queue an event to be published when this session commits. Rollback
    discards. No await — events fan out on the next loop tick after commit."""
    pending: list[Event] = session.sync_session.info.setdefault(_PENDING_EVENTS_KEY, [])
    pending.append(evt)


@sa_event.listens_for(Session, "after_commit")
def _flush_pending_events(sync_session: Session) -> None:
    pending: list[Event] | None = sync_session.info.pop(_PENDING_EVENTS_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should not happen in production (AsyncSession.commit
        # is awaited). Drop with a warning rather than crash the commit path.
        log.warning("event.flush.no_loop", count=len(pending))
        return
    for evt in pending:
        task = loop.create_task(publish(evt))
        _inflight_publish_tasks.add(task)
        task.add_done_callback(_inflight_publish_tasks.discard)
