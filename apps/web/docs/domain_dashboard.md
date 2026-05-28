# domain/dashboard

> Landing page for an org session — stat cards, in-flight band, needs-attention band.

## Scope

`/orgs/:slug/dashboard`. One query: `useDashboard()` → `GET /api/tickets/dashboard`. `NotConfiguredBanner` reads `GET /api/orgs/config-status` separately. Owns no data.

## Layout

- **4 stat cards** — In flight (spins when > 0) · HITL pending · Completed today · Failed today.
- **In flight band** — up to 10 running tickets (title, repo, age). Click → detail.
- **Needs attention band** — up to 5 done tickets with ≥1 medium/high finding. Click → detail.
- **`NotConfiguredBanner`** — mounts above cards when `configured: false`. Admins see missing-piece list; Builders see "Ask [admin] to finish setup." Bands still render so historical tickets remain visible.

## Live updates

`useDashboard` polls every 5 s. SSE invalidation (`workflow_state_changed`) is deferred; the poll is the floor.

## Tests

`test/dashboard.test.tsx` — loading skeleton smoke test. Populated state covered by PR-review e2e.
