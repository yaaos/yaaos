# domain/tickets

> Ticket list and detail — where anyone follows a pipeline run and a human resolves a blocked stage.

## Scope

- `/tickets` — filterable list.
- `/tickets/$ticketId` — detail: three tabs — Overview, Runs, Artifacts.

Consumes: `GET /api/tickets`, `POST /api/tickets`, `GET /api/tickets/:id`, `GET /api/tickets/:id/audit`, `GET /api/pipelines`, `GET /api/pipelines/runs`, `GET /api/pipelines/runs/overview`, `POST /api/pipelines/runs/start`, `GET /api/pipelines/runs/:runId/stages/:stageExecutionId/activity`, `POST /api/pipelines/runs/:runId/cancel`, `POST /api/pipelines/runs/pauses/:pauseId/respond`, `POST /api/pipelines/runs/rerun`, `POST /api/pipelines/runs/:runId/rerun`, `GET /api/artifacts`, `GET /api/artifacts/:id`, `GET /api/attachments`. Owns no data.

## List page

`TicketsListPage` — outer shell with `<Suspense>` + `<ErrorBoundary>` (skeleton while loading, `ErrorBanner` on fetch failure). Inner `TicketsList` calls `useTickets()` (Suspense variant) + `useTicketsFilters` logic hook.

`useTicketsFilters` (`use-tickets-filters.ts`) — derives filter state (status chips, free-text `q`, repo picker, "My tickets" toggle), filtered/paginated rows, repo-options list, and `loadMore`. Takes `{tickets, repos, myEmail}`. Returns data + setters; no JSX. Tested at unit tier (`test/use-tickets-filters.test.ts`).

The `/tickets` route validates search params (`q`, `repo`, `status`, `mine`) via Zod in `core/routing/schemas.ts`.

Live updates: `ticket_status_changed` SSE invalidates `["tickets"]` (200 ms debounce). See [core/sse](architecture.md).

## Detail page

Three tabs: **Overview** (default), **Runs**, **Artifacts**. Each tab body is wrapped in its own `<ErrorBoundary>` + `<Suspense>` pair (Overview's own top-level fetch is a plain, non-Suspense query — see below) so a single tab failure does not crash the other tabs.

### Overview tab (`overview.tsx`)

`useRunOverview(ticketId)` — a plain (non-Suspense) query; a 404 resolves to `null` (a legitimate "no run yet" empty state, not an error). When `overview === null` AND `ticket.type === "manual"`, renders a **KickoffCard** (wrapped in `<ErrorBoundary>` + `<Suspense>`) instead of the default empty state: a pipeline Select (`data-testid="kickoff-pipeline"`, options from `usePipelines()`), a prompt Textarea (`data-testid="kickoff-prompt"`), and a **Run** button (`data-testid="kickoff-run"`). On submit fires `useStartRun(ticketId)` → `POST /api/pipelines/runs/start`. A 409 (`run_in_flight`) surfaces a `ConfirmModal` (`data-testid="kickoff-confirm"`) — "Replace in-flight run?" — re-submitting with `replace_in_flight=true` on confirm. When `overview === null` AND `ticket.type !== "manual"`, the existing empty-state prose renders.

Branches on `RunOverview.status`:

- **`paused`** — the attention block: tripped-condition badges, the pausing stage's artifact in a `<details>` disclosure (`useArtifactVersion`), open residual findings, and four actions — Approve, Instruct (+ `Textarea`), Send back (+ `Select` of earlier skill/review stage names, sourced from the ticket's current run via `useRuns`), and Kill (destructive, behind `ConfirmModal` — "Kill run?" / "This can't be undone."). All four disabled with a `pause-waiting-on` "Waiting on {escalation logins}." line when the server-sent `PauseDetail.can_respond` is `false` — no client role math; `resolve_pause`'s own authorization (escalation set ∪ org admins) is the sole source of truth.
- **`in_flight`** — a live card naming the pipeline + current stage, with a Cancel action behind `ConfirmModal` ("Cancel run?"). Also shows a `data-testid="overview-live-ticker"` line when at least one activity frame has arrived for the run's current stage — displays the most recent frame's `message`. Clicking the ticker switches to the Runs tab. The `in_flight` branch also carries `data-connected="true"|"false"` on the `attention-block` element (driven by `useRunActivityTail.connected` / EventSource `onopen`).
- **`terminal`** — an outcome card: state + a PR link when `RunOutcome.pr_url` is set, else a mono `failure_reason`. A `failed`/`cancelled`/`killed` outcome also shows a **Re-run** action behind `ConfirmModal` ("Re-run pipeline?" / "Starts a new run from the beginning.") → `useRerunRun(ticketId)` → `POST /api/pipelines/runs/{run_id}/rerun`, targeting `RunOutcome.run_id`.

The card always carries `data-testid="attention-block"` with `data-state` set to `paused` / `in_flight` / the terminal run state (`completed`/`failed`/`killed`/`cancelled`) — one selector regardless of branch.

Below the run-state card, the Overview tab renders a **read-only attachments section** (`AttachmentsSection`). Renders nothing when there are no attachments. When present: an `<h3>` label + `<ul data-testid="attachments-list">` with one `<li data-testid="attachment-row-${a.id}">` per attachment showing filename, optional `produced_by_skill`, and `ago(attached_at)`. Populated by `useAttachments(ticketId)` (Suspense) → `GET /api/attachments?ticket_id=`. Live updates: `attachment_added` SSE invalidates `["attachments", ticketId]`.

### Runs tab (`runs.tsx`)

`useRuns(ticketId)` (Suspense) — every run for the ticket, newest first. One `<details>` card per run (`run-card-${id}`, newest open by default) containing a dense `Table` of that run's stage executions:

