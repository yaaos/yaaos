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

- `apps/web/src/domain/tickets/TicketsListPage.tsx` — the M06 list page (E2a.1).
- `apps/web/src/domain/tickets/index.tsx` — re-exports `TicketsListPage as TicketsPage` and still holds the legacy `TicketDetailPage` until the Phase 6 rewrite lands.

### List page (M06)

`TicketsListPage`:
- **Status chips (multi-select)** — running / hitl / done / failed / cancelled, M06 vocab from `m06_status` on each row. Default active: running + hitl.
- **Filters** — free-text search over title (`q`), repo single-select (`Select` primitive), "My tickets" toggle filtering by `t.author_login === user.primary_email`.
- **Table columns** — Status (icon + label) · Title · Repo (mono) · Stage · Findings (count + severity dot when `max_severity` set) · Updated (ago) · Builder (display name, or "yaaos" badge when `builder_kind === "system"`).
- **Load-more pagination** — reveals 50 more rows per click; no infinite scroll, no numbered pagination.
- **State patterns** — `Skeleton` table on first load, `EmptyState` (Search icon) when filters bite, `EmptyState` (Ticket icon) when truly empty, `ErrorBanner` with Retry on fetch failure.
- **Source of truth** — `useTickets()` → GET /api/tickets; the wire shape is `{items, next_cursor}` and the hook unwraps `items`.

### Detail page

`TicketDetailPage`:
- **Header** — `#PR · repo`, title, status + kind + draft chips, author byline. Buttons: **Cancel jobs** and **Re-review**.
- **Tabs** — Review (default) and Audit log, each with live counts.

### Review tab — `SummaryStrip`

Four-cell card: Findings (red if any blocker/major), Tokens (in + out), Latency (live-ticking `LiveLatency` while running; otherwise `duration_s`), Lessons applied. Findings counter sources from the durable findings table (`useFindingsForTicket`, `include_terminal=true`) so it counts unique findings across every review run; per-review JSONB caches would undercount on multi-review tickets. Tokens / Latency / Lessons aggregate across every `ReviewJob` for the ticket. Cost cell removed — backend no longer tracks cost.

### Review tab — `AgentCard`

One card representing the yaaos parent reviewer. Carries `data-testid="agent-card-yaaos"` and `data-state="<status>"`. The card header shows the job's `model` (alias requested, resolved name on completion) and `effort` next to the subtitle.

Body composition (applies to every status with a job — except `no-job` which renders an empty-state CTA):
- **Status banner** (top) — one-line `Running · resolving_entities` / `Posted: 4 findings` / `Failed: <error_message>` / `Skipped: <skip_reason>` / `Cancelled (<skip_reason>)`. Running shows the indeterminate bar + tokens too.
- **Activity feed** (`data-testid="activity-feed"`) — newest 10 events from the merged source: `job.activity_log` (hydrated history) ++ `useLiveActivity(job.id)` (live SSE tail), deduped by `ts+kind+message`, sorted newest-first. Format: `<formatTime(ts)> · <message>`.
- **"All events (N)"** `<details>` — full event list, collapsible.
- **Findings list** — only when `status === 'posted'` and findings exist; otherwise the banner alone covers the posted state.

### Finding rows

Inside `findings-list`: severity dot + title + severity label + `file:line` + subagent tag from `source_agent`. Click expands → body, italic `rationale`, line-numbered snippet diff. Applied-lesson chip(s) link to `/lessons`. **"Teach yaaos…"** button opens the modal.

### Teach-yaaos modal

Pre-fills title (empty), body (finding's body, editable, 1000-char cap), repo (the ticket's). Submit → `useCreateLesson` posts `/api/lessons`, invalidates `["lessons", repo]`, closes.

### Audit tab

Renders `useTicketAudit(id)` as a vertical list: `formatTime(created_at)` · `kind` · `[actor.kind:actor.login]`. Click expands the full payload JSON.

### Cancel / Re-review

- **Re-review** — `useRereviewMutation` → `POST /api/reviewer/rereview`. Cancels in-flight jobs for the PR via supersede discipline, then schedules one new review.
- **Cancel jobs** — `useCancelReviewerJobs` → `POST /api/reviewer/cancel?ticket_id=...`.

### Live updates

The SSE subscriber invalidates `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "jobs", id]`, and `["reviewer", "metrics"]` on the appropriate kinds (see [core_sse.md](core_sse.md)). 3s polling is the safety net.

## Data owned

None. State lives in `core/api` caches; mutations target endpoints owned by `domain/reviewer` and `domain/lessons`.

## How it's tested

E2e specs in `apps/e2e/tests/` exercise the round-trip; assertions check for exactly one `agent-card-` per ticket. No Vitest — components are render-heavy.
