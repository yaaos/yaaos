# core/api

> Typed HTTP client + the full TanStack Query hook surface every domain module consumes.

## Purpose

A thin layer between the FastAPI backend and the UI. Owns a generic `apiFetch<T>` helper, TypeScript shapes for every API resource, and one TanStack Query hook per endpoint.

## Public interface

Files under `core/api/public/`, imported directly via `@core/api/public/<file>`:

- `public/client.ts` — the `apiFetch<T>` helper and one TypeScript type per backend resource.
- `public/queries.ts` — one TanStack Query hook per endpoint, one mutation hook per write operation.
- `public/org-context.ts` — `getCurrentOrgSlug`, `useCurrentOrgSlug`.
- `public/auth-failure.ts` — `AuthFailureReason` and the central 401 handler.
- `public/membership.ts` — the one role-gating primitive: `Role`, `ROLE_RANK` (builder<admin<owner), `resolveMembership`, `hasRole`, and the hooks `useMembership` / `useHasRole`. Lives in `core/api` so both `core/*` and `domain/*` may use it (core must not import domain). `RequireMembership` (domain/auth) wraps it for render-gating.

Private (non-`public/`): `generated/` (only `core/api` may import it).

## Module architecture

### The fetch helper

`apiFetch<T>(path, init?)` — generic fetch wrapper. On 401, lazy-imports `handleAuthFailure` (breaks load-path cycle) which hard-navigates to `/login?reason=...&next=<current-path>`. Non-2xx throws `${status} ${path}: ${body}`. 204 → `undefined`. A W3C `traceparent` header is injected automatically by the OTel `FetchInstrumentation` registered in `core/observability` — no explicit header code in `client.ts`. The backend's `FastAPIInstrumentor` reads this header and continues the distributed trace.

### Generated types

`src/core/api/generated/schema.d.ts` is auto-generated from the committed backend artifact `apps/backend/openapi/web-api.json` by `apps/web/bin/gen-api-types`. It is committed; `bin/gen-api-types --check` (regenerate + compare against the committed file) runs as a gating stage in `bin/ci` — no running backend required.

- Only `core/api` may import from `generated/` — the boundary is enforced by `.dependency-cruiser.cjs`.
- The generated dir is excluded from Biome lint/format (`biome.json`).
- `client.ts` re-exports generated types under consumer-facing names (`Lesson`). A type alias keeps `ReviewJob` typed with a concrete `activity_log: ReviewJobActivityEvent[]` overlay (the JSONB column is `unknown[]` in the spec).
- The backend spec returns `unknown` for the notification and popover endpoints; those stay hand-typed in `queries.ts` until the spec is annotated.

### Central 401 handler

`auth-failure.ts` owns the one-and-only redirect-on-auth-died path:

- **Mutex** — a module-level `redirectInProgress` flag means concurrent 401s (every page often fires `/api/auth/me` + `/api/orgs/mine` + page queries in parallel) trigger exactly one `window.location.assign`. Hard nav rather than TanStack Router soft-nav clears React state + the query cache, which is the right thing when the session is dead.
- **Reason mapping** — backend `{"error": "<code>"}` body → UX banner reason: `session_idle_expired → "idle"`, `session_expired → "expired"`, `unauthenticated → "signed_out"`, unknown → `"signed_out"` (catch-all so renames don't break the banner).
- **`next` round-trip** — captures `window.location.pathname + search + hash` and tags it as `?next=`. `LoginPage` forwards it through the OAuth flow's `next` query param; backend `_safe_next` (and our mirroring `safeNext` helper) reject scheme-relative / off-origin paths and `/login` loops. The user lands back where they were trying to go after sign-in. Covers both "session died mid-flow" and "cold deeplink while logged out" identically.
- The backend already clears `yaaos_session` + `yaaos_csrf` via `Set-Cookie: Max-Age=0` on every 401 it issues (see [`apps/backend/app/core/auth/auth_failure.py`](../../backend/app/core/auth/auth_failure.py)), so by the time the redirect fires the browser already has fresh state.

### Resource types

`client.ts` owns the type surface. Types sourced from the generated schema:
- `Lesson` — alias of `components["schemas"]["Lesson"]`.
- `ReviewJob` — generated base with `activity_log` overridden to `ReviewJobActivityEvent[]` (JSONB column is untyped in spec).

Hand-typed (no generated equivalent — backend endpoints return `unknown`):
- `Ticket` — `pr_number`, `author_login`, `is_draft` enriched from the linked PR at read-time. Needs `response_model` on the tickets endpoints.
- `ReviewJobActivityEvent` — `{ts, kind, message, detail?}`; used in `ReviewJob.activity_log` and as the SSE payload for `/api/sse/workspace_activity/{id}`. JSONB column, no spec annotation.
- `Notification` / `NotificationsPopover` — hand-typed in `queries.ts`; backend spec returns `unknown` for these endpoints.
- `PluginMeta` — from `/api/settings/plugins`; drives the Settings UI plugin list.

### Query hooks

`queries.ts` — one hook per endpoint. All data-display hooks use `useSuspenseQuery`; callers never see `isLoading` — loading is handled by `<Suspense>` fallbacks. This covers every hook that powers a page or section: `useCurrentUser`, `useTickets`, `useTicket`, `useLessons`, `useNotifications`, `useDashboard`, `useAgents`, `useFindingsForTicket`, `useReviewJobsForTicket`, `useHitlHistory`, `useMyOrgs`, `useGithubInstallation`, `useGithubRepositories`, `useAvailablePlugins`. `useLessons` accepts a `LessonsFilter` object only (no string shorthand). `useAvailablePlugins(type)` fetches `GET /api/plugins/available?type=...` and returns `PluginMeta[]`; consumed by the VCS and Coding Agents settings pages. The polling-based utility hook `useConfigStatus` stays a regular `useQuery` — it powers ambient chrome (the onboarding gate), not data pages. See [core_sse.md](core_sse.md) for the full invalidation map.

Auth hooks live in `queries.ts` so all layers can call them without importing from `domain/auth`:
- `currentUserQueryOptions` — exported `queryOptions` object for `["auth","me"]`; use this to subscribe to or seed the cache without triggering a fetch (e.g. `useQuery({ ...currentUserQueryOptions, enabled: false })` in `useOtelIdentitySync`).
- `useCurrentUser()` — `GET /api/auth/me`; returns `CurrentUser | null`; delegates to `currentUserQueryOptions`.
- `useLogout()` — mutation, single-session sign-out.
- `useLogoutAll()` — mutation, all-sessions sign-out.
`domain/auth/queries.ts` re-exports these for backward compatibility within the auth domain.

`useAgents(orgSlug)` — fetches `GET /api/orgs/{slug}/agents`. Returns `AgentRow[]` within the 1-hour retention window. Invalidated live via `agent_liveness_changed` SSE. Returns an empty array when `orgSlug` is empty (no request issued).

### Mutation hooks

Each mutation invalidates the query keys it affects on success. Key taxonomy: [patterns.md § Query keys](patterns.md#query-keys). Hook-to-endpoint mapping is in `queries.ts`.

## Data owned

None. The `QueryClient` lives in `main.tsx`; hooks here just read/write it.

## How it's tested

E2e specs in `apps/e2e/tests/*.spec.ts` cover full hook + backend round-trips. Non-trivial cache logic (custom `select`, optimistic updates) gets Vitest tests in `apps/web/src/core/api/test/`. Domain-level integration tests use MSW (see [patterns.md § MSW testing strategy](patterns.md#msw-testing-strategy)).
