# core/api

> Typed HTTP client + the full TanStack Query hook surface every domain module consumes.

## Purpose

A small, hand-maintained layer between the FastAPI backend and the UI. Owns the typed `openapi-fetch` client, a generic `apiFetch<T>` helper, TypeScript shapes for every API resource, and one TanStack Query hook per endpoint.

## Public interface

Re-exports from `@core/api`: client helpers (`apiClient`, `apiFetch`, `getCurrentOrgSlug`, `setCurrentOrgSlug`), one TypeScript type per backend resource, one TanStack Query hook per endpoint, and one mutation hook per write operation. See `apps/web/src/core/api/index.ts` for the full list.

## Module architecture

### Two clients, one helper

`client.ts` exposes two surfaces:
- `apiClient` — `openapi-fetch` typed client. `Paths` hand-declared (currently covers `/api/health`); codegen deferred.
- `apiFetch<T>(path, init?)` — generic fetch wrapper. On 401, lazy-imports `handleAuthFailure` (breaks load-path cycle) which hard-navigates to `/login?reason=...&next=<current-path>`. Non-2xx throws `${status} ${path}: ${body}`. 204 → `undefined`.

### Central 401 handler

`auth-failure.ts` owns the one-and-only redirect-on-auth-died path:

- **Mutex** — a module-level `redirectInProgress` flag means concurrent 401s (every page often fires `/api/auth/me` + `/api/orgs/mine` + page queries in parallel) trigger exactly one `window.location.assign`. Hard nav rather than TanStack Router soft-nav clears React state + the query cache, which is the right thing when the session is dead.
- **Reason mapping** — backend `{"error": "<code>"}` body → UX banner reason: `session_idle_expired → "idle"`, `session_expired → "expired"`, `unauthenticated → "signed_out"`, unknown → `"signed_out"` (catch-all so renames don't break the banner).
- **`next` round-trip** — captures `window.location.pathname + search + hash` and tags it as `?next=`. `LoginPage` forwards it through the OAuth flow's `next` query param; backend `_safe_next` (and our mirroring `safeNext` helper) reject scheme-relative / off-origin paths and `/login` loops. The user lands back where they were trying to go after sign-in. Covers both "session died mid-flow" and "cold deeplink while logged out" identically.
- The backend already clears `yaaos_session` + `yaaos_csrf` via `Set-Cookie: Max-Age=0` on every 401 it issues (see [`apps/backend/app/core/auth/auth_failure.py`](../../backend/app/core/auth/auth_failure.py)), so by the time the redirect fires the browser already has fresh state.

### Resource types

Type aliases in `client.ts` mirror backend Pydantic models. Non-obvious fields:
- `Ticket` — `pr_number`, `author_login`, `is_draft` enriched from the linked PR at read-time.
- `Finding` — `severity: "must-fix" | "nit" | "suggestion" | "info"`; optional `rationale`, `snippet: FindingSnippetLine[]`, `applied_lesson_ids`, `source_agent`.
- `ReviewJob` — one row per (PR × review run); includes `activity_log` (persisted coding-agent stream events).
- `ReviewJobActivityEvent` — `{ts, kind, message, detail?}`; used in `ReviewJob.activity_log` and as the SSE payload for `/api/sse/workspace_activity/{id}`.
- `PluginMeta` — from `/api/settings/plugins`; drives the Settings UI plugin list.

### Query hooks

`queries.ts` — one hook per endpoint. Polling intervals (3–5s) are a safety net for missed SSE messages (see [core_sse.md](core_sse.md)); see the file for the full endpoint-to-hook mapping.

### Mutation hooks

Each mutation invalidates the query keys it affects on success. Key taxonomy: [patterns.md § Query keys](patterns.md#query-keys). Hook-to-endpoint mapping is in `queries.ts`.

## Data owned

None. The `QueryClient` lives in `main.tsx`; hooks here just read/write it.

## How it's tested

E2e specs in `apps/e2e/tests/*.spec.ts` cover full hook + backend round-trips. Non-trivial cache logic (custom `select`, optimistic updates) gets Vitest tests in `apps/web/src/core/api/test/`.
