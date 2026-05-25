# domain/tickets

> Ticket list and ticket detail — the product's signature surface where the yaaos parent reviewer posts live reviews.

## Purpose

Two pages under `/tickets`:
- `/tickets` — filterable, group-able list of every ticket.
- `/tickets/$ticketId` — detail: one review card showing findings tagged by the subagent that surfaced each, audit log, Teach-yaaos modal.

The only surface that exercises the full live-update path (webhook → reviewer pipeline → SSE → review card state swap).

## Public interface

- `TicketsPage`, `TicketDetailPage` — mounted by `core/routing` at `/tickets` and `/tickets/$ticketId`. The Teach-yaaos modal is a private subcomponent.

## Module architecture

- `apps/web/src/domain/tickets/TicketsListPage.tsx` — the list page (E2a.1).
- `apps/web/src/domain/tickets/index.tsx` — re-exports `TicketsListPage as TicketsPage` and the legacy `TicketDetailPage`.

### List page

`TicketsListPage`:
- **Status chips (multi-select)** — running / hitl / done / failed / cancelled, vocab from `status` on each row. Default active: all five.
- **Filters** — free-text search over title (`q`), repo single-select (`Select` primitive), "My tickets" toggle filtering by `t.author_login === user.primary_email`.
- **Table columns** — Status (icon + label) · Title · Repo (mono) · Stage · Findings (count + severity dot when `max_severity` set) · Updated (ago) · Builder (display name, or "yaaos" badge when `builder_kind === "system"`).
- **Load-more pagination** — reveals 50 more rows per click; no infinite scroll, no numbered pagination.
- **State patterns** — `Skeleton` table on first load, `EmptyState` (Search icon) when filters bite, `EmptyState` (Ticket icon) when truly empty, `ErrorBanner` with Retry on fetch failure.
- **Source of truth** — `useTickets()` → GET /api/tickets; the wire shape is `{items, next_cursor}` and the hook unwraps `items`.

### Detail page

`TicketDetailPage` (apps/web/src/domain/tickets/TicketDetailPage.tsx):

- **Header band** — repo · PR link (when present) · "updated <ago>"; title (h1); status pill (running spins, others static-tinted via shared `M06_STATUS_META`); "by <builder.display_name>" or "by yaaos" when `builder_kind === "system"`. Right-aligned action buttons: **Cancel** (non-terminal status only, destructive `ConfirmModal`) and **Re-run** (cost-protective `ConfirmModal`).
- **Stage indicator** — composes `StageIndicator` against `ticket.stages` from `GET /api/tickets/:id`. Hides itself when the field is absent.
- **3-tab strip** — Findings (default) / Activity / HITL.

#### Findings tab

`useFindingsForTicket(ticketId, true)` (include terminals). Each row is the standalone `FindingRow` composite — severity pill, file:line, body excerpt, inline **Ack** + **Push back** for `state === "open"`. The row's callbacks wire to `useAckFinding(ticketId)` and `usePushBackFinding(ticketId)`; both invalidate the findings query key on success.

#### Activity tab

`useReviewJobsForTicket(ticketId)` — flattens every job's `activity_log[]` into one chronological stream. Each event renders via `ActivityEventRow` (lucide icon per the kind taxonomy; long messages auto-collapse). `EmptyState` when the stream is empty.

#### HITL tab

`useHitlHistory(ticketId)` returns past + current exchanges. The first `resolved_at: null` row is the current prompt — rendered through `HitlPanel` (discriminated-union renderer for `kind: "choice" | "text" | "form"`, free-text fallback for unknown kinds). Resolved exchanges show in a "History" list below as JSON-pretty `resolution_payload`. `useHitlRespond(ticketId).mutate(response)` submits.

### Standalone composites

All four pieces above are pure-render components in their own files with Vitest coverage:

| File | Tests | Purpose |
|---|---|---|
| `StageIndicator.tsx` | `test/stage-indicator.test.tsx` | Renders stages array; single-stage chip + multi-stage chronological. |
| `HitlPanel.tsx` | `test/hitl-panel.test.tsx` | Discriminated-union renderer + fallback. |
| `FindingRow.tsx` | `test/finding-row.test.tsx` | Severity pill + inline ack/push-back UX (≥10-char reason gate). |
| `ActivityEventRow.tsx` | `test/activity-event-row.test.tsx` | Kind → icon mapping; long-message collapse via `<details>`. |

Keeping each composite separately testable means the page-level rewrite stayed render-only — no per-composite logic re-tested by the page-level smoke.

### Live updates

The tickets list (`useTickets`) relies entirely on SSE — `ticket_status_changed` invalidates `["tickets"]` via `core/sse/subscriber`, with the 200 ms debounce coalescing bursts. Detail-page queries (findings, jobs, hitl history) still carry a `refetchInterval` as a safety net for SSE gaps.

### Cancel / Re-run

`useCancelReviewerJobs.mutate(ticketId)` → `POST /api/reviewer/cancel?ticket_id=...`. `useRereviewMutation.mutate(ticketId)` → `POST /api/reviewer/rereview`. Both are routed through `ConfirmModal` so the destructive vs cost-protective copy lands per `D3` voice rules.

## Data owned

None. State lives in `core/api` caches; mutations target endpoints owned by `domain/reviewer` and `domain/tickets`.

## How it's tested

- `TicketsListPage`: `test/tickets-list.test.tsx` (filter chips render, empty state).
- Per-composite: 4 Vitest files (above).
- The page-level composition is exercised end-to-end by the PR-review e2e — see `apps/e2e/tests/pr-review-end-to-end.spec.ts`.
