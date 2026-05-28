# core/sse

> Single `EventSource` mounted at app root; translates server events into TanStack Query cache invalidations.

## Purpose

Owns the single browser-wide `EventSource` connecting to `/api/sse/general` and maps the `ticket_status_changed` event kind to query cache invalidations. Domain modules consume queries; `core/sse` makes those queries refresh. The workspace-activity stream is a separate hook (`useWorkflowActivityStream`) that connects to `/api/sse/workspace_activity/{id}`.

## Public interface

- `<SSESubscriber>` — React component mounted once in `main.tsx` between `QueryClientProvider` and `RouterProvider`. Renders `children` through; the work is a side effect inside a `useEffect`.
- `useWorkflowActivityStream(workflowExecutionId)` — React hook that opens a second `EventSource` to the per-workflow activity channel and yields `ReviewJobActivityEvent` objects.
- `ServerEvent` — envelope type: `{ kind, source_module, ts, ticket_id, [extra]: unknown }`.

## Module architecture

### Mounting

`<SSESubscriber>` wraps the router in `main.tsx`. The `EventSource` is module-scoped, not inside `useEffect` — the effect only attaches the `QueryClient`. StrictMode double-mount doesn't open extra connections. Exactly one connection per tab.

### Event → invalidation map

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| anything else | silently ignored |

`ticket_id` on the envelope scopes invalidations. Events without it fall back to the global keys (`["tickets"]`, `["reviewer", "metrics"]`).

### Coalesced invalidations

Invalidations are deduped on a 200 ms trailing debounce keyed by `JSON.stringify(queryKey)`. A burst of N events that all target the same key triggers one `invalidateQueries` call. Drains the dashboard "boot flurry" where the reviewer pipeline emits several `ticket_status_changed` events in tight succession.

### Reconnection

Native `EventSource` auto-reconnects with exponential backoff. `onerror` is a logger. TanStack Query's mount + window-focus refetch resyncs after a long disconnect; `refetchInterval` queries cover continuous drift.

## Data owned

None. The `EventSource` is per-mount.

## How it's tested

End-to-end via `apps/e2e/tests/sse-step-progress-live.spec.ts` — dispatches a webhook, opens the ticket detail page without refreshing, asserts the review card transitions to `posted` via SSE-driven invalidations alone. Vitest coverage in `subscriber.test.tsx` mocks the global `EventSource` constructor to assert (a) one connection survives StrictMode double-mount and (b) bursts of events coalesce to a single `invalidateQueries` per key.
