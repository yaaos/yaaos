# Frontend architecture

> Internal layer model, cross-cutting wiring, and key flows for the yaaos SPA.

## Layers

Three groupings under `apps/web/src/`:

| Layer | Path | Purpose | May import |
|---|---|---|---|
| `core` | `src/core/` | Routing, layout, API client, SSE, observability. No feature UI. | `shared` |
| `domain` | `src/domain/<feature>/` | One folder per feature surface — pages, components, hooks. | `core`, `shared` |
| `shared` | `src/shared/` | Reusable primitives — components, hooks, utilities, types. | — |

- Cross-domain imports are forbidden; any primitive two domains share moves to `shared/`.
- `core` cannot import from `domain` (mirrors the backend's one-way dependency direction).
- Boundaries are enforced by Biome rules + manual review; there is no `tach` equivalent.

## Cross-cutting wiring

- **TanStack Query** — single `QueryClient` in `main.tsx`. Keys are module-scoped arrays; see [patterns.md § Query keys](patterns.md#query-keys).
- **SSE** — single `<SSESubscriber>` (`core/sse`) wraps the router in `main.tsx`. One `EventSource` → `ticket_status_changed` → `invalidateQueries(...)`. Domain modules consume queries, not events. See [core_sse.md](core_sse.md).
- **Routing** — `core/routing` declares the full route tree centrally; domain modules export their page components. See [core_routing.md](core_routing.md).
- **Org slug** — derived from the URL on every read (`core/api/org-context.ts`). No module-global cache. `apiFetch` attaches `X-Org-Slug`; domain hooks stay org-agnostic at the call site.

## Key flows

**SSE → re-render** (crosses core/sse → core/api → domain):
`EventSource message` → `SSESubscriber` maps `kind` → `qc.invalidateQueries(key)` → TanStack Query refetch → domain component re-renders.

**Auth / tenancy gate** (crosses core/routing → core/api → shared):
Route `beforeLoad` hits `/api/auth/me` → 401 triggers `handleAuthFailure` in `core/api` → hard-nav to `/login?reason=…&next=…`. On success, `/orgs/$slug/...` parent route writes slug to `org-context.ts`; `AppShell` renders sidebar + outlet only for non-standalone paths. Role gates (`RequireMembership`) are UI hints; backend `require(action)` is the authority.
