# domain/workspaces

> Live fleet status for the org — workspace agents grouped by lifecycle and liveness.

## Scope

`/org/:slug/workspaces`. Default post-auth landing page. Reads `useAgents(slug)` → `GET /api/orgs/{slug}/agents` and `useConfigStatus()` → `GET /api/orgs/config-status`. Owns no data.

## Layout

- **Four sections** — Active / Draining / Unconfigured / Inactive. Sections with zero agents are hidden. Cards sort by `last_heartbeat_at desc NULLS LAST` within each section.
- **Section membership rules** (using `AgentRow` field names):
  - Active: `state != "offline" AND lifecycle == "active"`
  - Draining: `state != "offline" AND lifecycle == "draining"`
  - Unconfigured: `state != "offline" AND lifecycle == "unconfigured"`
  - Inactive: `state == "offline" OR lifecycle == "shutdown"`
- **Per-card status pair** — `<state> / <lifecycle>` e.g. "Online / Active", "Stale / Draining". State display strings: Online / Stale / Offline. Lifecycle display strings: Unconfigured / Active / Draining / Shutdown.
- **`NotConfiguredBanner`** — renders only when `configStatus.configured === false` AND `agents.length === 0`. When any agents exist, the agents themselves communicate setup state.
- **EmptyState** — renders when `agents.length === 0` AND `configStatus.configured === true`. Single CTA links to `/org/$slug/settings/workspaces`.

## Live updates

Pure SSE — no polling. `agent_changed` SSE events invalidate `["agents", orgSlug]`. On every reconnect, `onopen` reconciles by invalidating `["agents"]`. Heartbeat-driven `claimed_workspace_count` updates surface within ~5s of an in-flight workspace completing (drain cadence).

## Suspense / error boundary

`WorkspacesPage` wraps `WorkspacesContent` in `<ErrorBoundary>` + `<Suspense>`. `useAgents` is a `useSuspenseQuery` hook — no `isLoading` branch in the render tree.

## Testid prefix

`workspaces-*`. Page container: `workspaces-page`. Empty state: `workspaces-empty`. Section headers: `workspaces-section-active`, `workspaces-section-draining`, `workspaces-section-unconfigured`, `workspaces-section-inactive`. Cards: `workspaces-agent-card-${instance_id}`, `workspaces-agent-card-${instance_id}-status`.

The Settings → Workspaces page is a separate surface at `/org/$slug/settings/workspaces` with testid prefix `org-settings-workspaces-*` — distinct from this page.

## Public interface

- `apps/web/src/domain/workspaces/public/index.tsx` — `WorkspacesPage` (default route component)

## Tests

- `test/AgentSections.test.tsx` — component/Vitest: section partitioning by (state, lifecycle), sort order within sections, hide-empty rule, status-pair label formatting.
- `apps/e2e/tests/workspaces-agents.spec.ts` — Playwright: navigate to /workspaces, agent card appears via SSE, correct section placement.
- `apps/e2e/tests/workspaces-empty-state.spec.ts` — Playwright: empty-state EmptyState renders with CTA when org has zero agents and is configured.
