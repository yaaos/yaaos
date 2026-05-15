# `core/events` — Internal Architecture

> In-process pub/sub for SSE broadcasting to UI clients.
> M01: single-process, in-memory. M02+ (when a worker process appears): swap to Postgres LISTEN/NOTIFY or similar, behind the same interface.

## Purpose

`core/events` is a thin in-process transport. Domain modules `publish()` typed events; UI clients subscribe via a single SSE endpoint. The transport doesn't own event types or know what events mean — domain modules define their own subclasses of a base `Event` envelope.

It is **not** durable. Lost on process restart. Missed events while a client is disconnected are missed. The UI is expected to re-fetch state on (re)connect. `audit_log` is the durable record; `core/events` is the live wire.

## Public interface (`__all__`)

```python
"Event",          # base Pydantic class; domain modules subclass
"EventFilter",    # subscriber's filter criteria
"publish",        # async publish API
"subscribe",      # async iterator
```

The SSE HTTP endpoint (`GET /api/events`) is owned by this module via `register_routes(RouteSpec(...))` from `core/webserver`.

## `Event` envelope

```python
class Event(BaseModel):
    kind: str                # discriminator; each subclass sets a Literal
    source_module: str       # name of the module that published it (for debugging)
    ts: datetime             # publication time
    ticket_id: UUID | None = None   # optional scoping field used by filter
```

Domain modules subclass:

```python
# in domain/reviewer/events.py
class ReviewJobStatusChanged(Event):
    kind: Literal["review_job_status_changed"] = "review_job_status_changed"
    source_module: Literal["reviewer"] = "reviewer"
    ticket_id: UUID
    pr_id: UUID
    agent_id: UUID
    review_job_id: UUID
    status: ReviewJobStatus
```

The `ticket_id` field on the base is the standard scoping key for M01 (UI filters by ticket). Other scoping keys (e.g., `repo_id`) can be added if a future event needs them.

Subscribers can dispatch by `isinstance` or by the `kind` literal field.

## `EventFilter`

```python
class EventFilter(BaseModel):
    ticket_id: UUID | None = None        # match only events with this ticket_id
    kinds: list[str] | None = None       # match only events whose kind is in this list

    def matches(self, event: Event) -> bool:
        if self.ticket_id is not None and event.ticket_id != self.ticket_id:
            return False
        if self.kinds is not None and event.kind not in self.kinds:
            return False
        return True
```

When both fields are `None`, the filter matches every event (broadcast).

## `publish` and `subscribe`

```python
_subscribers: dict[SubscriberID, tuple[EventFilter, asyncio.Queue[Event]]] = {}

async def publish(event: Event) -> None:
    """Fire-and-forget. Dispatches to all subscribers whose filter matches.
    Slow subscribers don't block fast ones — each has its own queue.
    """
    for sub_id, (filt, queue) in _subscribers.items():
        if filt.matches(event):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # POC: log and drop the event for this subscriber.
                # The UI re-fetches on next interaction, so loss is recoverable.
                log.warning("event.dropped", subscriber=sub_id, kind=event.kind)


async def subscribe(filter: EventFilter) -> AsyncIterator[Event]:
    """Yield events matching `filter` until the consumer stops iterating.
    Backed by a per-subscriber asyncio.Queue. On consumer exit (or cancellation),
    the subscriber is unregistered.
    """
    sub_id = SubscriberID(str(uuid4()))
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
    _subscribers[sub_id] = (filter, queue)
    try:
        while True:
            yield await queue.get()
    finally:
        _subscribers.pop(sub_id, None)
```

`maxsize=100` is the POC backpressure setting — if a subscriber falls more than 100 events behind, new events are dropped for that subscriber (with a log line). The UI is expected to recover by re-fetching state. For M01 traffic (a few events per PR per minute), this is generous.

## The SSE endpoint

`core/events` registers its own route:

```python
# in core/events/web.py
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()  # no prefix — core/webserver applies it from RouteSpec

@router.get("")  # mounted at /api/events
async def stream_events(
    ticket_id: UUID | None = None,
    kinds: list[str] | None = Query(None),
):
    filter = EventFilter(ticket_id=ticket_id, kinds=kinds)

    async def _gen():
        async for event in subscribe(filter):
            payload = event.model_dump_json()
            yield f"data: {payload}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")

register_routes(RouteSpec(module_name="events", router=router))
```

- One endpoint: `GET /api/events`.
- Optional query params: `ticket_id`, `kinds` (repeatable).
- Returns `text/event-stream`; events serialized as JSON in the `data:` field.
- No `Last-Event-ID` replay support in M01.

## Subscriber lifecycle

1. Client opens `EventSource('/api/events?ticket_id=...')` from the SPA.
2. FastAPI calls the endpoint; the async generator starts; `subscribe()` registers a queue.
3. Events flow as they're published by domain modules.
4. Client disconnects (page nav, browser close, network drop) → generator's `finally` runs → subscriber unregistered.
5. Browser `EventSource` auto-reconnects on transient drop; a fresh subscription is created. Missed events during the gap are lost; UI re-fetches.

## Bootstrap & teardown

- `core/events` initializes its `_subscribers` dict at module import time. No special bootstrap step.
- Its SSE route is mounted via the usual `register_routes()` flow at lifespan startup (per `core/webserver`).
- No graceful shutdown handling. Existing SSE connections die with the process. Acceptable for POC.

## What `core/events` does NOT do

- Does not persist events. `audit_log` is the durable counterpart.
- Does not replay events on reconnect (no Last-Event-ID support).
- Does not cross processes. M01 is single-process; M02+ swap-in handles cross-process.
- Does not define event types — domain modules do.
- Does not own non-SSE wire protocols (no WebSocket).

## M02 forward-compat

When a separate worker process is added (M02+ long-running invocation supervisor for implementer agents):

- Publishers in the worker process can't reach in-process subscribers in the web process.
- Swap the internal dispatch to a cross-process mechanism. Likely options: Postgres `LISTEN/NOTIFY` (publish writes a notification; each process's listener task fans out to local subscribers) or a small Redis pub/sub layer.
- **The public interface (`publish`, `subscribe`, `EventFilter`, `Event`) does not change.** Internal `_subscribers` dispatch is replaced by the new mechanism.
- Domain modules don't change their publish calls.

## Decisions

### 2026-05-14 — `Event` is a Pydantic base; domain modules subclass with their own `kind` literal
Subscribers can dispatch by `isinstance` or by `event.kind`. No central catalog of all event types in `core/events`; each module owns its types.

### 2026-05-14 — Single `GET /api/events` SSE endpoint with `ticket_id` and `kinds` query filters
Server filters. Smallest surface for the SPA to learn. Domain-specific endpoints can be added later if needed.

### 2026-05-14 — Async iterator subscription, bounded queue per subscriber
`async for event in subscribe(filter)` composes naturally with FastAPI's `StreamingResponse`. Per-subscriber bounded queue (maxsize 100) decouples slow consumers from publishers. Drops on overflow are logged; UI recovers via re-fetch.

### 2026-05-14 — In-memory only; no event replay; missed events while disconnected are lost
Live wire only. `audit_log` is the durable record. POC simplicity over completeness.
