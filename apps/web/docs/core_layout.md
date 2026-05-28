# core/layout

> App shell — sidebar mount, route outlet, theme tokens, broken-integrations banner.

## Purpose

Wraps every signed-in page in the sidebar-only shell mandated by [design.md § Layout](design.md#layout). Owns the theme switcher (`data-theme` on `<html>`) and the in-page banner that surfaces broken integrations across the top of `<main>`. The sidebar itself lives in [core/sidebar](../src/core/sidebar/) and is composed into the shell here.

## Public interface

- `AppShell` — root-route component (see [core_routing.md](core_routing.md)). The only export.

## Module architecture

Files: `app-shell.tsx`, `theme.ts`, `broken-integrations-banner.tsx`, `index.ts`. Sidebar is its own module at `src/core/sidebar/`.

### `AppShell`

Two-column flex: fixed-width sidebar + flex-grow `<main>`. Only `<main>` scrolls. No top bar — see [design.md § Principles](design.md#principles). `BrokenIntegrationsBanner` renders above `<main>` when any required integration is unhealthy.

`STANDALONE_PATHS` (`/login`, `/user`, `/orgs`) render `<Outlet>` without the shell.

### Theme tokens

`theme.ts`: `getSidebarPinned()` / `setSidebarPinned(pinned)` — `localStorage` pinned-vs-rail state. `toggleTheme()` — flips `[data-theme="light"|"dark"]` on `<html>`. Token vocabulary: [design.md § Design tokens](design.md#design-tokens).

### `BrokenIntegrationsBanner`

Reads `useBrokenIntegrations()`; shows one row per broken integration with a deep link to settings. Hidden when everything is healthy.

## Data owned

None — `BrokenIntegrationsBanner` reads from a query hook; `theme.ts` reads/writes `localStorage` only.

## How it's tested

Rendered on every e2e test — shell breakage shows up as page-navigation failures. Sidebar unit tests in `core/sidebar/test/`.
