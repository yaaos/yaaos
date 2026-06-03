# core/layout

> App shell — sidebar mount, route outlet, theme tokens, broken-integrations banner.

## Purpose

Wraps every signed-in page in the sidebar-only shell mandated by [design.md § Layout](design.md#layout). Owns the theme switcher (`data-theme` on `<html>`) and the in-page banner that surfaces broken integrations across the top of `<main>`. The sidebar itself lives in [core/sidebar](../src/core/sidebar/) and is composed into the shell here.

## Public interface

Files under `core/layout/public/`, imported directly via `@core/layout/public/<file>`:

- `public/app-shell.tsx` — `AppShell`, root-route component (see [core_routing.md](core_routing.md)).
- `public/theme-context.tsx` — `ThemeProvider`, `useThemeContext()` (`{ theme: "light"|"dark", setTheme }`). Throws if called outside the provider.
- `public/theme.ts` — `applyStoredTheme`, `toggleTheme`, `getStoredTheme`, `setStoredTheme` — imperative helpers; used by `ThemeProvider` and the user-card theme toggle.
- `public/not-configured-banner.tsx` — `NotConfiguredBanner` — org-gate banner; reads `useConfigStatus()` + `useCurrentUser()`; imported by domain pages that surface it above their content.

Private (non-`public/`): `broken-integrations-banner.tsx` (internal to `AppShell`).

## Module architecture

Files: `public/app-shell.tsx`, `public/theme.ts`, `public/theme-context.tsx`, `broken-integrations-banner.tsx`. Sidebar is its own module at `src/core/sidebar/`.

### `AppShell`

Two-column flex: fixed-width sidebar + flex-grow `<main>`. Only `<main>` scrolls. No top bar — see [design.md § Principles](design.md#principles). `BrokenIntegrationsBanner` renders above `<main>` when any required integration is unhealthy.

`STANDALONE_PATHS` (`/login`, `/user`, `/orgs`) render `<Outlet>` without the shell.

**Sidebar Suspense boundary:** `<Sidebar>` is wrapped in `<Suspense fallback={<SidebarSkeleton />}>`. `UserCard` and `RequireMembership` both call `useSuspenseQuery` (`["auth","me"]`); without this boundary a cold cache on a deep-link would let the thrown promise bubble to the root error boundary and flash "Something went wrong" instead of a skeleton. `SidebarSkeleton` (defined in `app-shell.tsx`, `data-testid="sidebar-loading"`) renders the rail-width column placeholder while the query resolves.

**Focus reset:** a `useEffect` keyed on `pathname` (from `useRouterState`) moves keyboard focus to the first `<h1>` inside `<main>` (or to `<main>` itself when none exists) on every route change. `<main>` carries `tabIndex={-1}` + `outline-none` so programmatic focus works without entering the natural tab order. Screen-reader users hear the new page's title immediately; keyboard users land at the content start. See [core_routing.md § Focus reset](core_routing.md#focus-reset-on-navigation).

### Theme system

Two layers, same source of truth:

- `theme.ts` — imperative layer: reads/writes `localStorage` (`yaaos:theme`) and sets `[data-theme]` on `<html>`. Called directly at boot (`applyStoredTheme()` in `main.tsx`) and when the user toggles the theme.
- `theme-context.tsx` — React layer: `ThemeProvider` initializes state from the stored value (OS preference fallback), exposes `{ theme, setTheme }` via `useThemeContext()`. The Sonner `<Toaster>` reads this context — fixing the stuck-at-`"system"` drift bug from the unmounted `next-themes` provider.

`theme` is always `"light"` or `"dark"` — never `"system"`. Token vocabulary: [design.md § Design tokens](design.md#design-tokens).

### `BrokenIntegrationsBanner`

Reads `useBrokenIntegrations()`; shows one row per broken integration with a deep link to settings. Hidden when everything is healthy.

## Data owned

None — `BrokenIntegrationsBanner` reads from a query hook; `theme.ts` reads/writes `localStorage` only.

## How it's tested

Rendered on every e2e test — shell breakage shows up as page-navigation failures. Sidebar unit tests in `core/sidebar/test/`. Focus-reset behavior has unit/integration tests in `core/layout/test/app-shell-focus-reset.test.tsx` and e2e coverage in `apps/e2e/tests/focus-reset.spec.ts`. Sidebar Suspense boundary regression tested in `core/layout/test/app-shell-sidebar-suspense.test.tsx`.
