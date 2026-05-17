# Frontend patterns

Cross-app conventions (UTC on the wire, audit-log shape) live in [`docs/system-architecture.md`](../../../docs/system-architecture.md).

## Module documentation

Every shipped module has one `apps/web/docs/<layer>_<module>.md` following this fixed template, in order:

1. **Purpose** — one paragraph. What the module owns; what it does not.
2. **Public interface** — what's exported from `index.ts(x)` (components, hooks, queries). No internals.
3. **Module architecture** — the internal shape, in this order:
   - **Entities** — domain concepts owned by this module (usually views over backend data). One bullet each.
   - **Key value objects** — load-bearing types / props shapes. One bullet, one sentence each.
   - **Core user flows** — short numbered steps for the main ways the user exercises this module. Prose; no code.
   - **State machines** — if any. States as bullets, transitions as `from → to` arrow notation.
4. **Data owned** — query keys, client-side caches, local component state worth noting.
5. **How it's tested** — e2e coverage (no FE unit test discipline today; see [README](README.md)).

Discipline still applies: terse, bullets, no code snippets, no `Decisions` section, link don't repeat. Modules with no state machines just omit that sub-section.

## Dumb frontend

The SPA renders data and dispatches actions. It owns no rules yaaos's backend doesn't also enforce.

- **Forms** — FE validations exist for input immediacy; the backend re-validates and returns 4xx with field-keyed errors that surface inline. No FE rule the backend doesn't also have.
- **Verdicts / status / counts** — server-supplied. Never derived client-side.
- **Permissions** — show/hide based on server-supplied capability flags. M01 has no auth; the shape is in place.
- **Cache invalidation** — driven by mutation responses and SSE events. No "I bet this is stale" client heuristics.
- **Client-side filter/sort** — fine for UX over an already-fetched list. Anything that changes which rows the user *acts on* (bulk-delete, bulk-export) goes through the API.

If a FE change could alter what gets stored, posted, or counted without a corresponding API change, the logic is in the wrong place.

## Query keys

Every TanStack Query key is a module-scoped array. Canonical keys:

- `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`
- `["reviewer", "jobs", ticket_id]`, `["reviewer", "metrics"]`, `["reviewer", "agents"]`
- `["memory", repo]`
- `["github", "installation"]`, `["github", "repositories"]`
- `["plugin-health", pluginId]`
- `["onboarding"]`, `["health"]`

Mutations invalidate exactly the keys they affect; the SSE subscriber does the same (see [core_sse.md](core_sse.md)).

## Time and dates

- Backend emits ISO-8601 UTC (`Z` or `+00:00`).
- FE renders in the browser's local timezone via helpers in `apps/web/src/shared/utils/ago.ts`:
  - `ago(ts)` — relative duration (e.g., `"12s ago"`).
  - `formatTime(ts)` — local `HH:MM:SS`. Used for audit-log rows.
  - `formatDateTime(ts)` — full local date + time.
- **Anti-pattern:** `new Date(ts).toISOString()` — always UTC; never use for display. Use the helpers above.

`Intl.DateTimeFormat` reads the browser's OS timezone automatically — there is no central knob.

## API client

`core/api/client.ts` exposes:
- `apiClient` — `openapi-fetch` typed client (currently only `/api/health`).
- `apiFetch<T>(path, init?)` — generic fetch helper. Throws on non-2xx with status + body excerpt.

Every query/mutation hook wraps one of those. See [core_api.md](core_api.md) for the surface.

## Error handling at the API boundary

- **Mutations** — `useMutation` hooks expose `isPending` / `isSuccess` / `isError`; forms show inline "Saving…" / "Saved." / red error text. No global toast yet.
- **Queries** — components handle loading + error inline. Primitives expose `data-testid` slots so e2e can assert state.
- **Validation errors** — backend returns 4xx with a field-keyed error map; the form surfaces the message under the relevant input.

## Component primitives

`shared/components/` contains hand-rolled primitives over Tailwind: `Button`, `Card`/`CardHeader`/`CardContent`, `Badge`, `Dialog`/`DialogHeader`/`DialogBody`/`DialogFooter`. shadcn/ui isn't installed. Every interactive primitive accepts `data-testid` as a passthrough.

## SSE — single subscription at app root

One `EventSource` per browser tab, mounted via `<SSESubscriber>` in `main.tsx`. The subscriber translates each event's `kind` into `qc.invalidateQueries(...)` calls. Domain modules consume *queries*, not events.

Consequences:
- A new event kind adds one `case` to `core/sse/subscriber.tsx`, usually zero changes elsewhere.
- A domain module wanting live updates just uses the right query key.

Polling intervals (3-5s) on the underlying queries are a safety net for missed messages.

## Code style

- Function components only. No class components.
- Hooks for shared logic. No HOCs unless a library forces it.
- TanStack Query for server state. No `useEffect(() => fetch(...))`.
- React state for component-local state. Zustand listed but unused.
- Tailwind only. Color tokens are oklch, in `core/layout/theme.ts`.
- Type-strict; `tsc` warnings are CI errors.

## Imports

Absolute only via path aliases (`@core/...`, `@domain/...`, `@shared/...`). Only what's exported from `index.ts(x)`.

## Forms

React state + manual validation. No `react-hook-form` / `zod`.
