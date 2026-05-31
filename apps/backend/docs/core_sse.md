# core/sse

> Redis-backed pub/sub for org-scoped general events and workspace-activity streams.

## Scope

- **Owns:** two pub/sub pipelines, channel naming, event shapes (`GeneralEventKind`), `serialize_for_sse`, the connect prelude (`sse_prelude`).
- **Does not own:** the pub/sub transport, JSON encode/decode, or singleton lifecycle — all in [`core/redis`](core_redis.md); event persistence (none — Redis only); domain event definitions; auth enforcement (delegated to `core/auth` via `ORG_SCOPED` classification).
- **Boundary:** publishes land on the `core/redis` JSON bus; subscribers consume via async iterators; no backpressure — slow consumers do not block publishers; no event persistence.

## Why / invariants

- **`publish_general_after_commit` ties publish lifetime to a transaction** — events are stashed on `session.info`; an `after_commit` listener drains and schedules them as `asyncio.create_task`. Rollback discards silently — rolled-back transactions never emit SPA events.
- **Channel isolation by org (+ workflow execution for activity) is the boundary.** Cross-org and cross-wfx events cannot leak by construction. Shapes: `{org_id}:general`, `{org_id}:workspace_activity:{workflow_execution_id}`. A caller requesting another org's wfx subscribes to `{caller_org}:…:{wfx_other}` — a channel nobody publishes to — so the stream is empty rather than 404.
- **Every stream yields `sse_prelude()` (a `: comment` frame) before its first event.** This flushes response headers so the client's `EventSource` fires `onopen` immediately; otherwise a stream blocked waiting for its first event never flushes, and a client that missed the triggering event (pub/sub has no replay) never learns it is connected. The prelude precedes Redis subscription, so a publish racing the connect can still be missed on the bus — the client reconciles via a full refetch on `onopen` (see [web `core/sse`](../../web/docs/core_sse.md)).

## Gotchas

- **The bus lives in `core/redis`** — `publish`/`subscribe`/`subscriber_count` are imported from there. This module only names channels and shapes events.
- **Workspace-activity events are passed through unchanged** — no envelope, no `ts` stamping (unlike the general pipeline).
- **`/api/sse` prefix is `ORG_SCOPED`** in `core/auth/types.py` — all routes under `web.py` are auth-enforced without extra work.

## Data owned

None. Transport only — Redis is the substrate.

## How it's tested

`test/test_general_publish_service.py` — rollback discards events; commit delivers with correct shape; org isolation. Uses `db_session` + `redis_or_skip`.
`test/test_workspace_activity_publish_service.py` — cross-org and cross-wfx isolation.
`test/test_general_endpoint_service.py` — HTTP auth gate (401/400/403); connect prelude is the first frame; cross-org isolation on `_general_stream` directly.
`test/test_workspace_activity_endpoint_service.py` — non-owned wfx yields empty stream (channel-key isolation); happy-path streaming via `_workspace_activity_stream`.
`test/test_serialize_for_sse_service.py` — `data: <json>\n\n` shape.

The pub/sub transport itself (round-trip, fan-out, subscriber bookkeeping, singleton lifecycle, shutdown) is tested in [`core/redis`](core_redis.md).
