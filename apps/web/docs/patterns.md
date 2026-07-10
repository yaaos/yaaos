# Frontend patterns

Cross-app conventions (UTC on the wire, audit-log shape) live in [`docs/system-architecture.md`](../../../docs/system-architecture.md). Layer model and cross-cutting wiring: [architecture.md](architecture.md).

## Module shape

A module's **public surface is its `public/` directory**. Everything else in the module is private and unreachable across module boundaries (enforced — see Imports). Privacy is by location: there is no `internal/` folder or naming marker — a file is private unless it sits in `public/`. To expose something, move it into `public/` (a deliberate, reviewable act).

- **Domain module** — the route entry and anything other modules consume live in `public/` (`public/index.tsx`, or `public/<Page>.tsx`). Private subcomponents/hooks stay at the module root. Split into `list/` + `detail/` (plus `_shared.tsx`) only when a file exceeds ~1500 lines. Optional `api.ts` for module-local TanStack hooks (private unless re-exported from `public/`).
- **Core module** — public files under `public/`, private implementation file(s) at the module root, optional `types.ts`.

## Imports

Absolute only via path aliases (`@core/...`, `@domain/...`, `@shared/...`). Cross-module imports must target the module's `public/` surface directly (e.g. `@core/api/public/client`); a module's non-`public/` files are private and cannot be imported from another module. No re-export barrels. The architectural rules are enforced in `bin/ci` via `apps/web/.dependency-cruiser.cjs` at `error` severity — violations fail the build.

## Hook placement and naming

| Hook type | Location | Suffix rule |
|---|---|---|
| Server-state (TanStack Query) | `core/api/queries.ts` | `use<Resource>` |
| Module logic (derived state, callbacks, local state) | `domain/<module>/use-<thing>.ts` | `use-<thing>.ts` (kebab file, camel export) |
| Shared cross-module | `shared/hooks/public/` | `use<Thing>` |

- Logic hooks (`use-*.ts`) return data, state, and callbacks — **never JSX**. File extension is `.ts`, not `.tsx`. A `find src -name "use-*.tsx"` glob in `bin/ci` enforces this.
- Server hooks use `useSuspenseQuery` — callers never see `isLoading`. Loading handled by `<Suspense>` fallbacks.

## Suspense and error boundaries

Every data-fetching domain component renders under `<Suspense>` + `<ErrorBoundary>` from `react-error-boundary`:

- `<Suspense>` fallback: `<Skeleton>` skeletons sized to the eventual content.
- `<ErrorBoundary>` fallback: `<ErrorBanner message="Couldn't load …" onRetry={resetErrorBoundary} />` from `@shared/components/public/layout/error-banner`.
- Both components live in the same `index.tsx`. `<ErrorBoundary>` wraps `<Suspense>`.
- Mutations (writes) are not wrapped — they expose `isPending`/`isError` to the caller.

See `domain/notifications/` for the reference implementation.

## MSW testing strategy

Tests that cross `core/api` use MSW (`msw/node`) rather than `vi.mock`. Three-tier mapping:

| Tier | When to use |
|---|---|
| Unit (Vitest, no network) | Pure logic in one module — no React, no hooks |
| Component/integration (Vitest + RTL + MSW) | React component trees with real `QueryClient`; MSW intercepts HTTP |
| E2e (Playwright) | Full browser flow — SSE, OAuth, cookies, navigation |

MSW infra lives in `src/test/msw/`:
- `server.ts` — `setupServer()` export; global start/reset/stop in `src/test-setup.ts`.
- `handlers/<domain>.ts` — per-domain handlers typed against `core/api` types; drift = compile error.

Use `vi.useFakeTimers({ toFake: ["Date"] })` (not full fake timers) when tests need fixed `Date` — full fake timers block MSW/Promise resolution.

## testid conventions

