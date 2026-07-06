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

## Admin controls

When the current user has at least `admin` role on the org, Active and Draining sections gain:

- **Per-card checkbox** (`workspaces-agent-card-${instance_id}-select`) — toggles the card in/out of the section's selection.
- **Select-all checkbox** in the section header (`workspaces-section-active-select-all` / `workspaces-section-draining-select-all`) — checked/indeterminate/unchecked mirrors the selection; click selects all or deselects all.
- **Bulk-action button** (`workspaces-section-active-shutdown` / `workspaces-section-draining-cancel-shutdown`) — disabled while selection is empty; clicking opens an `AlertDialog` confirm.

Confirmation dialogs — `ShutdownDialog` (`workspaces-shutdown-dialog`, confirm `workspaces-shutdown-dialog-confirm`) and `CancelShutdownDialog` (`workspaces-cancel-shutdown-dialog`, confirm `workspaces-cancel-shutdown-dialog-confirm`). On confirm: mutation fires (`POST /api/orgs/{slug}/agents/shutdown` or `/cancel-shutdown`), toast appears (mixed-outcome copy in [core_api.md § Mutation hooks](core_api.md)), selection clears, `["agents", orgSlug]` invalidated. On error: destructive toast, selection NOT cleared. Unconfigured and Inactive sections have no admin controls.

Role gate: `useHasRole(orgSlug, "admin")` from `@core/api/public/membership`. Selection state and mutations live in `WorkspacesContent`; `AgentSections` receives them as props.

## Testid prefix

`workspaces-*`. Page container: `workspaces-page`. Empty state: `workspaces-empty`. Section headers: `workspaces-section-active`, `workspaces-section-draining`, `workspaces-section-unconfigured`, `workspaces-section-inactive`. Cards: `workspaces-agent-card-${instance_id}`, `workspaces-agent-card-${instance_id}-status`. Admin controls: `workspaces-section-active-select-all`, `workspaces-section-draining-select-all`, `workspaces-section-active-shutdown`, `workspaces-section-draining-cancel-shutdown`, `workspaces-agent-card-${instance_id}-select`, `workspaces-shutdown-dialog`, `workspaces-shutdown-dialog-confirm`, `workspaces-cancel-shutdown-dialog`, `workspaces-cancel-shutdown-dialog-confirm`.

The Settings → Workspaces page is a separate surface at `/org/$slug/settings/workspaces` with testid prefix `org-settings-workspaces-*` — distinct from this page.

## Public interface

- `apps/web/src/domain/workspaces/public/index.tsx` — `WorkspacesPage` (default route component)

## Tests

- `test/AgentSections.test.tsx` — component/Vitest: section partitioning by (state, lifecycle), sort order within sections, hide-empty rule, status-pair label formatting.
- `test/admin-controls.test.tsx` — component/Vitest: admin sees checkboxes + bulk buttons; non-admin does not; select-all and per-card checkbox mechanics; button disabled/enabled states.
- `test/dialogs.test.tsx` — component/Vitest: ShutdownDialog and CancelShutdownDialog copy, confirm callback, cancel dismiss.
- `test/empty-state.test.tsx` — component/Vitest + MSW: EmptyState with CTA renders when configured and zero agents; NotConfiguredBanner renders when unconfigured and zero agents.
- `test/mixed-outcome-toast.test.tsx` — unit/Vitest: `shutdownToastMessage` and `cancelShutdownToastMessage` all-success, mixed, all-no-op outcomes; already_shutdown suffix.
- `apps/e2e/tests/workspaces-agents.spec.ts` — Playwright: navigate to /workspaces, agent card appears via SSE, correct section placement.
- `apps/e2e/tests/workspaces-admin-drain.spec.ts` — Playwright: owner selects active agents, shuts them down, cards move to Draining.
- `apps/e2e/tests/workspaces-admin-cancel-shutdown.spec.ts` — Playwright: owner selects draining agents, cancels shutdown, cards move to Active.
- `apps/e2e/tests/workspaces-builder-readonly.spec.ts` — Playwright: builder sees cards but no checkboxes or buttons.
