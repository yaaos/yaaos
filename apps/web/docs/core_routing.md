# core/routing

> TanStack Router config — URL → component mapping for the SPA.

## Purpose

All of yaaos's pages route through here. A flat list of `createRoute` calls under a single root route that renders `AppShell` (from `core/layout`). 6 paths, no nesting, no per-feature route trees.

## Public interface

- `router` — TanStack `Router` instance, consumed by `main.tsx`'s `<RouterProvider>`.

The module also declares the TanStack module augmentation so the `Register` interface picks up the router type, giving `<Link to="/...">` typed autocomplete.

## Module architecture

### Route tree

| Path | Component | Module |
|---|---|---|
| `/` | redirect → `/dashboard` | — |
| `/dashboard` | `DashboardPage` | `@domain/dashboard` |
| `/tickets` | `TicketsPage` | `@domain/tickets` |
| `/tickets/$ticketId` | `TicketDetailPage` | `@domain/tickets` |
| `/memory` | `MemoryPage` | `@domain/memory` |
| `/settings` | `SettingsPage` | `@domain/settings` |

Every route is a direct child of root. Root's `component` is `AppShell`; each child renders inside the shell's `<Outlet />`. The `/` route uses `beforeLoad` to redirect to `/dashboard`.

### Per-module route registration

Not used — central declaration is easier to read at 5 paths. Refactor to per-feature route exports when the surface grows beyond ~20 paths or starts requiring nested layouts.

### Code-splitting

Not configured. All domain pages are eagerly imported; production bundle is ~400KB gzipped. Add `lazy()` boundaries when the bundle warrants.

### Type augmentation

`router.tsx` declares `module "@tanstack/react-router"` augmenting `Register` so `<Link to="/tickets/$ticketId">` type-checks everywhere.

## Data owned

None.

## How it's tested

Every e2e spec in `apps/e2e/tests/*` exercises routing via `page.goto(...)`. No dedicated Vitest — the route tree is declarative.