- Page container: `<page>-<state>` (e.g. `workspaces-page`, `ticket-detail`).
- List containers: `<entity>-list` (`tickets-list`, `lessons-list`, `audit-log`).
- List rows: `<entity>-row-<id>`.
- Actions: `<action>-<entity>` (`cancel-jobs-button`, `lesson-save`, `teach-yaaos`).
- Status badges: `<entity>-status` (`github-status`, `apikey-status`).
- Form fields: `<form>-<field>` (`gh-app-id`, `anthropic-key`, `teach-title`).
- Review cards carry `data-state="<status>"` — query via `[data-testid^="agent-card-"][data-state="posted"]`.
- Workspaces page prefix: `workspaces-*`. Sections: `workspaces-section-active`, `workspaces-section-draining`, `workspaces-section-unconfigured`, `workspaces-section-inactive`. Cards: `workspaces-agent-card-${instance_id}`, `workspaces-agent-card-${instance_id}-status`. Empty state: `workspaces-empty`. See [domain_workspaces.md](domain_workspaces.md).
- Ticket page tabs: `ticket-tab-<overview|runs|artifacts>`; tab bodies: `ticket-overview`, `ticket-runs`, `ticket-artifacts`. Overview's state card is always `attention-block` with `data-state="<paused|in_flight|<terminal-state>>"`, regardless of which branch rendered it. Pause actions: `approve-run`, `instruct-run`, `send-back-run`, `kill-run`. Overview in-flight card: `overview-live-ticker` (appears only when at least one live activity frame has arrived; shows the most recent frame's `message`; clicking switches to Runs tab). Overview terminal card and Runs tab card summary both carry `rerun-run` (on a `failed`/`cancelled`/`killed` run — opens `ConfirmModal` then `POST /api/pipelines/runs/{run_id}/rerun`; the Runs-tab button's `onClick` prevents the card's `<details>` accordion from toggling). Runs tab: `run-card-${run_id}` (`data-state="<run.state>"`), `stage-row-${stage_name}`, `stage-activity-toggle-${stage_name}` (Activity accordion toggle button), `stage-activity-live` (live-tail pane while stage is running; also carries `data-connected="true"|"false"` — `"true"` once `EventSource.onopen` fires), `activity-event-row` (each activity frame row in the live or persisted pane), `rerun-from-stage`. Artifacts tab: `artifact-lineage-${stage_name}`. See [domain_tickets.md](domain_tickets.md).
- Pipelines settings page: `pipelines-list` (Accordion), `pipelines-download-skills` (anchor download of the shipped skills bundle), `pipeline-row-${id}`, `pipeline-new`, `pipeline-new-from-template`, `pipeline-new-card`, `pipeline-name`, `pipeline-description`, `pipeline-save`, `pipeline-delete`, `pipeline-add-stage` (+ `-skill`/`-review`/`-action`/`-call`), `pipeline-stage-row-${key}` (`key` is a client-only id, not the stage's server id), `pipeline-stage-edit-${key}`, `pipeline-stage-menu-${key}` (+ `-move-up-${key}`/`-move-down-${key}`/`-remove-${key}`), `pipeline-template-dialog`. Per-kind stage editor Sheet: `stage-editor`, `stage-name`, `stage-skill-name`, `stage-agent`, `stage-model`, `stage-effort`, `stage-review-enabled`, `stage-boundary-mode`, `stage-boundary-on-blocker`, `stage-boundary-on-should-fix`, `stage-boundary-on-nit`, `stage-boundary-on-protected`, `stage-boundary-confidence`, `stage-editor-save`. See [domain_pipeline_settings.md](domain_pipeline_settings.md).
- Repos settings page path-set rows (in `ProtectedCodeSection.tsx`): `repo-path-set-row-${id}` (one per set), `repo-path-set-edit-${id}` (Edit button), `repo-path-set-delete-${id}` (Delete button), `repo-path-set-delete-confirm` (delete AlertDialog), `repo-path-set-delete-confirm-action` (red Delete action), `repo-path-set-editor` (Sheet), `repo-path-set-name` (name Input inside Sheet), `repo-path-set-editor-save` (Sheet Save button), `repo-path-set-globs-${id}` (glob Textarea), `repo-path-set-owners-${id}` (owners multi-select). See [domain_repo_settings.md](domain_repo_settings.md).

## Accessibility (WCAG 2.2 AA)

Target: WCAG 2.2 AA on all shipped pages.

- **Native-first markup.** Prefer `<button>`, `<dialog>` (`showModal()`), `<details>`, and `<nav>` in our code. Radix/shadcn primitives may hand-roll ARIA (vendor carve-out — don't override).
- **One `<h1>` per page.** Heading levels must be ordered (`h1 → h2 → h3`) — never skip a level.
- **Landmarks.** Every page has `<main>`, `<nav>` (sidebar), and `<header>`/`<footer>` where appropriate.
- **Focus-visible.** Global `*:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px }` in `src/styles.css`. Never suppress with `outline: none` unless immediately replaced by a visible custom style.
- **Focus-reset on navigation.** `AppShell` moves focus to the first `<h1>` inside `<main>` (or to `<main>` itself when no `<h1>` is present) on every route change. `<main>` carries `tabIndex={-1}` and `outline-none` so programmatic focus works without adding to the tab order. When `<h1>` doesn't already carry a `tabindex` attribute, the shell injects `tabindex="-1"` at route-change time so the heading is programmatically focusable; the attribute persists across that route's lifetime (benign — `tabindex=-1` keeps headings out of the tab order while remaining reachable via `.focus()`). Page authors do not need to add `tabindex` themselves. See [core_layout.md](core_layout.md).
- **Color is never the sole meaning carrier.** Status chips pair color with icon + label.
- **Icons.** Decorative: `aria-hidden="true"`. Meaningful (icon-only buttons): `aria-label` or `title`.
- **`aria-pressed` on toggle buttons.** Status filter chips carry `aria-pressed={active}`.
- **`aria-label` on unlabeled inputs.** Search inputs without a visible `<label>` carry `aria-label`.

**Enforcement:**
- Biome `a11y` group: all rules at `error` severity (`apps/web/biome.json`). Fails `bin/ci`.
- Runtime axe-core (`@axe-core/react`): loaded in dev builds only; logs violations to the browser console.
- `@axe-core/playwright` in `apps/e2e/tests/accessibility.spec.ts`: WCAG 2.1 AA sweep on anchor pages.

**Biome a11y coverage note:** Biome 1.9.4 ports the majority of `eslint-plugin-jsx-a11y` rules. Known gaps (not in Biome): `aria-required-children`, `aria-required-parent`, `no-access-key` (covered by `noAccessKey`). Runtime axe-core is the backstop for anything Biome doesn't catch statically.

## Tooling

| Concern | Tool |
|---|---|
| Lint + format | Biome (`apps/web/biome.json`) |
| API type drift | `bin/gen-api-types --check` — regenerate from `apps/backend/openapi/web-api.json` + compare against the committed file; gating stage before `tsc` |
| Type check | `tsc --noEmit` |
| Unit/integration tests | Vitest + RTL + MSW |
| Boundary lint | dependency-cruiser (`apps/web/.dependency-cruiser.cjs`, error — fails `bin/ci`) |
| Dead-code lint | knip (`apps/web/knip.json`) — unused files / exports / dependencies; fails `bin/ci` |
| Build | Vite |
| Bundle report | rollup-plugin-visualizer → `tmp/bundle-stats.html` after every build (non-gating) |

`apps/web/bin/ci` runs all steps. Bundle report is informational — CI does not fail on chunk size. A `use-*.tsx` glob check enforces that logic hooks are always `.ts` not `.tsx`. Generated types come from the committed backend artifact — no running backend needed for CI.

## Module documentation

Every shipped module has one `apps/web/docs/<layer>_<module>.md` with this fixed structure:

1. **Purpose** — what the module owns; what it doesn't.
2. **Public interface** — the files in `public/`. No internals.
3. **Module architecture** — Entities · Key value objects · Core user flows · State machines (omit if none; `from → to` notation).
4. **Data owned** — query keys, notable local state.
5. **How it's tested** — e2e coverage.

Terse, bullets, no code snippets, no `Decisions` section, link don't repeat.

## Auth + tenancy

- `apiFetch` auto-injects `X-Yaaos-Org-Slug` from `core/api/org-context.ts`. Domain hooks are org-agnostic at the call site.
- UI role gates go through one primitive in `@core/api/public/membership` (single `Role` + `ROLE_RANK`): `<RequireMembership orgSlug="..." minRole="admin">` for render-gating, `useHasRole(slug, minRole)` / `useMembership(slug)` for boolean checks. Never hand-roll `memberships.find(...)` + a role compare in a component. All of it is a UI hint only — backend `require(action)` is the authority.
- Every domain page is under `/org/$slug/...`. The `/` route probes `/api/auth/me` and redirects.

## Sidebar nav config

- `core/sidebar/nav-config.ts` defines the `NavConfig` type (`link | group`). Route paths are relative (`/workspaces`); renderer prefixes `/org/{slug}`.
- `role: "admin"` on a link or group hides it for non-admins; a group disappears when no child survives the filter.
- Collapse state lives in `localStorage` via `use-collapse-state.ts`, syncs across tabs. A group is expanded only while a child is the active route; navigating away auto-collapses it.
- Rail-mode groups open a right-anchored Popover. Active items use `bg-accent` only — no layout shift on select.

## Org Settings shell

- `OrgSettingsLayout` (`shared/components/public/layout/org-settings-layout.tsx`) is a passthrough `<div>` — no top chrome, no tab bar. Per-page role gating in each settings page. Shared across `domain/org_settings` and `domain/pipeline_settings` (rule-of-three graduation — see [components.md](components.md)).
- Coding-agent plugin settings dispatch through `apps/web/src/domain/org_settings/coding_agents/plugin_registry.ts`. First-party plugins register at module load via side-effect import (`claude_code`); unregistered plugins get the built-in placeholder.
- VCS empty-state: "Connect GitHub" card — single CTA fires `useStartGithubInstall`. Coding Agents install card: "Add Claude Code" button — installs directly via `useInstallCodingAgent`.

## Dumb frontend

The SPA renders data and dispatches actions — it owns no rules the backend doesn't also enforce.

- Verdicts, statuses, counts, permissions: server-supplied. Never derived client-side.
- Cache invalidation: driven by mutation responses and SSE events. No client heuristics.
- Client-side filter/sort: fine for UX over a fetched list. Any operation that changes which rows the user *acts on* goes through the API.

If a FE change could alter stored/posted/counted state without a corresponding API change, the logic is in the wrong place.

## Query keys

Module-scoped arrays. Canonical keys:

- `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`
- `["runs", ticket_id]` — every pipeline run for a ticket, newest first, with stage-execution lists (Runs tab; invalidated by `run_state_changed`/`stage_state_changed` SSE)
- `["runs", "overview", ticket_id]` — server-computed Overview-tab payload, tagged `paused | in_flight | terminal` (invalidated by `run_state_changed` SSE)
- `["runs", "stage-activity", run_id, stage_execution_id]` — persisted coding-agent activity blob for one stage execution (lazy-loaded in the Runs tab's per-row accordion)
- `["artifacts", ticket_id]` — every artifact lineage for a ticket, grouped by stage name (Artifacts tab; invalidated by `artifact_stored` SSE)
- `["artifacts", "version", artifact_id]` — one artifact version, body included
- `["reviewer", "metrics"]`, `["reviewer", "agents"]`
- `["agents", orgSlug]` — slug-scoped so different orgs don't share the entry
- `["lessons", repos, q, created_by, sort]` — each field is the filter value or `"all"` / `""` as default
- `["github", "installation"]`, `["github", "repositories"]`
- `["plugin-health", pluginId]`
- `["onboarding"]`
- `["notifications", readState]`, `["notifications", "popover"]`
- `["pipelines"]`, `["pipelines", id]` — org pipeline-definition list + one full definition (Pipelines settings page)
- `["pipeline-templates"]` — the shipped, code-defined pipeline templates ("New from template" picker)
- `["actions"]` — registered control-plane actions (Pipelines settings page's "Add stage" → action picker)
- `["coding-agents"]`, `["claude-code", "defaults"]` — installed coding agents + `claude_code`'s model/effort dropdown values (Pipelines settings page's stage editor)
- `["repos"]`, `["repos", repoExternalId]` — installed-repo accordion list + one repo's full config (Repos settings page)
- `["intake-points"]` — registered intake points (Repos settings page's trigger-binding intake picker)
- `["org-members"]` — active org members (Repos settings page's notify/owner multi-selects)

Mutations and the SSE subscriber ([core_sse.md](core_sse.md)) invalidate exactly the keys they affect.

## Time and dates

Backend emits ISO-8601 UTC (`Z`). FE renders in browser local timezone via `apps/web/src/shared/utils/public/ago.ts`:

- `ago(ts)` — relative duration (`"12s ago"`).

Anti-pattern: `new Date(ts).toISOString()` — always UTC, never use for display.

## API client

One surface in `core/api/public/client.ts`: `apiFetch<T>` (generic helper, throws on non-2xx). Every hook wraps it. Full surface: [core_api.md](core_api.md).

## Error handling at the API boundary

- **Mutations** — expose `isPending`/`isSuccess`/`isError`; forms show inline "Saving…" / "Saved." / red error text.
- **Queries** — `useSuspenseQuery`; `<ErrorBoundary>` catches thrown errors; `<Suspense>` shows skeletons while pending.
- **Validation errors** — 4xx field-keyed map surfaces under the relevant input.

## Observability

Rule: every `catch` block that swallows or transforms an error MUST call `recordException(err)` from `@core/observability/public/sdk` before silently dropping the error, rethrowing, or surfacing UI feedback. Never silent-catch without either `recordException` or a deliberate rethrow.

Grep recipe — audit all catch sites:
```
rg "\.catch\(|catch \(" apps/web/src --type ts
```

Sanctioned exceptions (catches that do NOT need `recordException`):
- `sdk.ts:204` — `_resetObservabilityForTests` ignores shutdown errors in tests only (not shipped code).
- `queries.ts:52` and `queries.ts:104` — conditional 401-rethrows (`if message.startsWith("401") return null`). A 401 is a routing signal (`handleAuthFailure` redirects to `/login`), not a client error; recording it as an exception would generate noise.

Any new catch site that isn't a routing signal must call `recordException`.

## Code style

- Function components only. Hooks for shared logic; no HOCs unless forced by a library.
- TanStack Query for server state — no `useEffect(() => fetch(...))`.
- Tailwind v4 only. CSS-first token definitions via `@theme` in `src/styles.css`; tokens are
  referenced as Tailwind utilities (`bg-background`, `text-foreground`, etc.) without a JS
  config file. The oklch semantic values live in `@layer base` custom properties.
- Theming is one system: `core/layout/ThemeProvider` + `useThemeContext` back the `data-theme`
  toggle and feed the Sonner toast's `theme` prop. No `next-themes`.
- `cva` (class-variance-authority) + `cn` (tailwind-merge + clsx) for variant classes on
  primitives. Container-query variants (`@container`, `@sm:`, `@lg:`) are available natively
  in Tailwind v4 — prefer them over media queries for component-level breakpoints.
- `tsc` strict — warnings are CI errors.
- Forms: `react-hook-form` + `zod` for validated forms. Plain React state for simple filters/toggles.
