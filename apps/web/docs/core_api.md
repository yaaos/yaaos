# core/api

> Typed HTTP client + the full TanStack Query hook surface every domain module consumes.

## Purpose

A thin layer between the FastAPI backend and the UI. Owns the typed `openapi-fetch` client, a generic `apiFetch<T>` helper, TypeScript shapes for every API resource, and one TanStack Query hook per endpoint.

## Public interface

Re-exports from `@core/api`: client helpers (`apiClient`, `apiFetch`, `getCurrentOrgSlug`, `setCurrentOrgSlug`), one TypeScript type per backend resource, one TanStack Query hook per endpoint, and one mutation hook per write operation. See `apps/web/src/core/api/index.ts` for the full list.

## Module architecture

### Two clients, one helper

`client.ts` exposes two surfaces:
- `apiClient` — `openapi-fetch` typed client backed by the generated `paths` type from `generated/schema.d.ts`. Covers all endpoints in the schema; `/api/health` now carries a fully typed response.
- `apiFetch<T>(path, init?)` — generic fetch wrapper. On 401, lazy-imports `handleAuthFailure` (breaks load-path cycle) which hard-navigates to `/login?reason=...&next=<current-path>`. Non-2xx throws `${status} ${path}: ${body}`. 204 → `undefined`.

### Generated types

`src/core/api/generated/schema.d.ts` is auto-generated from the backend's `/openapi.json` by `apps/web/bin/gen-api-types`. It is committed; the generation script also supports a `--check` flag for drift detection (regenerates and runs `git diff --exit-code`).

- Only `core/api` may import from `generated/` — the boundary is enforced by `.dependency-cruiser.cjs`.
- The generated dir is excluded from Biome lint/format (`biome.json`).
- `client.ts` re-exports generated types under consumer-facing names (`HealthResponse`, `Lesson`, `AuditEntry`). A type alias keeps `ReviewJob` typed with a concrete `activity_log: ReviewJobActivityEvent[]` overlay (the JSONB column is `unknown[]` in the spec).
- The backend spec returns `unknown` for the notification and popover endpoints; those stay hand-typed in `queries.ts` until the spec is annotated.

### Central 401 handler

`auth-failure.ts` owns the one-and-only redirect-on-auth-died path:

- **Mutex** — a module-level `redirectInProgress` flag means concurrent 401s (every page often fires `/api/auth/me` + `/api/orgs/mine` + page queries in parallel) trigger exactly one `window.location.assign`. Hard nav rather than TanStack Router soft-nav clears React state + the query cache, which is the right thing when the session is dead.
- **Reason mapping** — backend `{"error": "<code>"}` body → UX banner reason: `session_idle_expired → "idle"`, `session_expired → "expired"`, `unauthenticated → "signed_out"`, unknown → `"signed_out"` (catch-all so renames don't break the banner).
- **`next` round-trip** — captures `window.location.pathname + search + hash` and tags it as `?next=`. `LoginPage` forwards it through the OAuth flow's `next` query param; backend `_safe_next` (and our mirroring `safeNext` helper) reject scheme-relative / off-origin paths and `/login` loops. The user lands back where they were trying to go after sign-in. Covers both "session died mid-flow" and "cold deeplink while logged out" identically.
- The backend already clears `yaaos_session` + `yaaos_csrf` via `Set-Cookie: Max-Age=0` on every 401 it issues (see [`apps/backend/app/core/auth/auth_failure.py`](../../backend/app/core/auth/auth_failure.py)), so by the time the redirect fires the browser already has fresh state.

### Resource types

`client.ts` owns the type surface. Types sourced from the generated schema:
- `HealthResponse` — alias of `components["schemas"]["HealthResponse"]`.
- `Lesson` — alias of `components["schemas"]["Lesson"]`.
- `AuditEntry` — alias of `components["schemas"]["AuditEntryView"]`.
- `ReviewJob` — generated base with `activity_log` overridden to `ReviewJobActivityEvent[]` (JSONB column is untyped in spec).

Hand-typed (no generated equivalent — backend endpoints return `unknown`):
- `Ticket` — `pr_number`, `author_login`, `is_draft` enriched from the linked PR at read-time. Needs `response_model` on the tickets endpoints.
- `Finding` — `severity: "must-fix" | "nit" | "suggestion" | "info"`; optional `rationale`, `snippet: FindingSnippetLine[]`, `applied_lesson_ids`, `source_agent`. Needs `response_model` on the findings endpoint.
- `ReviewJobActivityEvent` — `{ts, kind, message, detail?}`; used in `ReviewJob.activity_log` and as the SSE payload for `/api/sse/workspace_activity/{id}`. JSONB column, no spec annotation.
- `Notification` / `NotificationsPopover` — hand-typed in `queries.ts`; backend spec returns `unknown` for these endpoints.
- `PluginMeta` — from `/api/settings/plugins`; drives the Settings UI plugin list.

### Query hooks

`queries.ts` — one hook per endpoint. Server-state hooks use `useSuspenseQuery`; callers never see `isLoading` — loading is handled by `<Suspense>` fallbacks. Polling intervals are used only for `useHealth`, `useConfigStatus`, `useNotifications`, and similar non-SSE queries. `useDashboard` and `useAgents` are pure-SSE — no polling. See [core_sse.md](core_sse.md) for the full invalidation map.

`useAgents(orgSlug)` — fetches `GET /api/orgs/{slug}/agents`. Returns `AgentRow[]` within the 1-hour retention window. Invalidated live via `agent_liveness_changed` SSE. Enabled only when `orgSlug` is non-empty.

### Mutation hooks

Each mutation invalidates the query keys it affects on success. Key taxonomy: [patterns.md § Query keys](patterns.md#query-keys). Hook-to-endpoint mapping is in `queries.ts`.

## Data owned

None. The `QueryClient` lives in `main.tsx`; hooks here just read/write it.

## How it's tested

E2e specs in `apps/e2e/tests/*.spec.ts` cover full hook + backend round-trips. Non-trivial cache logic (custom `select`, optimistic updates) gets Vitest tests in `apps/web/src/core/api/test/`. Domain-level integration tests use MSW (see [patterns.md § MSW testing strategy](patterns.md#msw-testing-strategy)).
