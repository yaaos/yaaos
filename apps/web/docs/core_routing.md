# core/routing

> TanStack Router config — URL → component mapping for the SPA.

## Purpose

All of yaaos's pages route through here. M02 reshapes the tree around org-scoped paths: every domain page sits under `/orgs/$slug/...`. The login + account pages are user-scoped (no org context); `/` is a probe that redirects to the dashboard for the user's first org, or to `/login` when unauthenticated.

## Public interface

- `router` — TanStack `Router` instance, consumed by `main.tsx`'s `<RouterProvider>`.

The module also declares the TanStack module augmentation so the `Register` interface picks up the router type, giving `<Link to="/orgs/$slug/...">` typed autocomplete.

## Module architecture

### Route tree

| Path | Component | Notes |
|---|---|---|
| `/` | beforeLoad probe | Hits `/api/auth/me`; on 401 → `/login`, on 200 → `/orgs/<first-slug>/dashboard`. |
| `/login` | `LoginPage` (`@domain/auth`) | User-scoped; clears `org_id` contextvar. |
| `/account` | `AccountPage` (`@domain/auth`) | User-scoped; emails + TOTP setup entry + "Sign out everywhere". |
| `/orgs/$slug` | scope-only route | `beforeLoad` calls `setCurrentOrgSlug(slug)` so `apiFetch` injects `X-Org-Slug`. |
| `/orgs/$slug/dashboard` | `DashboardPage` | |
| `/orgs/$slug/tickets` | `TicketsPage` | |
| `/orgs/$slug/tickets/$ticketId` | `TicketDetailPage` | |
| `/orgs/$slug/memory` | `MemoryPage` | |
| `/orgs/$slug/settings` | `SettingsPage` | |
| `/orgs/$slug/members` | `MembersPage` (`@domain/orgs`) | |
| `/dashboard` (legacy) | redirects to `/` | Deleted in Phase 14 once links are migrated. |

### `setCurrentOrgSlug` + auto-injection

`apps/web/src/core/api/org-context.ts` holds a module-global current slug. The `/orgs/$slug` parent route writes to it in `beforeLoad` whenever a navigation enters an org-scoped subtree; `/login` and `/account` clear it. `apiFetch` reads the slug and adds `X-Org-Slug` unless the caller already set one.

This pattern lets every domain hook (`useTickets`, `useMemory`, etc.) stay org-agnostic at the call site — the SPA layer adds the header, the backend's `require(action)` dep validates it.

### Login flow

- Anonymous user hits any URL → `/` probe → `/login`.
- `LoginPage` enumerates `/api/auth/providers`; clicking a button hits `/api/auth/login?provider=<id>&next=<path>`.
- OAuth callback completes server-side and 303-redirects to `next`. The session cookie is now set.
- `/` probe re-runs (or the SPA refetches `/api/auth/me`) → `/orgs/<first-slug>/dashboard`.

### Type augmentation

`router.tsx` declares `module "@tanstack/react-router"` augmenting `Register` so `<Link to="/orgs/$slug/tickets/$ticketId">` type-checks everywhere. Callers passing `params={(prev) => ({ slug: prev.slug as string, ticketId: ... })}` cast slug because TanStack's params type inference treats parent params as optional inside a child route; the cast is intentional and Phase 14 may revisit.

## Data owned

None. The current slug lives in `@core/api`'s `org-context` module.

## How it's tested

- Phase 7 Playwright spec (`apps/e2e/tests/login-and-membership.spec.ts`) drives the full login → org-scoped routes → membership flow via the `oauth_test` provider.
- Per-page e2e specs in `apps/e2e/tests/*.spec.ts` exercise routing via `page.goto(...)`.
