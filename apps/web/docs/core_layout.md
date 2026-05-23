# core/layout

> App shell — sidebar, topbar, theme tokens, route outlet.

## Purpose

Wraps every page in a consistent left-nav + top-bar shell. Defines oklch color tokens and spacing that Tailwind resolves against. Renders the TanStack Router `<Outlet />` inside `<main>`.

## Public interface

- `AppShell` — root-route component (see [core_routing.md](core_routing.md)). The only export; `Sidebar`, `Topbar`, and the theme module are internal.

## Module architecture

### Files

Under `src/core/layout/`: `app-shell.tsx`, `sidebar.tsx`, `topbar.tsx`, `theme.ts`, with `index.ts` re-exporting `AppShell`.

### `AppShell`

Two-column flex: fixed-width sidebar on the left, flexible-width content on the right. Only `<main>` scrolls; sidebar and topbar stay pinned. Topbar shows a static crumb derived from `useRouterState().location.pathname` through `CRUMB_BY_PATH`; routes wanting a custom title add an entry there.

`STANDALONE_PATHS` (`/login`, `/user`, `/orgs`) render the `Outlet` without sidebar or topbar — user-scoped + org-picker pages don't surface org nav. Visiting a standalone path while authenticated still works; visiting one of the others while unauthenticated bounces through `indexRoute → /login`.

### `Sidebar`

TanStack `<Link>`s to the 5 top-level pages — active state is computed automatically. Text labels, no per-route icons.

### `Topbar`

Single-line header showing the current page's crumb, theme toggle, live-indicator pill, and (when signed in) an Account link + Sign-out button. Sign-out calls `/api/auth/logout` then hard-navigates to `/login` so the in-memory query cache is torn down (otherwise stale `/me` data persists into the next session).

### Theme tokens

`theme.ts` exports oklch color tokens as CSS custom properties (light + dark variants share lightness for consistent contrast). Tailwind utilities like `bg-surface`, `text-text-2`, `border-border-soft` resolve against them. The shell sets `data-theme` on the root. M01 ships only the light theme; dark variables exist but no toggle wires them up.

## Data owned

None.

## How it's tested

Rendered on every e2e test — any shell breakage shows up as page-navigation test failures. No dedicated layout tests.
