# core/sse

> Single browser-wide `EventSource`, keyed by the active org; translates server events into TanStack Query cache invalidations and exposes connection state via a tear-free store.

## Purpose

Owns the single browser-wide `EventSource` connecting to `/api/sse/general` and maps event kinds to query cache invalidations. Domain modules consume queries; `core/sse` makes those queries refresh. The workspace-activity stream is a separate hook (`useWorkflowActivityStream`) that connects to `/api/sse/workspace_activity/{id}`.

`/api/sse` is org-scoped, but the browser `EventSource` API cannot set the `X-Org-Slug` header. The org slug therefore rides in the `?org=<slug>` query param; the backend accepts it for `/api/sse` routes and runs it through the same membership check.

## Public interface

- `useServerEvents()` — React hook called once from the root `AppShell`. Attaches the current `QueryClient` and keeps the general stream pointed at the active org (read from the URL via `useCurrentOrgSlug`).
- `useConnectionStatus()` — hook returning `ConnectionStatus` (`"idle" | "connecting" | "connected" | "disconnected"`). Backed by `useSyncExternalStore`; safe under concurrent React.
- `useSSESnapshot()` — hook returning the full `SSESnapshot` (`{ status, lastEvent }`). Same store backing; use when both fields are needed.
- `useWorkflowActivityStream(workflowExecutionId)` — React hook that opens a second `EventSource` to the per-workflow activity channel (with `?org=`) and yields `WorkflowActivityEvent` objects.
- `ServerEvent` — envelope type: `{ kind, source_module, ts, ticket_id, [extra]: unknown }`.
- `ConnectionStatus` — `"idle" | "connecting" | "connected" | "disconnected"`.
- `SSESnapshot` — `{ status: ConnectionStatus, lastEvent: ServerEvent | null }`.

## Module architecture

### Mounting + org keying

`useServerEvents()` runs in `AppShell`, the root route component (always mounted, inside the router so it sees route changes). The `EventSource` is module-scoped, not owned by a `useEffect` — the effects only attach the `QueryClient` and report the current org slug. StrictMode double-mount and route remounts don't open extra connections. Exactly one connection per tab.

The connection is keyed by org slug: switching org closes the old stream and opens a new one for the new `?org=`; with no org in scope (`/login`, the `/orgs` picker) there is no stream.

### Module-scope store

`subscriber.tsx` maintains a module-scope store with two operations:
- `subscribe(listener)` — registers a listener; returns an unsubscribe function. Used as the first argument to `useSyncExternalStore`.
- `getSnapshot()` — returns the current `SSESnapshot`. Referentially stable (same object) while state has not changed; replaced (new object) on every status transition or post-debounce event flush. `useSyncExternalStore` uses `Object.is` to bail out of re-renders, so stability prevents render loops.

Status transitions:
- `idle` — no org in scope.
- `connecting` — `EventSource` constructed, `onopen` not yet fired.
- `connected` — `onopen` fired.
- `disconnected` — `onerror` fired (EventSource auto-reconnects; status returns to `connected` on next `onopen`).

`lastEvent` is updated once per debounce flush, co-located with the `invalidateQueries` flush.

### Event → invalidation map

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| `review_requested` / `review_started` / `review_completed` / `review_failed` / `review_superseded` | `["tickets"]`, `["tickets", "dashboard"]` |
| `finding_raised` / `finding_re_observed` / `finding_anchor_updated` / `finding_state_changed` / `finding_acknowledged` / `finding_resolution_detected` / `finding_stale_detected` | `["tickets"]`, `["tickets", "dashboard"]` |
| `agent_liveness_changed` | `["agents"]` |
| anything else | silently ignored |

`ticket_id` on the envelope scopes invalidations. Events without it fall back to the global keys (`["tickets"]`, `["reviewer", "metrics"]`). `onopen` reconciles by invalidating `["tickets"]`, `["reviewer", "metrics"]`, and `["agents"]`.

### Coalesced invalidations

Invalidations are deduped on a 200 ms trailing debounce keyed by `JSON.stringify(queryKey)`. A burst of N events that all target the same key triggers one `invalidateQueries` call. `lastEvent` commits on the same 200 ms cadence. Drains the dashboard "boot flurry" where the reviewer pipeline emits several `ticket_status_changed` events in tight succession.

### Reconnection

Native `EventSource` auto-reconnects with exponential backoff; `onerror` transitions status to `disconnected` but does not close the stream. The query client runs with `refetchOnWindowFocus: false` and no `refetchInterval`, so SSE is the only live-update path — reconnect reconciliation matters.

`onopen` reconciles on every (re)connect: it invalidates the list-level keys (`["tickets"]`, `["reviewer", "metrics"]`) so a refetch recovers anything created while the stream was not OPEN. The stream connects asynchronously and pub/sub has no replay, so an event published in that window is lost on the bus — the refetch reads it from persisted state instead. The server emits a connect prelude so `onopen` fires immediately rather than waiting for the first event (see [backend `core/sse`](../../backend/docs/core_sse.md)).

## Data owned

None. The `EventSource` is per-mount.

## How it's tested

End-to-end via `apps/e2e/tests/pr-review-end-to-end.spec.ts` ("review card state transitions live via SSE without reload") — lands on the tickets list first, then dispatches a webhook, and asserts the new ticket appears and the review posts via SSE-driven invalidations alone, no reload. Vitest coverage in `core/sse/test/`:
- `subscriber.test.tsx` — connection lifecycle: no connection without org, StrictMode safety, org retarget, org clear, burst coalescing, `onopen` reconcile, unparseable JSON.
- `store.test.ts` — store contract: `getSnapshot` referential stability, status transitions (`idle`/`connecting`/`connected`/`disconnected`), `subscribe` listener fire + unsubscribe, debounce coalescing, slug-change reconnect.
