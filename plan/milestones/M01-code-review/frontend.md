# M01 — Frontend (planned)

> Planned frontend modules for M01.
> Each module gets its own `docs/<module>.md` written as it's built (responsibility, public interface, key components, how it's tested).
> Architecture-wide context (stack, topology, layering rules) lives in [architecture.md](architecture.md) and [modularity.md](modularity.md).

## Tooling

| Concern | Choice |
|---|---|
| Build / dev server | Vite |
| UI framework | React |
| Routing | TanStack Router |
| Server state | TanStack Query |
| Client state (cross-route) | Zustand |
| Component library | shadcn/ui |
| Styling | Tailwind |
| API client | `openapi-typescript` (types) + `openapi-fetch` (typed client) + hand-written TanStack Query hooks |
| Real-time | Native `EventSource` (SSE) wrapped in a `core/events` hook |
| Forms | react-hook-form + zod (shadcn Form integration) |
| Validation | zod |
| Lint / format | Biome |
| Unit / component tests | Vitest |
| End-to-end tests | **In `apps/e2e/`**, not here. TypeScript Playwright in its own workspace. See [patterns.md](patterns.md#three-categories). |
| Icons | lucide-react |
| Toasts | sonner |
| Dates | date-fns |
| TypeScript | `strict: true`, `noUncheckedIndexedAccess`, `noImplicitOverride` |
| Package manager | pnpm (workspaces) |

## Module map

13 modules total: 7 core · 6 domain. `shared/` is a flat bucket, not modules.

### Core (7)

Top-level SPA wiring. No feature-specific UI.

| Module | Responsibility |
|---|---|
| `routing` | TanStack Router config, route tree, guards, code-splitting boundaries. |
| `layout` | App shell, header, navigation, page wrapper, route outlet. |
| `api` | OpenAPI-generated types, `openapi-fetch` client instance, `QueryClient`, shared TanStack Query helpers (cache keys, defaults). |
| `events` | SSE client: `useEventStream` hook with reconnect, subscription primitives domain modules use to listen for specific event types. |
| `observability` | Error boundary, structured client-side logging, console wrappers. |
| `notifications` | Toast system (`sonner` wrapped in a thin `notify()` API), confirmation dialogs. |
| `state` | Zustand store conventions, persistence (if any), DevTools wiring. Domain modules define their own slices; `state` just holds the conventions. |

### Domain (5)

One module per UI surface.

| Module | Responsibility |
|---|---|
| `dashboard` | Two-state landing page. **Onboarding state** (any of the three readiness flags false): three-step stepper card driving GitHub App install / Anthropic key set / first repo allowlisted, with completion check marks + a `{X} of 3 complete` badge. **Populated state**: 4 metric tiles (reviews posted, cost, open tickets, failure rate) + live agents in-flight panel (per-ticket review-job statuses, joined client-side from `useTickets()` + `useReviewJobsForTicket()` + `useReviewerAgents()`). Sparklines, 24h deltas, and the right-hand activity feed are deferred (see `plan/design/M01-DELTAS.md` § Dashboard deferrals). |
| `tickets` | Ticket list (filters by repo / author / date range, infinite scroll, SSE-driven live updates) and ticket detail (linked PR summary + single "Re-review" button + tabbed: Agents / Audit log). Each ticket in M01 links to one GitHub PR; the UI is ticket-centric so M02+ ticket sources (Linear, Jira, …) slot in without UI restructure. |
| `prompts` | Agent prompt editor: three text areas (architecture / security / style), save with non-empty validation, reset-to-default. |
| `memory` | Per-repo memory management: list lessons (title / body / source / created-at), create, edit, delete. |
| `settings` | Three independent cards (no gating between them): **GitHub App** (install status + outbound links to install/manage on GitHub), **Model API key** (Anthropic key entry form, always editable), **Plugin health** (one row per plugin — `github`, `claude_code`, `in_process` — each fetched independently via `usePluginHealth(id)`). Each card stands on its own; setting one doesn't unlock another. |

### Shared (flat bucket)

```
apps/web/src/shared/
├── components/      # primitives wrapping shadcn/ui (Button, Card, Dialog, Tabs, Form, …)
├── hooks/           # cross-domain hooks (useDebounce, useInterval, useLocalStorage, …)
├── utils/           # formatDate, classnames helper, etc.
├── icons/           # re-exports from lucide-react
└── types/           # cross-domain TypeScript types (paginated response shapes, etc.)
```

Anything in `shared/` may be imported by any module. Cross-domain imports between `domain/foo` and `domain/bar` are still forbidden — extract to `shared/` instead.

## Boundary decisions

Module boundaries deliberately drawn this way:

- **The frontend is dumb; all business logic lives in the API.** The SPA renders data and dispatches actions — it does not compute verdicts, derive status, run eligibility checks, decide permissions, or hold any rule the backend doesn't also enforce. Frontend-side input validations exist only for UX immediacy and are duplicated authoritatively on the backend. See [architecture.md § Dumb frontend](architecture.md#dumb-frontend-all-business-logic-in-the-api) for the full principle and [patterns.md § Dumb frontend](patterns.md#dumb-frontend--no-business-logic) for practical guidance.
- **FE modules organize by UI surface, not by backend module.** `prompts` and `memory` are separate FE modules even though they fold into `reviewer` on the backend. `tickets` (FE) shows data sourced from `tickets`, `pull_requests`, and `reviewer` on the backend — same idea. Different pages, different routes, different forms → different FE modules. The asymmetry doesn't leak because all FE modules talk to BE through `core/api`.
- **`settings` is its own module** (not folded into `dashboard`). Settings will grow; dashboard stays focused on at-a-glance status and onboarding banners.
- **No `auth` core module in M01.** Slot is reserved (`core/routing` guards file exists empty). Added when auth ships.
- **No separate `forms` module.** Form usage is per-feature. Shared form primitives live in `shared/components/`.

## Things considered and rejected

- **`websocket` core module** — SSE is the chosen real-time transport; `core/events` covers it. WebSockets would be over-engineering for unidirectional server→client push.
- **Standalone `audit_log` UI module** — audit log is a tab inside `tickets` detail, not its own surface.
- **State management beyond Zustand + TanStack Query** — Redux / Jotai unnecessary at M01 scope.

## Open for next pass

To be defined per module before implementation:

- Public interface (which components / hooks / types are exported from `index.ts`).
- Owned routes (which paths each `domain/` module mounts).
- SSE event types each module subscribes to.
- Test surface (which behaviors are covered by Vitest unit/component vs Playwright e2e).

## Decisions

### 2026-05-13 — SSE over WebSockets
All real-time updates from server to client use SSE (`EventSource`).
**Why:** unidirectional server→client push is the only need; SSE is simpler ops (plain HTTP, built-in auto-reconnect, no protocol upgrade) and easier to debug. WebSockets buy bi-directional capability we don't need.

### 2026-05-13 — openapi-typescript + openapi-fetch over generators that emit hooks
API codegen produces types + a thin typed fetch client. TanStack Query hooks are hand-written.
**Why:** generated hook code is opaque to read and reason about; plain TanStack Query usage is transparent and benefits from a vast pool of training data / examples.

### 2026-05-15 — Dumb frontend; all business logic in the API
SPA is rendering + dispatch only. No business outcomes computed client-side; validations are UX-only and always duplicated on the backend.
**Why:** prevents drift between two implementations of the same logic; centralizes the future auth/RBAC boundary; non-SPA clients observe identical behavior; behavior changes ship in one image.

### 2026-05-13 — Frontend module map locked
7 core, 6 domain. `prompts` and `memory` are separate FE modules despite folding into `reviewer` on the backend, because FE modules organize by UI surface.
