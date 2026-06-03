# core/layout

> App shell — sidebar mount, route outlet, theme tokens, broken-integrations banner.

## Purpose

Wraps every signed-in page in the sidebar-only shell mandated by [design.md § Layout](design.md#layout). Owns the theme switcher (`data-theme` on `<html>`) and the in-page banner that surfaces broken integrations across the top of `<main>`. The sidebar itself lives in [core/sidebar](../src/core/sidebar/) and is composed into the shell here.

## Public interface

- `AppShell` — root-route component (see [core_routing.md](core_routing.md)).
- `ThemeProvider` — React context provider; mount once at app root (inside `<React.StrictMode>`).
- `useThemeContext()` — `{ theme: "light"|"dark", setTheme }`. Throws if called outside the provider.
- `applyStoredTheme`, `toggleTheme`, `getStoredTheme`, `setStoredTheme` — imperative helpers from `theme.ts`; mostly used by `ThemeProvider` and the user-card theme toggle.

## Module architecture

Files: `app-shell.tsx`, `theme.ts`, `theme-context.tsx`, `broken-integrations-banner.tsx`, `index.ts`. Sidebar is its own module at `src/core/sidebar/`.

### `AppShell`

Two-column flex: fixed-width sidebar + flex-grow `<main>`. Only `<main>` scrolls. No top bar — see [design.md § Principles](design.md#principles). `BrokenIntegrationsBanner` renders above `<main>` when any required integration is unhealthy.

`STANDALONE_PATHS` (`/login`, `/user`, `/orgs`) render `<Outlet>` without the shell.

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

Rendered on every e2e test — shell breakage shows up as page-navigation failures. Sidebar unit tests in `core/sidebar/test/`.
