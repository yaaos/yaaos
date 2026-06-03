# domain/tickets

> Ticket list and detail — where yaaos posts live reviews and operators respond to HITL prompts.

## Scope

- `/tickets` — filterable list.
- `/tickets/$ticketId` — detail: findings, activity log, HITL panel.

Consumes: `GET /api/tickets`, `GET /api/tickets/:id`, `POST /api/reviewer/cancel`, `POST /api/reviewer/rereview`, findings/jobs/hitl endpoints. Owns no data.

## List page

`TicketsListPage` — outer shell with `<Suspense>` + `<ErrorBoundary>` (skeleton while loading, `ErrorBanner` on fetch failure). Inner `TicketsList` calls `useTickets()` (Suspense variant) + `useTicketsFilters` logic hook.

`useTicketsFilters` (`use-tickets-filters.ts`) — derives filter state (status chips, free-text `q`, repo picker, "My tickets" toggle), filtered/paginated rows, repo-options list, and `loadMore`. Takes `{tickets, repos, myEmail}`. Returns data + setters; no JSX. Tested at unit tier (`test/use-tickets-filters.test.ts`).

The `/tickets` route validates search params (`q`, `repo`, `status`, `mine`) via Zod in `core/routing/schemas.ts`.

Live updates: `ticket_status_changed` SSE invalidates `["tickets"]` (200 ms debounce). See [core/sse](architecture.md).

## Detail page

Three tabs: **Findings** (default), **Activity**, **HITL**. Each tab body is wrapped in its own `<ErrorBoundary>` + `<Suspense>` pair so a single tab failure does not crash the other tabs.

- **Findings** — `useFindingsForTicket(ticketId, true)` (`useSuspenseQuery`, `refetchInterval: 5s`). Each `FindingRow` has inline Ack / Push-back for `state === "open"` (≥10-char reason gate on push-back).
- **Activity** — `useReviewJobsForTicket` (`useSuspenseQuery`, `refetchInterval: 3s`) flattened into a chronological stream via `ActivityEventRow`; long messages auto-collapse.
- **HITL** — `useHitlHistory` (`useSuspenseQuery`). First `resolved_at: null` row is the current prompt (`HitlPanel` renders `kind: "choice" | "text" | "form"`); resolved exchanges show below as JSON. `useHitlRespond(ticketId).mutate(response)` submits.

Header: Cancel (`ConfirmModal`, non-terminal only) and Re-run (`ConfirmModal`, cost-protective). Both fire through `core/api`.

Detail queries carry `refetchInterval` as SSE-gap fallback.

## Standalone composites

`StageIndicator`, `HitlPanel`, `FindingRow`, `ActivityEventRow` — each has its own Vitest file under `test/`.

`ActivityEventRow` accepts `ReviewJobActivityEvent` from `@core/api` — no local duplicate interface. The same type is used for both persisted `activity_log` events and live SSE events merged in `ActivityTab`.

## Tests

- `test/use-tickets-filters.test.ts` — unit: pure hook logic (status toggle, repo filter, query filter, myOnly, pagination, repoOptions merge).
- `test/tickets-list.test.tsx` — component/MSW: filter chips render, empty state.
- `test/ticket-detail.test.tsx` — component/MSW: title, stage indicator, tab strip, Cancel/Re-run buttons.
- 4 composite Vitest files (see above).
- Page composition: `apps/e2e/tests/pr-review-end-to-end.spec.ts`.
