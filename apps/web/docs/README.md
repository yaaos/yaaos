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
| [domain_lessons](domain_lessons.md) | Per-repo lessons CRUD. |

## Directory shape

Under `apps/web/src/`: `core/` (api, sse, routing, layout, observability), `domain/` (one folder per surface), `shared/` (components, hooks, utils, types), and `main.tsx` (entry — mounts `QueryClient` + `SSESubscriber` + Router).

## Running locally

`pnpm dev` from `apps/web/` starts Vite on :5173, proxying `/api/*` and `/assets/*` to the backend (run separately via `apps/backend/bin/dev`).

## CI

`apps/web/bin/ci` runs Biome format-check + lint, `tsc --noEmit`, Vitest, and the Vite production build. Semgrep static security scanning lives in its own RWX task (`web-security` in `.rwx/push.yml`) using the official `semgrep/semgrep` Docker image — kept out of `bin/ci` because the web-builder image is node-only (no Python) and the web pipeline must not depend on the backend pipeline's artifacts. Local dev shortcut: `cd apps/web && uv run --directory ../backend semgrep scan --config p/typescript --config p/react --config p/owasp-top-ten --error --metrics off --quiet src` — reuses the semgrep already installed in the backend's uv venv as a convenience (not a structural dependency; the rulesets and target are entirely web-specific). Full docker-image invocation also documented inline in `apps/web/bin/ci`.

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
