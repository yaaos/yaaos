# Frontend patterns

Cross-app conventions (UTC on the wire, audit-log shape) live in [`docs/system-architecture.md`](../../../docs/system-architecture.md). Layer model and cross-cutting wiring: [architecture.md](architecture.md).

## Module shape

- **Domain module** — collapses page + local subcomponents into one `index.tsx`. Split into `list/` + `detail/` (plus `_shared.tsx`) only when a file exceeds ~1500 lines. Optional `api.ts` for module-local TanStack hooks.
- **Core module** — `index.ts` (re-exports), implementation file(s), optional `types.ts`.

## Imports

Absolute only via path aliases (`@core/...`, `@domain/...`, `@shared/...`). Only what's exported from `index.ts(x)` — no deep imports across module boundaries (barrel-only). The architectural rules are enforced in `bin/ci` via `apps/web/.dependency-cruiser.cjs` at `error` severity — violations fail the build.

## Hook placement and naming

| Hook type | Location | Suffix rule |
|---|---|---|
| Server-state (TanStack Query) | `core/api/queries.ts` | `use<Resource>` |
| Module logic (derived state, callbacks, local state) | `domain/<module>/use-<thing>.ts` | `use-<thing>.ts` (kebab file, camel export) |
| Shared cross-module | `shared/hooks/` | `use<Thing>` |

- Logic hooks (`use-*.ts`) return data, state, and callbacks — **never JSX**. File extension is `.ts`, not `.tsx`. A `find src -name "use-*.tsx"` glob in `bin/ci` enforces this.
- Server hooks use `useSuspenseQuery` — callers never see `isLoading`. Loading handled by `<Suspense>` fallbacks.

## Suspense and error boundaries

Every data-fetching domain component renders under `<Suspense>` + `<ErrorBoundary>` from `react-error-boundary`:

- `<Suspense>` fallback: `<Skeleton>` skeletons sized to the eventual content.
- `<ErrorBoundary>` fallback: `<ErrorBanner message="Couldn't load …" onRetry={resetErrorBoundary} />` from `@shared/components/layout`.
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
- `handlers/<domain>.ts` — per-domain handlers typed against `@core/api` types; drift = compile error.

Use `vi.useFakeTimers({ toFake: ["Date"] })` (not full fake timers) when tests need fixed `Date` — full fake timers block MSW/Promise resolution.

## testid conventions

- Page container: `<page>-<state>` (e.g. `dashboard-onboarding`, `ticket-detail`).
- List containers: `<entity>-list` (`tickets-list`, `findings-list`, `lessons-list`, `audit-log`).
- List rows: `<entity>-row-<id>`.
- Actions: `<action>-<entity>` (`rereview-button`, `cancel-jobs-button`, `lesson-save`, `teach-yaaos`).
- Status badges: `<entity>-status` (`github-status`, `apikey-status`).
- Form fields: `<form>-<field>` (`gh-app-id`, `anthropic-key`, `teach-title`).
- Review cards carry `data-state="<status>"` — query via `[data-testid^="agent-card-"][data-state="posted"]`.

## Accessibility (WCAG 2.2 AA)

Target: WCAG 2.2 AA on all shipped pages.

