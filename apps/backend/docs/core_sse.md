# core/sse

> Redis-backed pub/sub for org-scoped general events and workspace-activity streams.

## Scope

- **Owns:** two pub/sub pipelines, channel naming, event shapes (`GeneralEventKind`), `serialize_for_sse`, the connect prelude (`sse_prelude`), the process-wide shutdown event + graceful-close frame.
- **Does not own:** the pub/sub transport, JSON encode/decode, or singleton lifecycle — all in [`core/redis`](core_redis.md); event persistence (none — Redis only); domain event definitions; auth enforcement (delegated to `core/auth` via `ORG_SCOPED` classification).
- **Boundary:** publishes land on the `core/redis` JSON bus; subscribers consume via async iterators; no backpressure — slow consumers do not block publishers; no event persistence.

## Why / invariants

- **`publish_general_after_commit` ties publish lifetime to a transaction** — events are stashed on `session.info`; an `after_commit` listener drains and dispatches them fire-and-forget via `spawn("sse.publish_general", …)`. Rollback discards silently — rolled-back transactions never emit SPA events. The `spawn()` call propagates the calling span's trace context so the publish appears as a `spawn:sse.publish_general` child span in the calling request's trace.
- **Channel isolation by org (+ run for activity) is the boundary.** Cross-org and cross-run events cannot leak by construction. Shapes: `{org_id}:general`, `{org_id}:workspace_activity:{run_id}`. A caller requesting another org's run subscribes to `{caller_org}:…:{run_id_other}` — a channel nobody publishes to — so the stream is empty rather than 404.
- **Every stream yields `sse_prelude()` (a `: comment` frame) before its first event.** This flushes response headers so the client's `EventSource` fires `onopen` immediately; otherwise a stream blocked waiting for its first event never flushes, and a client that missed the triggering event (pub/sub has no replay) never learns it is connected. The prelude precedes Redis subscription, so a publish racing the connect can still be missed on the bus — the client reconciles via a full refetch on `onopen` (see [web `core/sse`](../../web/docs/core_sse.md)).
- **`shutdown()` closes all active streams before the process exits.** Both stream generators race each `__anext__` against a module-level `asyncio.Event` set lazily at first access. The event is installed as a fresh instance per test by the `sse_shutdown_event_isolation` autouse fixture (via `set_shutdown_event_for_tests()`). When `shutdown()` sets the event, every waiting generator emits a final `retry: 1000\n: server closing\n\n` frame (instructs the browser to reconnect in ~1 s) and returns — the `StreamingResponse` completes cleanly instead of hanging on a dead socket until the browser's TCP timeout. Registered with the web shutdown registry only (SSE is web-presence).

## Gotchas

- **The bus lives in `core/redis`** — `publish`/`subscribe`/`subscriber_count` are imported from there. This module only names channels and shapes events.
- **Workspace-activity events are passed through unchanged** — no envelope, no `ts` stamping (unlike the general pipeline).
- **`/api/sse` prefix is `ORG_SCOPED`** in `core/auth/types.py` — all routes under `web.py` are auth-enforced without extra work.

## Data owned

None. Transport only — Redis is the substrate.

## `GeneralEventKind` values

- `ticket_status_changed` — ticket run-state transition
- `review_requested`, `review_started`, `review_completed`, `review_failed`, `review_superseded` — review job lifecycle
- `finding_raised`, `finding_re_observed`, `finding_anchor_updated`, `finding_state_changed`, `finding_acknowledged`, `finding_resolution_detected`, `finding_stale_detected` — finding lifecycle
- `comment_reply_received`, `agent_reply_posted` — conversation replies
- `agent_changed` — workspace-agent state change (liveness transition, heartbeat, or lifecycle flip); payload carries `{agent_id}`; org-scoped
- `run_state_changed` — `pipeline_runs` row state transition; payload carries `{ticket_id, run_id, state}`; published by [`domain/pipelines`](domain_pipelines.md) at every run-state write (promotion to `running`, and every terminal)
- `stage_state_changed` — a `stage_executions` row reached a terminal status (`completed`/`failed`); payload carries `{ticket_id, run_id}`; published by [`domain/pipelines`](domain_pipelines.md)
- `artifact_stored` — a new `artifacts` row was written; payload carries `{ticket_id}`; published by [`domain/pipelines`](domain_pipelines.md) after `domain/artifacts.store`

## How it's tested

`test/test_general_publish_service.py` — rollback discards events; commit delivers with correct shape; org isolation. Uses `db_session` + `redis_or_skip`.
`test/test_sse_publish_spawn_span.py` — after a run state transition the `spawn:sse.publish_general` span shares the calling span's trace_id.
`test/test_workspace_activity_publish_service.py` — cross-org and cross-wfx isolation.
`test/test_general_endpoint_service.py` — HTTP auth gate (401/400/403); connect prelude is the first frame; cross-org isolation on `_general_stream` directly.
`test/test_workspace_activity_endpoint_service.py` — non-owned wfx yields empty stream (channel-key isolation); happy-path streaming via `_workspace_activity_stream`.
`test/test_serialize_for_sse_service.py` — `data: <json>\n\n` shape.
`test/test_shutdown_service.py` — `shutdown()` on a live general stream and a live workspace-activity stream each emit the final frame and raise `StopAsyncIteration`; idle shutdown doesn't raise. Test isolation is structural: the `sse_shutdown_event_isolation` autouse fixture binds a fresh event per test.

The pub/sub transport itself (round-trip, fan-out, subscriber bookkeeping, singleton lifecycle, shutdown) is tested in [`core/redis`](core_redis.md).
