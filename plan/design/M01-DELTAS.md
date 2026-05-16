# M01 — Deltas from the design prototype

> The design files in this folder are the visual + interaction reference.
> This doc lists everything the design shows that **changes** for M01 implementation — what to drop, what to hardcode, what differs in scope or backend shape.
> Read this alongside `README.md` (the design handoff) and the M01 planning docs (`../milestones/M01-code-review/`).

When the design and this doc disagree, **this doc wins**. When this doc and the planning docs disagree, **the planning docs win**.

---

## Locked decisions

### Tickets — `kind` chip is a hardcoded placeholder
The design shows a `new feature` / `bug fix` / `investigation` chip on ticket cards, the ticket header, and the tickets list. In M01 there is **no `kind` field on the Ticket entity**. Hardcode the chip as `feature` on the frontend everywhere it appears. Treat as forward-compat for M02 intake sources; revisit then.

### Findings — surface the new fields, no in-UI interactions
- **Schema (in `Finding`):** `snippet` (structured `FindingSnippetLine[]`, with `line_number`, `kind ∈ context|add|del`, `text`), `rationale` (short string, rendered as italic quote under the body), and `applied_lesson_ids` (list of lesson UUIDs). The agent fills these in; the UI renders them. See `internals/vcs.md` for the locked types.
- **Lesson chips:** the `applied_lesson_ids` field on a finding drives the lesson chip(s). Chip text = the lesson's title; clicking the chip navigates to `/memory/$repoId` and highlights the lesson.
- **No interactive controls on findings.** Remove the **Resolve**, **Dismiss**, and **Reply on GitHub** action buttons from the prototype. Findings are read-only display in the yaaof UI.
  - The only interactive control on a finding is **"Teach yaaof…"** — opens the `New lesson` modal pre-filled with the finding's file + body + a link back to the source PR. Save creates a lesson on the finding's repo.
  - A non-interactive "view on GitHub ↗" link is fine (it's navigation, not a control on the finding state).
- All review-thread interaction (replies, resolutions, dismissals) happens on GitHub. The yaaof UI captures and displays results; it does not let users act on them.

### Lessons — no `applied_count`
The design shows "applied 217×" on each lesson. **Drop the counter from the UI everywhere.** yaaof does not maintain per-lesson aggregate counters in M01 (would require per-prompt lesson-ID tracking we explicitly scope out). Lessons render: title, body, source PR link, created/updated timestamps. That's it.

### Worker pool affordances — drop
The design surfaces "Worker pool · 2 of 4 busy" on queued agent cards and a `Queue · workers` metric tile (`1 · 2/4`) on the dashboard. M01 has **no worker pool** (background work is direct `asyncio.create_task`). Remove both affordances:
- Queued agent card body: show only the spinner + step label.
- Dashboard metric tiles: drop the "Queue · workers" tile entirely (5 tiles → 4).

### Audit kinds — keep the small set; no heartbeats, no step transitions
The design's audit log uses `review_job.heartbeat` and `review_job.step_changed`. **Both are dropped.**
- **No `review_job.heartbeat` audit entries.** The heartbeat coro updates `review_jobs.last_heartbeat_at` in place; nothing audited.
- **No `review_job.step_changed` audit entries.** Phase transitions only update `review_jobs.current_step` in place; the UI reads it live via SSE.

The audit-log tab's filter chips reflect only the kinds actually emitted (see `internals/reviewer.md` "Audit log entries" table for the full M01 set).

### Started / completed — first-class columns, no extra audit kind
Track when each review job runs via `review_jobs.started_at` and `review_jobs.completed_at` (already in the data model). Also persist `review_jobs.duration_s` on completion for read-speed. **No `review_job.started` audit entry** — the `review_job.prompt_sent` entry is the start-of-work marker in the timeline.

### `review_jobs` denormalized columns
The agent card surfaces several fields that are also captured in the audit log; for read-speed they live on the row:
- `prompt_hash` (text)
- `lessons_applied` (UUID[])
- `tokens_in`, `tokens_out` (int)
- `cost_usd` (numeric)
- `duration_s` (int, set on completion)

The audit log remains historical truth; the row is the convenience view.

### View prompt button — drop
The design adds a `View prompt` button on each agent card. **Drop the button.** yaaof stores only the prompt **hash** (not the full text); there is nothing to view. The audit-log entry for `review_job.prompt_sent` shows the hash, model, lessons-applied count, and checkout sha — which is what's available.

### Findings — Memory loop is the only interactive thread
The single interactive thread on a finding is the "Teach yaaof…" button. Implementation:
- Button on the expanded finding row.
- Click → opens the existing `New lesson` modal (the one on the Memory page).
- Modal is pre-filled with:
  - `repo` = the finding's repo (the modal's repo tag).
  - `title` = empty (let the user write).
  - `body` = the finding's body (so the user can edit it down to a lesson).
  - Source link = the source PR.
