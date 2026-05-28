# core/sse

> Redis-backed pub/sub for org-scoped general events and workspace-activity streams.

## Purpose

Two pipelines in one module:

- **General-event pipeline** — org-scoped typed events consumed by the SPA's live-update stream. Channel shape: `{org_id}:general`. Uses `GeneralEventKind` as the discriminator. `publish_general_after_commit` ties publish lifetime to a transaction — rollbacks silently discard stashed events so rolled-back transactions never emit SPA events.
- **Workspace-activity pipeline** — per-org per-workflow activity stream with channel isolation by both org and workflow execution. Channel shape: `{org_id}:workspace_activity:{workflow_execution_id}`. Raw agent event dict passed through unchanged — no envelope, no `ts` stamping.

Both pipelines are backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process reaches an SSE subscriber attached to a different web process. Fire-and-forget per Redis semantics — slow consumers do not backpressure publishers, and no event persistence.

The `/api/sse` prefix is declared as `ORG_SCOPED` in `core/auth/types.py` so all routes mounted at `core/sse/web.py` are enforced without additional classification work.

## Public interface

**HTTP routes (via `app/core/sse/web.py`):**

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/sse/general` | `ORG_READ` — org-scoped general event stream for the caller's resolved org. |
| GET | `/api/sse/workspace_activity/{workflow_execution_id}` | `ORG_READ` + workflow-in-org ownership check (404 on cross-org). |

Each frame is `data: <json>\n\n`. The general route carries `GeneralEventKind`-typed payloads; the workspace_activity route carries the raw agent event dict unchanged. Cross-org isolation is enforced by the per-org Redis channel shape — subscribers only receive events published to their org's channel. Closes when the client disconnects.

The workspace_activity route adds an ownership check via the `register_workspace_activity_ownership_check` registrar; the app bootstrap wires `domain/orgs.assert_workflow_in_org` into it. The registrar keeps `core/sse` from importing `domain/*`.

**Python symbols exported from `app/core/sse/__init__.py`:**

**General-event pipeline:**

- `GeneralEventKind` — closed `StrEnum` with 15 members: `TICKET_STATUS_CHANGED`; reviewer aggregate kinds `REVIEW_REQUESTED`, `REVIEW_STARTED`, `REVIEW_COMPLETED`, `REVIEW_FAILED`, `REVIEW_SUPERSEDED`; finding kinds `FINDING_RAISED`, `FINDING_RE_OBSERVED`, `FINDING_ANCHOR_UPDATED`, `FINDING_STATE_CHANGED`, `FINDING_ACKNOWLEDGED`, `FINDING_RESOLUTION_DETECTED`, `FINDING_STALE_DETECTED`; comment kinds `COMMENT_REPLY_RECEIVED`, `AGENT_REPLY_POSTED`.
- `publish_general(*, org_id, kind, payload)` — stamps `ts` (ISO UTC) server-side, builds `{kind, ts, **payload}`, publishes to the org's general channel.
- `publish_general_after_commit(session, *, org_id, kind, payload)` — stashes the event on `session.info`; an SQLAlchemy `after_commit` listener drains and publishes on commit. Rollback discards the stash silently — rolled-back transactions never emit SPA events.
- `subscribe_general(org_id)` — async iterator over general events for that org.

**Workspace-activity pipeline:**

- `publish_workspace_activity(*, org_id, workflow_execution_id, payload)` — publishes `payload` unchanged (no envelope, no `ts` stamping) to the org+workflow channel.
- `subscribe_workspace_activity(org_id, workflow_execution_id)` — async iterator over workspace-activity events for that org + workflow execution. Isolated by both dimensions — cross-org and cross-wfx events do not leak.

**Shared:**

- `register_workspace_activity_ownership_check(check)` — boot-time registrar for the workflow-in-org ownership dep used by the workspace_activity route. Idempotent for the same callable; raises on conflicting double-registration. The FastAPI `Depends` thunk lives in `core/sse/web` so `core/sse/service` stays framework-agnostic.
- `reset_workspace_activity_ownership_check()` — drops the registered ownership check; used by tests that wire the check themselves without going through `app.web`.
- `serialize_for_sse(payload)` — formats a `dict[str, Any]` as an HTTP `text/event-stream` data frame (`data: <json>\n\n`). Both general and workspace-activity subscribers use this before writing to the HTTP response.
- `subscriber_count(channel)` — diagnostic; **local-process** subscriber count (Redis's `PUBSUB NUMSUB` is cluster-wide and not what callers want).
- `RedisPubsub` — class form for callers that want to construct their own bus (mostly tests).
- `get_pubsub()` — process-singleton accessor.
- `shutdown()` — closes the singleton and sets it to `None`; self-registered with both the web and worker shutdown registries at import time. Both processes host Redis subscriptions (the worker publishes; the web process subscribes), so both need cleanup.
- `reset_pubsub()` — drops the singleton synchronously; used by tests to isolate singleton state between runs without going through the async `shutdown()` path.

## Module architecture

### Key value objects

- `GeneralEventKind` — closed `StrEnum`; 15 values; the discriminator on the `{org_id}:general` wire payload.

### Channel naming

Two shapes:

- `{org_id}:general` — org-scoped general events. Formed by the internal `_channel_for_general()` helper (not in `__all__`).
- `{org_id}:workspace_activity:{workflow_execution_id}` — per-org per-workflow workspace-activity events. Formed by the internal `_channel_for_workspace_activity()` helper (not in `__all__`). Dual-dimension isolation ensures cross-org and cross-wfx events never mix.

### After-commit stash mechanism

`publish_general_after_commit` appends `(org_id, kind, payload)` to a list stored under `session.info["yaaos_sse_general_pending"]`. A module-level SQLAlchemy `after_commit` listener (`@listens_for(Session, "after_commit")`) pops the list on commit and schedules each publish as an `asyncio.create_task` — the publish runs on the next loop tick. On rollback the list is never popped, discarding all stashed events. The module holds strong refs to in-flight tasks so asyncio GC doesn't collect them mid-fan-out.

### Persistence invariant

**Events are never persisted.** They exist only between publish and the subscriber's consumer loop. Reload-the-UI = empty until the next event.

### Backend

Layered on [`core/redis`](core_redis.md). This module owns channel naming + JSON encode/decode; `core/redis` owns connection management and the per-loop client cache. Client construction is lazy — importing the module or grabbing the singleton doesn't touch Redis.

## Data owned

None. The module is transport — Redis is the substrate.

## How it's tested

- `test/test_service.py` — `RedisPubsub` round-trip via `get_pubsub()`: publish with no subscribers returns 0; fan-out delivers to every subscriber; subscriber bookkeeping balances on iterator exit; singleton identity. Uses the `redis_or_skip` fixture so local dev without Redis isn't blocked.
- `test/test_shutdown.py` — singleton lifecycle: `shutdown()` drops singleton; idempotent.
- `test/test_shutdown_service.py` — hook registration: `shutdown()` appears in both web and worker shutdown registries; draining either registry drops the singleton.
- `test/test_general_publish_service.py` — general-event pipeline: rollback discards stashed events; commit delivers them with correct `{kind, ts, ...payload}` shape; publishing on `org_B` does not reach `org_A`'s subscriber. Uses `db_session` + `redis_or_skip`.
- `test/test_workspace_activity_publish_service.py` — workspace-activity pipeline: cross-org isolation (org_B publish does not reach org_A subscriber on same wfx); cross-wfx isolation (wfx_2 publish does not reach wfx_1 subscriber in same org). Uses `redis_or_skip`.
- `test/test_serialize_for_sse_service.py` — `serialize_for_sse` formats `dict` payload as `data: <json>\n\n`.
- `test/test_general_endpoint_service.py` — HTTP auth gate on `GET /api/sse/general` (401/400/403); cross-org isolation on `_general_stream` directly (httpx-ASGITransport hangs on close for infinite streams — the HTTP wrapper has no logic beyond auth, so the generator is the right test target). Uses `db_session` + `redis_or_skip` for the streaming test; the auth tests use only `db_session`.
- `test/test_workspace_activity_endpoint_service.py` — cross-org 404 on `GET /api/sse/workspace_activity/{id}` (via the registered ownership check); happy-path streaming via `_workspace_activity_stream` (same httpx-ASGI constraint as the general endpoint).
