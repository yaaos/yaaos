# core/sse

> Single browser-wide `EventSource`, keyed by the active org; translates server events into TanStack Query cache invalidations.

## Purpose

Owns the single browser-wide `EventSource` connecting to `/api/sse/general` and maps the `ticket_status_changed` event kind to query cache invalidations. Domain modules consume queries; `core/sse` makes those queries refresh. The workspace-activity stream is a separate hook (`useWorkflowActivityStream`) that connects to `/api/sse/workspace_activity/{id}`.

`/api/sse` is org-scoped, but the browser `EventSource` API cannot set the `X-Org-Slug` header. The org slug therefore rides in the `?org=<slug>` query param; the backend accepts it for `/api/sse` routes and runs it through the same membership check.

## Public interface

- `useServerEvents()` — React hook called once from the root `AppShell`. Attaches the current `QueryClient` and keeps the general stream pointed at the active org (read from the URL via `useCurrentOrgSlug`).
- `useWorkflowActivityStream(workflowExecutionId)` — React hook that opens a second `EventSource` to the per-workflow activity channel (with `?org=`) and yields `WorkflowActivityEvent` objects.
- `ServerEvent` — envelope type: `{ kind, source_module, ts, ticket_id, [extra]: unknown }`.

## Module architecture

### Mounting + org keying

`useServerEvents()` runs in `AppShell`, the root route component (always mounted, inside the router so it sees route changes). The `EventSource` is module-scoped, not owned by a `useEffect` — the effects only attach the `QueryClient` and report the current org slug. StrictMode double-mount and route remounts don't open extra connections. Exactly one connection per tab.

The connection is keyed by org slug: switching org closes the old stream and opens a new one for the new `?org=`; with no org in scope (`/login`, the `/orgs` picker) there is no stream.

### Event → invalidation map

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| anything else | silently ignored |

`ticket_id` on the envelope scopes invalidations. Events without it fall back to the global keys (`["tickets"]`, `["reviewer", "metrics"]`).

### Coalesced invalidations

Invalidations are deduped on a 200 ms trailing debounce keyed by `JSON.stringify(queryKey)`. A burst of N events that all target the same key triggers one `invalidateQueries` call. Drains the dashboard "boot flurry" where the reviewer pipeline emits several `ticket_status_changed` events in tight succession.

### Reconnection

Native `EventSource` auto-reconnects with exponential backoff; `onerror` is a no-op. The query client runs with `refetchOnWindowFocus: false` and no `refetchInterval`, so SSE is the only live-update path — reconnect reconciliation matters.

`onopen` reconciles on every (re)connect: it invalidates the list-level keys (`["tickets"]`, `["reviewer", "metrics"]`) so a refetch recovers anything created while the stream was not OPEN. The stream connects asynchronously and pub/sub has no replay, so an event published in that window is lost on the bus — the refetch reads it from persisted state instead. The server emits a connect prelude so `onopen` fires immediately rather than waiting for the first event (see [backend `core/sse`](../../backend/docs/core_sse.md)).

## Data owned

None. The `EventSource` is per-mount.

## How it's tested

End-to-end via `apps/e2e/tests/pr-review-end-to-end.spec.ts` ("review card state transitions live via SSE without reload") — lands on the tickets list first, then dispatches a webhook, and asserts the new ticket appears and the review posts via SSE-driven invalidations alone, no reload. Vitest coverage in `subscriber.test.tsx` mocks the global `EventSource` constructor to assert (a) no connection opens until an org is in scope and the URL carries `?org=`, (b) repeated attach/slug reports keep exactly one connection (StrictMode-safe), (c) changing org re-targets the stream and clearing it closes the stream, (d) bursts of events coalesce to a single `invalidateQueries` per key, and (e) `onopen` reconciles the list-level keys on (re)connect.
