# domain/tickets

> Ticket list and detail — where yaaos posts live reviews and operators respond to HITL prompts.

## Scope

- `/tickets` — filterable list.
- `/tickets/$ticketId` — detail: workflow run step tree, findings, HITL panel.

Consumes: `GET /api/tickets`, `GET /api/tickets/:id`, `GET /api/tickets/:id/workflow-runs`, `GET /api/tickets/:id/activity/:executionId/:stepId`, `POST /api/reviewer/cancel`, findings/hitl endpoints. Owns no data.

## List page

`TicketsListPage` — outer shell with `<Suspense>` + `<ErrorBoundary>` (skeleton while loading, `ErrorBanner` on fetch failure). Inner `TicketsList` calls `useTickets()` (Suspense variant) + `useTicketsFilters` logic hook.

`useTicketsFilters` (`use-tickets-filters.ts`) — derives filter state (status chips, free-text `q`, repo picker, "My tickets" toggle), filtered/paginated rows, repo-options list, and `loadMore`. Takes `{tickets, repos, myEmail}`. Returns data + setters; no JSX. Tested at unit tier (`test/use-tickets-filters.test.ts`).

The `/tickets` route validates search params (`q`, `repo`, `status`, `mine`) via Zod in `core/routing/schemas.ts`.

Live updates: `ticket_status_changed` SSE invalidates `["tickets"]` (200 ms debounce). See [core/sse](architecture.md).

## Detail page

Three tabs: **Findings** (default), **Activity**, **HITL**. Each tab body is wrapped in its own `<ErrorBoundary>` + `<Suspense>` pair so a single tab failure does not crash the other tabs.

- **Findings** — `useFindingsForTicket(ticketId, true)`. Each `FindingRow` is non-interactive (read-only). Canonical schema: `severity ∈ {blocker, should_fix, nit}`, `confidence ∈ {verified, plausible, speculative}`, `category`, `rationale`, `rule_violated`, `rule_source`, `suggested_fix`, optional `file`/`line`.
- **Activity** — `useWorkflowRuns(ticketId)` (key `["workflow","runs",ticketId]`). The most recent run's steps render as a step tree:
  - Non-`InvokeClaudeCode` steps: compact label row (`name · state · ago`).
  - Running `InvokeClaudeCode`: pinned `max-h-[400px]` live stream via `useWorkflowActivityStream(executionId)` (SSE to `/api/sse/workspace_activity/:id`).
  - Terminal `InvokeClaudeCode`: `<details>/<summary>` accordion; opens with `useStepActivity(ticketId, executionId, stepId)` (key `["workflow","activity",executionId,stepId]`).
- **HITL** — `useHitlHistory` (`useSuspenseQuery`). First `resolved_at: null` row is the current prompt (`HitlPanel` renders `kind: "choice" | "text" | "form"`); resolved exchanges show below. `useHitlRespond(ticketId).mutate(response)` submits.

Header: title + status pill + Cancel button (`ConfirmModal`, non-terminal only). No Re-run.

`StageIndicator` is sourced from `useWorkflowRuns` (not `ticket.stages`). Runs arrive oldest-first; displayed left to right.

Live updates: `workflow_state_changed` SSE invalidates `["workflow","runs",ticketId]` + `["tickets",ticketId]`. No polling fallback.

## Standalone composites

`StageIndicator`, `HitlPanel`, `FindingRow`, `ActivityEventRow` — each has its own Vitest file under `test/`.

`ActivityEventRow` accepts `ReviewJobActivityEvent` from `core/api`. Used for both live SSE events (in the running `InvokeClaudeCode` step) and persisted blob events (in the terminal accordion).

## Public interface

- `apps/web/src/domain/tickets/public/TicketsListPage.tsx` — `TicketsListPage`
- `apps/web/src/domain/tickets/public/TicketDetailPage.tsx` — `TicketDetailPage`

Router imports each directly by path; no barrel.

## Tests

- `test/use-tickets-filters.test.ts` — unit: pure hook logic (status toggle, repo filter, query filter, myOnly, pagination, repoOptions merge).
- `test/tickets-list.test.tsx` — component/MSW: filter chips render, empty state.
- `test/ticket-detail.test.tsx` — component/MSW: title, stage indicator, tab strip, Cancel button, no Re-run, step tree.
- `test/finding-row.test.tsx` — component: severity/confidence chips, file:line, non-interactive.
- `test/stage-indicator.test.tsx` — component: empty, single-run, multi-run chronological, awaiting_human label.
- Page composition: `apps/e2e/tests/pr-review-end-to-end.spec.ts`.