- **Native-first markup.** Prefer `<button>`, `<dialog>` (`showModal()`), `<details>`, and `<nav>` in our code. Radix/shadcn primitives may hand-roll ARIA (vendor carve-out — don't override).
- **One `<h1>` per page.** Heading levels must be ordered (`h1 → h2 → h3`) — never skip a level.
- **Landmarks.** Every page has `<main>`, `<nav>` (sidebar), and `<header>`/`<footer>` where appropriate.
- **Focus-visible.** Global `*:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px }` in `src/styles.css`. Never suppress with `outline: none` unless immediately replaced by a visible custom style.
- **Focus-reset on navigation.** `AppShell` moves focus to the first `<h1>` inside `<main>` (or to `<main>` itself when no `<h1>` is present) on every route change. `<main>` carries `tabIndex={-1}` and `outline-none` so programmatic focus works without adding to the tab order. See [core_layout.md](core_layout.md).
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
| Type check | `tsc --noEmit` |
| Unit/integration tests | Vitest + RTL + MSW |
| Boundary lint | dependency-cruiser (`apps/web/.dependency-cruiser.cjs`, error — fails `bin/ci`) |
| Build | Vite |
| Bundle report | rollup-plugin-visualizer → `tmp/bundle-stats.html` after every build (non-gating) |

`apps/web/bin/ci` runs all steps. Bundle report is informational — CI does not fail on chunk size. A `use-*.tsx` glob check enforces that logic hooks are always `.ts` not `.tsx`.

## Module documentation

Every shipped module has one `apps/web/docs/<layer>_<module>.md` with this fixed structure:

1. **Purpose** — what the module owns; what it doesn't.
2. **Public interface** — exports from `index.ts(x)`. No internals.
3. **Module architecture** — Entities · Key value objects · Core user flows · State machines (omit if none; `from → to` notation).
4. **Data owned** — query keys, notable local state.
5. **How it's tested** — e2e coverage.

Terse, bullets, no code snippets, no `Decisions` section, link don't repeat.

## Auth + tenancy

- `apiFetch` auto-injects `X-Org-Slug` from `core/api/org-context.ts`. Domain hooks are org-agnostic at the call site.
- Use `<RequireMembership orgSlug="..." role="admin">` for UI role gates — hint only; backend `require(action)` is the authority.
- Every domain page is under `/orgs/$slug/...`. The `/` route probes `/api/auth/me` and redirects.

## Sidebar nav config

- `core/sidebar/nav-config.ts` defines the `NavConfig` type (`link | group`). Route paths are relative (`/dashboard`); renderer prefixes `/orgs/{slug}`.
- `role: "admin"` on a link or group hides it for non-admins; a group disappears when no child survives the filter.
- Collapse state lives in `localStorage` via `use-collapse-state.ts`, syncs across tabs. A group is expanded only while a child is the active route; navigating away auto-collapses it.
- Rail-mode groups open a right-anchored Popover. Active items use `bg-accent` only — no layout shift on select.

## Org Settings shell

- `OrgSettingsLayout` is a passthrough `<div>` — no top chrome, no tab bar. Per-page role gating in each settings page.
- Coding-agent plugin settings dispatch through `apps/web/src/domain/org_settings/coding_agents/plugin_registry.ts`. First-party plugins register at module load via side-effect import (`claude_code`); unregistered plugins get the built-in placeholder.
- `PluginPicker` (`shared/plugin_picker/`) is shared between the VCS empty-state and Coding Agents Add flow. Backed by `useAvailablePlugins(type)` → `GET /api/plugins/available?type=...`.

## Dumb frontend

The SPA renders data and dispatches actions — it owns no rules the backend doesn't also enforce.

- Verdicts, statuses, counts, permissions: server-supplied. Never derived client-side.
- Cache invalidation: driven by mutation responses and SSE events. No client heuristics.
- Client-side filter/sort: fine for UX over a fetched list. Any operation that changes which rows the user *acts on* goes through the API.

If a FE change could alter stored/posted/counted state without a corresponding API change, the logic is in the wrong place.

## Query keys

Module-scoped arrays. Canonical keys:

- `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`
- `["reviewer", "jobs", ticket_id]`, `["reviewer", "metrics"]`, `["reviewer", "agents"]`
- `["lessons", repos, q, created_by, sort]` — each field is the filter value or `"all"` / `""` as default
- `["github", "installation"]`, `["github", "repositories"]`
- `["plugin-health", pluginId]`
- `["onboarding"]`, `["health"]`
- `["notifications", readState]`, `["notifications", "popover"]`

Mutations and the SSE subscriber ([core_sse.md](core_sse.md)) invalidate exactly the keys they affect.

## Time and dates

Backend emits ISO-8601 UTC (`Z`). FE renders in browser local timezone via `apps/web/src/shared/utils/ago.ts`:

- `ago(ts)` — relative duration (`"12s ago"`).
- `formatTime(ts)` — local `HH:MM:SS` (audit-log rows).
- `formatDateTime(ts)` — full local date + time.

Anti-pattern: `new Date(ts).toISOString()` — always UTC, never use for display.

## API client

Two surfaces in `core/api/client.ts`: `apiClient` (typed `openapi-fetch`) and `apiFetch<T>` (generic helper, throws on non-2xx). Every hook wraps one of these. Full surface: [core_api.md](core_api.md).

## Error handling at the API boundary

- **Mutations** — expose `isPending`/`isSuccess`/`isError`; forms show inline "Saving…" / "Saved." / red error text.
- **Queries** — `useSuspenseQuery`; `<ErrorBoundary>` catches thrown errors; `<Suspense>` shows skeletons while pending.
- **Validation errors** — 4xx field-keyed map surfaces under the relevant input.

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
