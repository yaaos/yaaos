# Frontend docs

React SPA built with Vite. Bundled into the backend's Docker image at build time and served from FastAPI via the catch-all in `core/webserver`.

## Read first

- [modularity.md](modularity.md) — layer shape, import rules, testid conventions.
- [patterns.md](patterns.md) — query-key taxonomy, time helpers, SSE invalidation, error boundary.

## Module map

4 core docs + 5 domain docs.

### Core

| Module | Responsibility |
|---|---|
| [core_api](core_api.md) | `openapi-fetch` client + `apiFetch` + every TanStack Query/mutation hook. |
| [core_sse](core_sse.md) | Single `EventSource` at app root; events → query-cache invalidations. |
| [core_routing](core_routing.md) | TanStack Router config + route tree. |
| [core_layout](core_layout.md) | App shell — sidebar, topbar, theme tokens, route outlet. |

### Domain

| Module | Responsibility |
|---|---|
| [domain_dashboard](domain_dashboard.md) | Two-state landing page: onboarding stepper or populated metrics + in-flight. |
| [domain_tickets](domain_tickets.md) | Ticket list + detail (review card, findings tagged by source subagent, Teach-yaaos modal). |
| [domain_settings](domain_settings.md) | Three peer cards: GitHub App, Model API key, Plugin health. |
| [domain_memory](domain_memory.md) | Per-repo lessons CRUD. |

## Directory shape

Under `apps/web/src/`: `core/` (api, sse, routing, layout, observability), `domain/` (one folder per surface), `shared/` (components, hooks, utils, types), and `main.tsx` (entry — mounts `QueryClient` + `SSESubscriber` + Router).

## Running locally

`pnpm dev` from `apps/web/` starts Vite on :5173, proxying `/api/*` and `/assets/*` to the backend (run separately via `apps/backend/bin/dev`).

## CI

`apps/web/bin/ci` runs Biome format-check + lint, `tsc --noEmit`, Vitest, and the Vite production build.

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
| Component primitives | hand-rolled in `shared/components/` |
| Lint / format | Biome |
| Unit tests | Vitest |
| Icons | lucide-react |
| TypeScript | `strict: true`, path aliases (`@core/...`, `@domain/...`, `@shared/...`) |
