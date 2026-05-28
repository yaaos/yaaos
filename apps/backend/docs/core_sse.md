# core/sse

> Redis-backed pub/sub for ActivityEvent fanout and org-scoped general events.

## Purpose

Two pipelines in one module:

- **Activity pipeline** — bridges activity-event producers (`core/agent_gateway` WebSocket ingress, reviewer's direct publisher) and the per-workflow SSE handler. Channel shape: `activity:{workflow_execution_id}`.
- **General-event pipeline** — org-scoped typed events consumed by the SPA's live-update stream. Channel shape: `{org_id}:general`. Uses `GeneralEventKind` as the discriminator. `publish_general_after_commit` ties publish lifetime to a transaction — rollbacks silently discard stashed events so rolled-back transactions never emit SPA events.

Both pipelines are backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process reaches an SSE subscriber attached to a different web process. Fire-and-forget per Redis semantics — slow consumers do not backpressure publishers, and no event persistence.

The `/api/sse` prefix is declared as `ORG_SCOPED` in `core/auth/types.py` so future routes mounted at `core/sse/web.py` are enforced without additional classification work.

## Public interface

Exported from `app/core/sse/__init__.py`:

**Activity pipeline:**

- `publish(channel, event)` — fan out to every subscriber on `channel`; returns the Redis-reported delivery count (number of subscribers across the cluster).
- `subscribe(channel)` — async iterator that yields each subsequent event published on `channel`. Subscriber registers a Redis subscription on first iteration and unregisters when the iterator exits.
- `channel_for(workflow_execution_id)` — centralized name shape (`activity:{id}`) so publishers + subscribers agree.
- `subscriber_count(channel)` — diagnostic; **local-process** subscriber count (Redis's `PUBSUB NUMSUB` is cluster-wide and not what callers want).

**General-event pipeline:**

- `GeneralEventKind` — closed `StrEnum` with 15 members: `TICKET_STATUS_CHANGED`; reviewer aggregate kinds `REVIEW_REQUESTED`, `REVIEW_STARTED`, `REVIEW_COMPLETED`, `REVIEW_FAILED`, `REVIEW_SUPERSEDED`; finding kinds `FINDING_RAISED`, `FINDING_RE_OBSERVED`, `FINDING_ANCHOR_UPDATED`, `FINDING_STATE_CHANGED`, `FINDING_ACKNOWLEDGED`, `FINDING_RESOLUTION_DETECTED`, `FINDING_STALE_DETECTED`; comment kinds `COMMENT_REPLY_RECEIVED`, `AGENT_REPLY_POSTED`.
- `publish_general(*, org_id, kind, payload)` — stamps `ts` (ISO UTC) server-side, builds `{kind, ts, **payload}`, publishes to the org's general channel.
- `publish_general_after_commit(session, *, org_id, kind, payload)` — stashes the event on `session.info`; an SQLAlchemy `after_commit` listener drains and publishes on commit. Rollback discards the stash silently — rolled-back transactions never emit SPA events.
- `subscribe_general(org_id)` — async iterator over general events for that org. Wraps `subscribe` on the org's channel.

**Shared:**

- `RedisPubsub` — class form for callers that want to construct their own bus (mostly tests).
- `get_pubsub()` — process-singleton accessor.
- `shutdown()` — closes the singleton and sets it to `None`; self-registered with both the web and worker shutdown registries at import time. Both processes host Redis subscriptions (the worker publishes; the web process subscribes), so both need cleanup.
- `reset_pubsub()` — drops the singleton synchronously; used by tests to isolate singleton state between runs without going through the async `shutdown()` path.

## Module architecture

### Key value objects

- `GeneralEventKind` — closed `StrEnum`; 15 values; the discriminator on the `{org_id}:general` wire payload.

### Channel naming

Two shapes:

- `activity:{workflow_execution_id}` — per-workflow activity events. Formed by `channel_for()`.
- `{org_id}:general` — org-scoped general events. Formed by the internal `_channel_for_general()` helper (not in `__all__`).

### After-commit stash mechanism

`publish_general_after_commit` appends `(org_id, kind, payload)` to a list stored under `session.info["yaaos_sse_general_pending"]`. A module-level SQLAlchemy `after_commit` listener (`@listens_for(Session, "after_commit")`) pops the list on commit and schedules each publish as an `asyncio.create_task` — the publish runs on the next loop tick. On rollback the list is never popped, discarding all stashed events. The module holds strong refs to in-flight tasks so asyncio GC doesn't collect them mid-fan-out.

### Persistence invariant

**Events are never persisted.** They exist only between publish and the subscriber's consumer loop. Reload-the-UI = empty until the next event.

### Backend

Layered on [`core/redis`](core_redis.md). This module owns channel naming + JSON encode/decode; `core/redis` owns connection management and the per-loop client cache. Client construction is lazy — importing the module or grabbing the singleton doesn't touch Redis.

## Data owned

None. The module is transport — Redis is the substrate.

## How it's tested

- `test/test_service.py` — round-trip: publish with no subscribers returns 0; fan-out delivers to every subscriber; subscriber bookkeeping balances on iterator exit; singleton identity. Uses the `redis_or_skip` fixture so local dev without Redis isn't blocked.
- `test/test_shutdown.py` — singleton lifecycle: `shutdown()` drops singleton; idempotent.
- `test/test_shutdown_service.py` — hook registration: `shutdown()` appears in both web and worker shutdown registries; draining either registry drops the singleton.
- `test/test_general_publish_service.py` — general-event pipeline: rollback discards stashed events; commit delivers them with correct `{kind, ts, ...payload}` shape; publishing on `org_B` does not reach `org_A`'s subscriber. Uses `db_session` + `redis_or_skip`.