- Card summary line: pipeline name, state badge, actor, `ago(created_at)` + total duration (`duration(created_at, completed_at)` — elapsed against now while `completed_at` is null), and (on a `failed`/`cancelled`/`killed` run) a **Re-run** button.
- Columns: stage name (kind icon), status, timing (`ago(started_at)` + `duration(started_at, completed_at)`, `text-xs text-muted-foreground mono`), confidence badge, review iterations, boundary outcome, decisions (inline `action by actor · ago`) + mono `failure_reason`, and a row-action cell.
- **Activity** toggle (skill/review stages only, `data-testid="stage-activity-toggle-${stageName}"`) expands a second table row with two branches:
  - **Running stage** — `data-testid="stage-activity-live"`: live-tail pane subscribed to the workspace-activity SSE stream via `useRunActivityTail(runId)`; appends `ActivityEventRow`s as frames arrive; auto-scrolls to bottom; shows "Streaming live" until the first frame. Also carries `data-connected="true"|"false"` (driven by `useRunActivityTail.connected` / EventSource `onopen`) — `"true"` once the backend's Redis subscription is confirmed. Connects immediately on accordion open; disconnects on accordion close (component unmount) or stage completion.
  - **Terminal stage** — calls `useStageActivity(runId, stageExecutionId)` (Suspense, lazy-fetched on open) and renders the persisted `ActivityLog.events` as `ActivityEventRow`s. Shows "No activity recorded." when `events` is empty or the blob is absent.
- **Artifact** button (rows that produced one) opens a right `Sheet` with the latest version's rendered `Markdown` body.
- **Instruct & re-run** button (completed skill/review rows) opens a `Dialog` + `Textarea` → `useRerunFromStage(ticketId)` → `POST /api/pipelines/runs/rerun`.
- **Re-run** button (`data-testid="rerun-run"`, on the card summary when `run.state` is `failed`/`cancelled`/`killed`) opens `ConfirmModal` ("Re-run pipeline?" / "Starts a new run from the beginning.") → `useRerunRun(ticketId)` → `POST /api/pipelines/runs/{run_id}/rerun`. The button's `onClick` calls `preventDefault`/`stopPropagation` so it never toggles the card's `<details>` accordion.

### Artifacts tab (`artifacts.tsx`)

`useArtifacts(ticketId)` (Suspense) — one lineage section per stage name (`artifact-lineage-${stageName}`): an `h2` + a version `Select` ("v4 · \<pipeline\> · 2d ago", non-final versions suffixed "(draft)") + the selected version's rendered `Markdown` body (`useArtifactVersion`).

Header: title + status pill. No page-level Cancel/Kill — those actions live inside the Overview tab's per-state card, scoped to the run/pause they act on.

Live updates: `run_state_changed` SSE invalidates `["runs", ticketId]` + `["runs","overview",ticketId]` + `["tickets", ticketId]`; `stage_state_changed` invalidates `["runs", ticketId]` + `["runs","stage-activity"]` prefix (so the persisted blob refetches when a stage completes); `artifact_stored` invalidates `["artifacts", ticketId]`. No polling fallback.

## Standalone composites

`ActivityEventRow` — pure-render row for one coding-agent activity event (icon-by-`kind`, `ago` timestamp, collapsible long messages); shared by the Runs tab's per-stage activity accordion. Has its own Vitest file under `test/`.

## Public interface

- `apps/web/src/domain/tickets/public/TicketsListPage.tsx` — `TicketsListPage`
- `apps/web/src/domain/tickets/public/TicketDetailPage.tsx` — `TicketDetailPage`

Router imports each directly by path; no barrel.

## Tests

- `test/use-tickets-filters.test.ts` — unit: pure hook logic (status toggle, repo filter, query filter, myOnly, pagination, repoOptions merge).
- `test/tickets-list.test.tsx` — component/MSW: filter chips render, empty state.
- `test/ticket-detail.test.tsx` — component/MSW: title, status pill, tab strip; Overview's three `RunOverview.status` branches (paused / in_flight / terminal); the disabled-actions "Waiting on {names}." state when `can_respond` is `false`; switching to the Runs tab renders a stage row; the terminal outcome card's Re-run action (renders for a `failed` outcome, confirm-then-mutation fires the rerun POST).
- `test/runs-tab.test.tsx` — component/MSW: the Runs tab card's Re-run button renders for `failed`/`cancelled`/`killed` runs, is absent for `completed`/`running` runs, confirm-then-mutation fires `POST /api/pipelines/runs/{id}/rerun`, clicking it does not toggle the card's accordion, and rendered timing (summary total duration + per-stage timing column).
- `test/live-activity.test.tsx` — component/MSW: `stage-activity-live` renders when the stage is running and the Activity accordion is opened; persisted branch ("No activity recorded.") renders when the stage is completed; `overview-live-ticker` appears with the most recent frame message; ticker hidden when no frames arrived.
- `test/activity-event-row.test.tsx` — component: icon-by-kind mapping, long-message collapse.
- Page composition (browser-visible): `apps/e2e/tests/pipeline-run-overview.spec.ts` (attention block, live SSE-driven pause resolution, role-gated actions), `apps/e2e/tests/pipeline-run-tabs.spec.ts` (Runs tab stage rows, Artifacts tab version dropdown), `apps/e2e/tests/pipeline-live-activity.spec.ts` (live activity rows appear in `stage-activity-live` without reload; overview ticker shows message + switches to Runs tab), `apps/e2e/tests/manual-kickoff.spec.ts` (KickoffCard renders for a manual ticket with no run; picking a pipeline + clicking Run starts a run; a 409 shows `kickoff-confirm`).
