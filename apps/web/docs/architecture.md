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
- Boundaries are enforced by `apps/web/.dependency-cruiser.cjs` at `severity: "error"` as a dedicated `bin/ci` step. A module's public surface is its `public/` directory — cross-module imports must target `<module>/public/**`; non-`public/` files are private. Only `core/api` may import from `core/api/generated/`. `shared/components/ui/` is excluded (managed vendor layer).

## Cross-cutting wiring

- **TanStack Query** — single `QueryClient` in `main.tsx`. Keys are module-scoped arrays; see [patterns.md § Query keys](patterns.md#query-keys). Hooks use `useSuspenseQuery`; callers wrap in `<Suspense>` + `<ErrorBoundary>` from `react-error-boundary`.
- **SSE** — `useServerEvents()` (`core/sse`) runs in the root `AppShell`. One org-keyed `EventSource` (`?org=<slug>`, since `EventSource` can't send `X-Yaaos-Org-Slug`) → `ticket_status_changed` → `invalidateQueries(...)`. Domain modules consume queries, not events. See [core_sse.md](core_sse.md).
- **Routing** — `src/router.tsx` (composition root) declares the full route tree; `core/routing` owns the search schemas. Domain modules export their page components. See [core_routing.md](core_routing.md).
- **Org slug** — derived from the URL on every read (`core/api/org-context.ts`). No module-global cache. `apiFetch` attaches `X-Yaaos-Org-Slug`; domain hooks stay org-agnostic at the call site.
- **Generated types** — `src/core/api/generated/schema.d.ts` is generated from the committed backend artifact `apps/backend/openapi/web-api.json` by `bin/gen-api-types` and committed. `/api/testing/*` paths are excluded upstream (stripped in the backend artifact). `bin/gen-api-types --check` (regenerate + compare against the committed file) runs as a gating stage in `bin/ci` before `tsc` — no running backend required. Only `core/api` imports the generated dir.
- **MSW test infra** — `src/test/msw/` holds the Node.js MSW server and per-domain handlers. Global lifecycle in `src/test-setup.ts`. Domain tests use real `QueryClient` + real `apiFetch` + MSW HTTP interception. See [patterns.md § MSW testing strategy](patterns.md#msw-testing-strategy).
- **Observability** — `core/observability` initializes the OTel Web SDK at boot (`configure()` in `main.tsx`). `FetchInstrumentation` injects `traceparent` on same-origin `/api/` fetches only (cross-origin fetches never receive it) so browser spans continue as children of the backend trace. Render errors flow through `react-error-boundary` → `recordException` → span exception events (not console.error). See [core_observability.md](core_observability.md).

## Key flows

**SSE → re-render** (crosses core/sse → core/api → domain):
`EventSource message` → `useServerEvents` maps `kind` → `qc.invalidateQueries(key)` → TanStack Query refetch → domain component re-renders.

**Auth / tenancy gate** (crosses core/routing → core/api → shared):
Route `beforeLoad` hits `/api/auth/me` → 401 triggers `handleAuthFailure` in `core/api` → hard-nav to `/login?reason=…&next=…`. On success, `/orgs/$slug/...` parent route writes slug to `org-context.ts`; `AppShell` renders sidebar + outlet only for non-standalone paths. Role gates (`RequireMembership`) are UI hints; backend `require(action)` is the authority.

**Distributed trace join** (crosses core/observability → core/api → backend → agent):
`UserInteractionInstrumentation` opens a span on click/submit → `FetchInstrumentation` injects `W3C traceparent` header on the `/api/*` fetch → backend's `FastAPIInstrumentor` continues the same trace as a child span, stamping its own `yaaos.org_id`/`yaaos.user_id` authoritatively → agent span appended to the same trace. Browser client exports its side via OTLP/HTTP to Dash0 (triple-gated: endpoint + auth token + dataset). Render errors: `ErrorBoundary` calls `recordException` on the active user-interaction span (or opens a short-lived fallback span). `traceparent` is the only cross-wire trace context — no baggage header is ever emitted.
