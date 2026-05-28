# domain/tickets

> Ticket list and detail — where yaaos posts live reviews and operators respond to HITL prompts.

## Scope

- `/tickets` — filterable list.
- `/tickets/$ticketId` — detail: findings, activity log, HITL panel.

Consumes: `GET /api/tickets`, `GET /api/tickets/:id`, `POST /api/reviewer/cancel`, `POST /api/reviewer/rereview`, findings/jobs/hitl endpoints. Owns no data.

## List page

`TicketsListPage` — filters: status chips (running/hitl/done/failed/cancelled), free-text `q`, repo picker, "My tickets" toggle. Table: Status · Title · Repo · Stage · Findings · Updated · Builder. Load-more pagination (50/click). Source: `useTickets()` → `GET /api/tickets`.

Live updates: `ticket_status_changed` SSE invalidates `["tickets"]` (200 ms debounce). See [core/sse](architecture.md).

## Detail page

Three tabs: **Findings** (default), **Activity**, **HITL**.

- **Findings** — `useFindingsForTicket(ticketId, true)`. Each `FindingRow` has inline Ack / Push-back for `state === "open"` (≥10-char reason gate on push-back).
- **Activity** — `useReviewJobsForTicket` flattened into a chronological stream via `ActivityEventRow`; long messages auto-collapse.
- **HITL** — first `resolved_at: null` row is the current prompt (`HitlPanel` renders `kind: "choice" | "text" | "form"`); resolved exchanges show below as JSON. `useHitlRespond(ticketId).mutate(response)` submits.

Header: Cancel (`ConfirmModal`, non-terminal only) and Re-run (`ConfirmModal`, cost-protective). Both fire through `core/api`.

Detail queries carry `refetchInterval` as SSE-gap fallback.

## Standalone composites

`StageIndicator`, `HitlPanel`, `FindingRow`, `ActivityEventRow` — each has its own Vitest file under `test/`.

## Tests

- `test/tickets-list.test.tsx` — filter chips, empty states.
- 4 composite Vitest files (see above).
- Page composition: `apps/e2e/tests/pr-review-end-to-end.spec.ts`.