- Save → new lesson is created on the repo; toast confirms.

### User preferences
- **Theme** (light / dark): keep. Auto-detect OS preference + manual toggle in the topbar. Persist in `localStorage` (M01 has no auth so no per-user DB row).
- **Sidebar pin state** (pinned / floating): keep. Persist in `localStorage`.
- **Density** (compact / regular / comfy): **drop completely.** Single density throughout. Remove the Tweaks-panel density control + the `data-density` attribute + the `--d-row` / `--d-pad` density-scaled values from the token file. Pick one density (the design's "regular": 38px row, 16px pad) and bake it in.

### Search and command palette — drop
The design shows a `/` shortcut on the tickets list (focuses search) and `⌘K` command palette in the topbar. **Drop both for M01.**
- Remove the search input on the tickets list (and the `/` hint chip).
- Remove the search icon-button in the topbar.
- The `⌘K` keyboard binding is gone.
- Filtering remains via the existing filter chips on each list (Tickets, Memory, etc.).

### Org name — none in M01
The design shows `acme` in headers ("acme · last 24h", "acme · N in review · M done"). yaaof has **no org name field in M01**. Drop the org name from every header subtitle.

### Logo — reserve space, no asset yet
The design's logo is a placeholder "Y" with a `LOGO · PLACEHOLDER` mono caption. **Keep the placeholder slot** (don't restructure the sidebar header), but render a simple "yaaof" wordmark for now. A real logo lands later; the slot is sized for it.

### Dashboard activity feed deferred (2026-05-16)
The design's right-hand "Activity" panel needs `GET /api/dashboard/activity`, which isn't built yet. The dashboard's populated state runs the "Live agents · in flight" panel full-width until the activity endpoint lands. Revisit when there's user pull for it.

### Dashboard sparklines + 24h deltas deferred (2026-05-16)
Each metric tile in the design shows a sparkline (Reviews 24h) or a `+delta` indicator. `GET /api/reviewer/metrics` returns lifetime aggregates only — no 24h buckets, no hourly data. Tiles render headline value + subtitle ("all-time") until `GET /api/dashboard/metrics` (24h-windowed, bucketed) is defined. The user-visible difference is the absence of the line/delta widgets; the tile layout and hierarchy are unchanged.

### Settings — three independent cards, no gating (2026-05-16)
A previous implementation showed an "Onboarding status" badge card + an Anthropic-key form, which read as if Anthropic gated everything else. The Settings page is now three peer cards (GitHub App, Model API key, Plugin health), each with its own status and actions. Each card is independently configurable — the user can save the Anthropic key whether or not the GitHub App is installed, and vice versa. The card-level layering smell that lived in the URL design (`/api/settings/anthropic_key`, `/api/settings/health` exposing GitHub plugin state under a settings namespace) was also fixed: plugin-owned routes now live under `/api/<plugin>/...`. See `plan/milestones/M01-code-review/backend.md` § 2026-05-16.

