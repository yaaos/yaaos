# core/api

> Typed HTTP client + the full TanStack Query hook surface every domain module consumes.

## Purpose

A small, hand-maintained layer between the FastAPI backend and the UI. Owns the typed `openapi-fetch` client, a generic `apiFetch<T>` helper, TypeScript shapes for every API resource, and one TanStack Query hook per endpoint.

## Public interface

Re-exports from `@core/api`:
- **Client:** `apiClient`, `apiFetch`.
- **Resource types:** `HealthResponse`, `OnboardingStatus`, `Ticket`, `Lesson`, `ReviewJob`, `ReviewJobActivityEvent`, `Finding`, `FindingSnippetLine`, `AuditEntry`, `GithubInstallation`, `GithubRepository`, `GithubRepositoriesResponse`, `PluginMeta`, `PluginType`, `PluginHealth`, `SetGithubCredentialsInput`.
- **Queries:** `useHealth`, `useOnboarding`, `useTickets`, `useTicket`, `useTicketAudit`, `useReviewJobsForTicket`, `useLessons`, `useMetricsSummary`, `useGithubInstallation`, `useGithubRepositories`, `usePluginsList`, `usePluginHealth`.
- **Mutations:** `useRereviewMutation`, `useCancelReviewerJobs`, `useCreateLesson`, `useDeleteLesson`, `useSetAnthropicKey`, `useSetGithubCredentials`.

## Module architecture

### Two clients, one helper

`client.ts`:
- `apiClient` — `openapi-fetch` typed client. `Paths` is hand-declared and currently only covers `/api/health`.
- `apiFetch<T>(path, init?)` — generic fetch wrapper. Throws on non-2xx with `${status} ${path}: ${body}`; returns `undefined` on 204; otherwise returns parsed JSON.

OpenAPI codegen is deferred — the surface is small enough that hand-declared types are cheaper.

### Resource types

Each API resource has a type alias in `client.ts`, mirroring the backend Pydantic models. Notes:
- `Ticket` — includes `pr_number` / `author_login` / `is_draft` enriched from the linked PR at read-time.
- `Finding` — `severity` is `"must-fix" | "nit" | "suggestion" | "info"`; carries optional `rationale`, `snippet: FindingSnippetLine[]`, `applied_lesson_ids`, and `source_agent` (which yaaos subagent surfaced this finding).
- `ReviewJob` — one row per (PR × review run). Full state including `current_step`, `last_heartbeat_at`, `tokens_in`/`out`, `findings`, `model`, `effort`, and `activity_log` (persisted chronological events from the coding-agent stream).
- `ReviewJobActivityEvent` — `{ts, kind, message, detail?}`. `message` is rendered server-side. Used both in `ReviewJob.activity_log` (persisted) and as the payload of `review_job_activity` SSE events (live tail).
- `PluginMeta` — driven by `/api/settings/plugins` so the Settings UI auto-lists plugins.

### Query hooks

`queries.ts` defines one hook per endpoint:

| Hook | Endpoint | Refetch |
|---|---|---|
| `useHealth` | `GET /api/health` | 5s |
| `useOnboarding` | `GET /api/settings/onboarding` | 5s |
| `useTickets` | `GET /api/tickets` | 3s |
| `useTicket(id)` | `GET /api/tickets/${id}` | — |
| `useTicketAudit(id)` | `GET /api/tickets/${id}/audit` | 3s |
| `useReviewJobsForTicket(id)` | `GET /api/reviewer/jobs/by-ticket/${id}` | 3s |
| `useLessons(repo?)` | `GET /api/memory/lessons[?repo=...]` | — |
| `useMetricsSummary` | `GET /api/reviewer/metrics` | 5s |
| `useGithubInstallation` | `GET /api/github/installation` | 5s |
| `useGithubRepositories` | `GET /api/github/repositories` | on demand |
| `usePluginsList` | `GET /api/settings/plugins` | — |
| `usePluginHealth(id)` | `GET /api/${id}/health` | 5s |

Polling intervals are a safety net for missed SSE messages (see [core_sse.md](core_sse.md)).

### Mutation hooks

Mutations invalidate the keys they affect on success:

| Hook | Endpoint | Invalidates |
|---|---|---|
| `useRereviewMutation` | `POST /api/reviewer/rereview?ticket_id=...` | `["tickets"]`, `["reviewer","jobs",id]`, `["tickets",id,"audit"]`, `["reviewer","metrics"]` |
| `useCancelReviewerJobs` | `POST /api/reviewer/cancel?ticket_id=...` | same as re-review |
| `useCreateLesson` | `POST /api/memory/lessons` | `["memory", repo]` |
| `useDeleteLesson` | `DELETE /api/memory/lessons/${id}` | `["memory", repo]` |
| `useSetAnthropicKey` | `POST /api/claude_code/api_key` | `["onboarding"]`, `["plugin-health","claude_code"]` |
| `useSetGithubCredentials` | `POST /api/github/credentials` | `["github","installation"]`, `["plugin-health","github"]`, `["onboarding"]` |

Key taxonomy: see [patterns.md § Query keys](patterns.md#query-keys).

## Data owned

None. The `QueryClient` lives in `main.tsx`; hooks here just read/write it.

## How it's tested

- `apps/web/src/domain/dashboard/test/dashboard.test.tsx` exercises `useOnboarding` indirectly.
- Every e2e spec in `apps/e2e/tests/*.spec.ts` drives full hook + backend round-trips.

Non-trivial cache logic (custom `select`, optimistic updates) earns dedicated Vitest tests in `apps/web/src/core/api/test/`.
