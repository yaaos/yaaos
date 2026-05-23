# domain/dashboard

> Landing page for an org-scoped session — 4 stat cards + "In flight" band + "Needs attention" band. The "not configured" gate banner mounts above the stat cards when the org isn't ready.

## Purpose

The `/orgs/:slug/dashboard` route. Single round-trip projection via `useDashboard()` → `GET /api/tickets/dashboard`. The `NotConfiguredBanner` composite reads `/api/orgs/config-status` and renders separately above the stat cards when the org is missing prerequisites.

## Public interface

- `DashboardPage` — mounted by `core/routing` at `/orgs/$slug/dashboard`. Subcomponents (`StatCard`, `BandHeader`, `RowList`, `InFlightRow`, `NeedsAttentionRow`) are private to the same file.

## Module architecture

### File shape

Single `apps/web/src/domain/dashboard/index.tsx` (~220 LOC). Composes the M06 layout primitives (`PageHeader`, `EmptyState`, `NotConfiguredBanner`) + shadcn primitives (`Skeleton`).

### Stat cards (4)

Each `StatCard` carries a label, a numeric value, a lucide icon, and a tone (`info` / `warning` / `success` / `destructive`). The `in_flight` icon spins (`animate-spin`) when the count is > 0. Cards stay visible at 0 — per E2a.3 the empty surface is part of the UX, not hidden.

| Card | Source field | Tone |
|---|---|---|
| In flight | `stats.in_flight` | info (Loader2, spins when > 0) |
| HITL pending | `stats.hitl_pending` | warning (Bell) |
| Completed today | `stats.completed_today` | success (CheckCircle2) |
| Failed today | `stats.failed_today` | destructive (XCircle) |

### "In flight" band

Up to 10 ticket rows. Each row: spinning `Loader2`, ticket title, repo (mono), `ago(updated_at)`. Click → ticket detail. Empty-state when the band is empty (running tickets feed it).

### "Needs attention" band

Up to 5 ticket rows whose computed `m06_status === "done"` AND have at least one medium/high finding. Severity-tinted `AlertCircle` (destructive for high, warning for medium, info for low), title, findings count, repo. Click → ticket detail.

### Not-configured gate

`NotConfiguredBanner` mounts above the stat cards when `useConfigStatus()` reports `configured: false`. Admins see the missing-piece list ("Connect a VCS provider, Configure a coding agent, ..."); Builders see "Ask [admin display name] to finish setup." The Dashboard's own band states still render below — a partially-configured org may still have historical tickets.

### Live updates

- `useDashboard` polls every 5s.
- SSE invalidation (`workflow_state_changed` → invalidate `["tickets", "dashboard"]`) is deferred until the dashboard kinds populate consistently; the 5s poll is the M06 floor.

## Data owned

None. State lives in `core/api` query caches.

## How it's tested

- `apps/web/src/domain/dashboard/test/dashboard.test.tsx` — Vitest smoke-test asserting the loading skeleton renders before the dashboard query resolves.
- E2E coverage of the populated state is deferred — the band logic depends on real ticket data, which is exercised by the PR-review end-to-end spec rather than a per-page dashboard spec.
