# domain/dashboard

> Two-state landing page — onboarding stepper before the system is ready; metric tiles + live agents-in-flight once it is.

## Purpose

The `/dashboard` route. Picks state from `useOnboarding()`: when `github_app_installed` and `anthropic_key_set` are both true the operator sees populated metrics + in-flight reviews; otherwise a step-by-step setup card. Read-only.

## Public interface

- `DashboardPage` — mounted by `core/routing` at `/dashboard`. Subcomponents (`DashboardOnboarding`, `DashboardPopulated`, `MetricTile`, `InFlightRow`) are private to the same file.

## Module architecture

### File shape

Single `index.tsx` (~230 LOC) holding `DashboardPage` (state dispatcher), `DashboardOnboarding`, `DashboardPopulated`, `MetricTile`, `InFlightRow`.

### State dispatch

`DashboardPage` renders `DashboardPopulated` when both onboarding flags are true, otherwise `DashboardOnboarding`. `anthropic_key_set` is authoritative from the backend — true only when the key actually authenticates (or when `YAAOS_CODING_AGENT_STUB=1` short-circuits the probe). A saved-but-invalid key keeps the stepper visible.

### Onboarding state

Single card with two stepper rows (Install GitHub App / Add Anthropic key). Each row: 28px circular avatar (green check when done, numbered grey otherwise), title + subtitle (line-through + muted when done; success-bg tint when done), right side carries a "Done" badge or a primary `<Button>` linking to `/settings`. An aux "Then…" card describes post-onboarding behavior.

### Populated state

Header reads "Overview" (one org in M01).

**Metrics row — 3 tiles** from `useMetricsSummary()` + `useTickets()`:
1. Reviews posted (`total_reviews_posted`, "all-time").
2. Open tickets (count where `status === "in_review"`).
3. Failure rate (`failure_rate * 100`, with `failure_count` failed subtitle).

Cost is not surfaced — CLI pricing data is not authoritative, so the backend doesn't track it.

Sparklines and 24h delta indicators are deferred — `/api/reviewer/metrics` returns lifetime aggregates only.

**Live · in flight panel** — full-width card listing tickets where `status === "in_review"`. Each row is a `<Link to="/tickets/$ticketId">` with PR number + repo + truncated title, updated-ago, and one `yaaos` status badge sourced from the latest review job via `useReviewJobsForTicket(ticket.id)`.

The per-row hook is N+1; TanStack Query dedupes shared keys and the cost is negligible at POC scale. A future `GET /api/dashboard/in-flight` would consolidate. The activity feed from the original design is deferred — `GET /api/dashboard/activity` isn't built.

### Live updates

- `useOnboarding` polls every 5s (no SSE invalidation).
- `useMetricsSummary`, `useTickets`, `useReviewJobsForTicket` invalidate on `review_job_status_changed` and `ticket_status_changed` (see [core_sse.md](core_sse.md)).

## Data owned

None. State lives in `core/api` query caches.

## How it's tested

- `apps/web/src/domain/dashboard/test/dashboard.test.tsx` — Vitest covering state-dispatch with mock `useOnboarding`.
- `apps/e2e/tests/onboarding-stepper.spec.ts` — fresh DB → stepper at 0/2 → paste credentials + dispatch install webhook + save Anthropic key → dashboard flips to populated.
