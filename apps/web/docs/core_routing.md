# core/routing

> TanStack Router config — URL → component mapping for the SPA.

## Purpose

Every authenticated page lives under `/orgs/$slug/...`. There is exactly one URL tree for authenticated work; the only routes that render outside it are `/login` and `/orgs` (the picker). User-account pages (`Details`, `Security`, `Notifications`) sit at `/orgs/$slug/user/*` — the slug is always part of the URL, which is the only source of truth for current org context.

## Public interface

- `router` — TanStack `Router` instance, consumed by `main.tsx`'s `<RouterProvider>`.

The module declares the TanStack `Register` augmentation so `<Link to="/orgs/$slug/...">` gets typed autocomplete.

## Module architecture

### Route tree

| Path | Component | Notes |
|---|---|---|
| `/` | beforeLoad probe | Hits `/api/auth/me`. 401 → `/login`. 200 + 1 membership → that org's dashboard. 200 + 0 or >1 → `/orgs` picker. |
| `/login` | `LoginPage` (`@domain/auth`) | `beforeLoad` probes `/api/auth/me`; on 200, redirects to `/` (prevents authed-user bounce loop). Reads `?reason=` (`signed_out`, `expired`, `idle`, `not_provisioned`) for the banner. |
| `/orgs` | `OrgPickerPage` | Standalone (no sidebar). Empty state when the user has zero memberships ("ask an admin to invite you"). |
| `/orgs/$slug` | scope-only route | Parent for all org-scoped subtrees, including user-area pages. |
| `/orgs/$slug/dashboard` | `DashboardPage` | |
| `/orgs/$slug/tickets`, `…/$ticketId` | `TicketsPage`, `TicketDetailPage` | |
| `/orgs/$slug/lessons` | `LessonsPage` | |
| `/orgs/$slug/settings` | redirect | 303 → `/orgs/$slug/settings/auth`. |
| `/orgs/$slug/settings/{auth,members,audit,vcs,coding-agents,coding-agents/$pluginId,api-keys,mcp-proxy,workspace}` | per-page `…SettingsPage` | Owner/Admin gates per page. |
| `/orgs/$slug/user` | redirect | 303 → `…/user/details`. |
| `/orgs/$slug/user/{details,security,notifications}` | `DetailsPage`, `SecurityPage`, `NotificationsPage` | USER_SCOPED on the backend (`/api/user/*`, `/api/notifications/*`); the slug in the path is purely a frontend routing concern. |

### Slug source of truth = URL

`core/api/org-context.ts` exposes `getCurrentOrgSlug()` (plain function) and `useCurrentOrgSlug()` (hook). Both derive from `window.location` on every read — no module-global cache, no localStorage. Two tabs in different orgs stay independent.

`apiFetch` uses `getCurrentOrgSlug()` to attach `X-Org-Slug`. Chrome components use `useCurrentOrgSlug()` to re-render on navigation. Chrome renders only inside the org-scope route (`STANDALONE_PATHS` exit early) so every chrome component is guaranteed a non-null slug.

### `/api/auth/me` contract

Returns `{user, memberships[]}` (each entry: `slug`, `display_name`, `role`, `handle`, `broken_integrations`). No `current_org_slug` field — the server has no opinion about "current org"; that's URL state.

### Login + provisioning

- 401 → `handleAuthFailure` hard-navigates to `/login?reason=signed_out&next=…`.
- `LoginPage` lists `/api/auth/providers`; clicking hits `/api/auth/login?provider=<id>&next=<path>`.
- OAuth completes server-side. No match → `/login?reason=not_provisioned`, no cookie — **OAuth never auto-provisions**. New users must be invited via `/api/memberships/accept`.
- On success: `_safe_next` validates `next`; if it targets `/orgs/$slug/...`, the user must have a membership in `$slug`, otherwise collapses to `/`.

### In-app navigation

Use `<Link>` from `@tanstack/react-router` for all SPA navigation. Native `<a href="...">` is for external URLs or backend `/api/` redirects only. Grep guard: `grep -rn '<a\s[^>]*href="/' apps/web/src` → zero in-SPA hits.

`router.tsx` augments `Register` so typed `<Link to="...">` works everywhere. Prefer `params={{ slug }}` over interpolated strings for full type safety.

## Data owned

None. The slug is derived from the URL on every read.

## How it's tested

- `apps/e2e/tests/login-and-membership.spec.ts` covers the full login → org-scoped routes → membership flow via the `oauth_test` provider, including the regression case (hard-nav to `/orgs/acme/user/details` then click Dashboard).
- `apps/e2e/tests/session-died-redirect.spec.ts` covers 401 → `/login?reason=…&next=…` round trips.
- Backend `apps/backend/app/domain/sessions/test/test_oauth_endpoints.py` covers no-auto-provisioning and the not-provisioned redirect.