### GitHub App — Reinstall behavior
The `Reinstall` button on the Settings → GitHub App card **re-runs the install flow**: opens `s.github_app.app_url` in a new tab (the GitHub App's install/configuration page). yaaof's backend doesn't manage install state changes — GitHub does — so the button is an outbound link, not a yaaof-side action.

### Repos — Reconnect behavior
The `Reconnect` button on a non-`active` repo row **re-verifies the GitHub App's access** to that repo:
- Backend endpoint: `POST /api/repos/{id}/reconnect`.
- Behavior: re-issues an installation token, attempts `is_repo_accessible(external_id)` via `vcs`, updates `repos.status` based on the result, returns the new status.
- UI: spinner on the button while in flight, then re-renders the row with the new status. Toast on success or failure.

---

## What stays exactly as the prototype shows

Everything else in the design is locked as-is. Notable bits worth calling out:

- **Sidebar pin + float interaction.** Single primitive, two states; mouse-leave handling exactly as described in `README.md` and `app/shell.jsx`.
- **Live progression of agent cards.** The kinetic feel (running pulse, indeterminate bar, live token/cost counters) is the product's signature moment. Replicate the cadences (1s elapsed / 700ms tokens / 1500ms cost) — back them with SSE in real implementation.
- **Audit-log row expansion to formatted JSON.** Exactly as designed.
- **Onboarding stepper** (3 rows on the dashboard). Exactly as designed.
- **Memory page repo tabs + lesson cards + New-lesson modal.** Exactly as designed (minus `applied_count`).
- **Prompts page tabs + dirty dot + monospaced editor.** Exactly as designed.
- **Repos table.** Exactly as designed (with `Reconnect` defined above).
- **Settings page three cards.** Exactly as designed (with `Reinstall` and `Test connection` / `Rotate key` behaviors as described).
- **Theme tokens.** Keep oklch values; port to Tailwind `theme.extend`.
- **Geist + Geist Mono.** Source via Google Fonts.
- **lucide-react icons.** Substitute 1:1 for the prototype's hand-rolled icons.
- **sonner toasts.** Use as designed.

---

## Endpoints implied by the design (to define when the corresponding screen is built)

Per the M01 plan, endpoints get specified as we encounter them, not all upfront. The list below is the set the design assumes; each is small and lives on the owning module:

- `GET /api/dashboard/metrics` — five (now four) metric tiles + sparkline buckets.
- `GET /api/dashboard/activity` — right-side activity feed.
- `GET /api/dashboard/in-flight` — live-agents card.
- `GET /api/settings/plugin-health` — plugin-health card rows.
- `POST /api/settings/api-key/test` — "Test connection" button.
- `POST /api/settings/api-key/rotate` — "Rotate key" form submit.
- `POST /api/repos/{id}/reconnect` — "Reconnect" button.
- `POST /api/reviewer/jobs/cancel-all?ticket_id=...` — "Cancel jobs" button on ticket header. Wires to `reviewer.cancel_pending(ticket_id)`.
- (No "View prompt" endpoint — button is dropped.)

---

## SSE event taxonomy (to define when the corresponding screen is built)

Per the M01 plan, event kinds get added as their consumers are built. The screens require at minimum:

- `ticket.created`, `ticket.status_changed` — tickets list, dashboard activity.
- `review_job.scheduled`, `review_job.prompt_sent`, `review_job.posted`, `review_job.failed`, `review_job.cancelled` — tickets list pulse, ticket-detail agent cards, audit log, dashboard activity.
- `review_job.step_progress` (a single in-place row update; not an audit entry) — agent card "current step" updates.
- `lesson.created`, `lesson.updated`, `lesson.deleted` — memory page live updates, dashboard activity.
- `repo.status_changed` — repos page live updates.

Define each formally in its owning module's internals doc when it's built.
