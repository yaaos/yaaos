# Frontend docs

React SPA built with Vite. Bundled into the backend's Docker image at build time and served from FastAPI via the catch-all in `core/webserver`.

## Read first

- [design.md](design.md) — design principles, layout, navigation rules, state patterns, density, voice, icons, a11y, design tokens. Read before adding a surface or chrome element.
- [components.md](components.md) — index of available primitives + composites.
- [architecture.md](architecture.md) — layer model (core / domain / shared), cross-cutting wiring, key flows.
- [patterns.md](patterns.md) — module shape, import rules, testid conventions, query-key taxonomy, time helpers, SSE invalidation.

## Module map

### Core

| Module | Responsibility |
|---|---|
| [core_api](core_api.md) | `openapi-fetch` client + `apiFetch` + every TanStack Query/mutation hook. |
| [core_sse](core_sse.md) | Single `EventSource` at app root; events → query-cache invalidations. |
| [core_routing](core_routing.md) | TanStack Router config + route tree. |
| [core_layout](core_layout.md) | App shell — sidebar mount, theme tokens, route outlet, broken-integrations banner. No topbar (see [design.md](design.md)). |

### Domain

| Module | Responsibility |
|---|---|
| [domain_dashboard](domain_dashboard.md) | landing — 4 stat cards + In-flight band + Needs-attention band, with the NotConfiguredBanner on top when the org isn't ready. |
| [domain_tickets](domain_tickets.md) | tickets list + ticket detail (header band, StageIndicator, Findings / Activity / HITL tabs). |
| [domain_lessons](domain_lessons.md) | Per-repo lessons CRUD. |
| [domain_notifications](domain_notifications.md) | cross-org inbox page + sidebar bell popover. |
| [domain_org_settings](domain_org_settings.md) | Tabbed org-settings shell (Auth, Members, VCS, Coding Agents, API Keys, MCP Proxy, Audit). |
| [domain_auth](domain_auth.md) | Login page (email-first SSO-discover) + logout. |
| [domain_user](domain_user.md) | `/user/details`, `/user/security`, `/user/messaging` — self-service profile + 2FA. |
| [domain_orgs](domain_orgs.md) | Org picker (`/orgs`) + Members + Audit + SSO config — surfaces tied to a specific org's identity layer. |

## Running locally

`pnpm dev` from `apps/web/` starts Vite on :5173, proxying `/api/*` and `/assets/*` to the backend (run separately via `apps/backend/bin/dev`).

## CI

`apps/web/bin/ci` runs Biome, `tsc --noEmit`, Vitest, and the Vite build. Semgrep runs in a separate RWX task (`web-security`) via the `semgrep/semgrep` Docker image — kept out of `bin/ci` because the web-builder image is node-only. Local semgrep shortcut and full docker invocation documented inline in `apps/web/bin/ci`.

## Stack

| Concern | Choice |
|---|---|
| Build / dev server | Vite |
| UI framework | React 18 |
| Routing | TanStack Router |
| Server state | TanStack Query |
| API client | `openapi-fetch` (typed) + hand-written `apiFetch` |
| Real-time | Native `EventSource` (SSE) |
| Forms | React state + manual validation |
| Styling | Tailwind, oklch color tokens |
| Component primitives | shadcn-style copies in `shared/components/ui/` (Radix-backed) + composites in `shared/components/{layout,chrome}/` |
| Lint / format | Biome |
| Unit tests | Vitest |
| Icons | lucide-react |
| TypeScript | `strict: true`, path aliases (`@core/...`, `@domain/...`, `@shared/...`) |
