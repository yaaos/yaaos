# Frontend modularity

## Layers

Three top-level groupings under `apps/web/src/`:

| Layer | Path | Purpose | May depend on |
|---|---|---|---|
| `core` | `src/core/` | Top-level wiring: routing, layout, API client, SSE, observability. No feature UI. | `shared` |
| `domain` | `src/domain/<feature>/` | Feature modules — pages, components, hooks for one surface. | `core`, `shared` |
| `shared` | `src/shared/` | Reusable primitives — components, hooks, utilities, types. | — |

Rules:
- Cross-domain imports are forbidden. If two domains need the same primitive, it moves to `shared/`.
- `core` cannot import from `domain` (one-way dependency, mirrors the backend).

## Module shape

A domain module collapses page + local subcomponents into one `index.tsx`. Split into `list/` + `detail/` (plus `_shared.tsx`) only when a file passes ~1500 lines. Optional `api.ts` for module-local TanStack hooks; optional `routes.tsx` once route registration goes per-module.

A core module has `index.ts` (re-exports), implementation file(s), and optional `types.ts`.

## Imports

- Absolute only, via TS path aliases (`@core/...`, `@domain/...`, `@shared/...`).
- Only what's in `index.ts` — no deep imports across modules.

## testid conventions

- Page-level container: `<page>-<state>` (e.g., `dashboard-onboarding`, `ticket-detail`).
- List containers: `<entity>-list` (`tickets-list`, `findings-list`, `lessons-list`, `audit-log`).
- List rows: `<entity>-row-<id>`.
- Actions: `<action>-<entity>` (`rereview-button`, `cancel-jobs-button`, `lesson-save`, `teach-yaaos`).
- Status badges: `<entity>-status` (`github-status`, `apikey-status`).
- Form fields: `<form>-<field>` (`gh-app-id`, `anthropic-key`, `teach-title`).

The review card additionally carries `data-state="<status>"` so specs can query `[data-testid^="agent-card-"][data-state="posted"]`.

## Cross-cutting wiring

- **TanStack Query** — single `QueryClient` in `main.tsx`. Keys are module-scoped arrays; see [patterns.md § Query keys](patterns.md#query-keys).
- **SSE** — single `<SSESubscriber>` (`core/sse/subscriber.tsx`) wraps the router in `main.tsx`. Opens one `EventSource` on `/api/events`, translates events into query-cache invalidations. See [core_sse.md](core_sse.md).
- **Routing** — `core/routing` declares the full route tree centrally; domain modules export their page components.

## Tooling

| Concern | Tool |
|---|---|
| Lint + format | Biome (`apps/web/biome.json`) |
| Type check | `tsc --noEmit` |
| Unit tests | Vitest |
| Build | Vite |

`apps/web/bin/ci` runs all four. No tach equivalent — boundaries are enforced manually plus by Biome's rules.
