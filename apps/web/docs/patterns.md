# Frontend patterns

Cross-app conventions (UTC on the wire, audit-log shape) live in [`docs/system-architecture.md`](../../../docs/system-architecture.md). Layer model and cross-cutting wiring: [architecture.md](architecture.md).

## Module shape

- **Domain module** — collapses page + local subcomponents into one `index.tsx`. Split into `list/` + `detail/` (plus `_shared.tsx`) only when a file exceeds ~1500 lines. Optional `api.ts` for module-local TanStack hooks.
- **Core module** — `index.ts` (re-exports), implementation file(s), optional `types.ts`.

## Imports

Absolute only via path aliases (`@core/...`, `@domain/...`, `@shared/...`). Only what's exported from `index.ts(x)` — no deep imports across module boundaries.

## testid conventions

- Page container: `<page>-<state>` (e.g. `dashboard-onboarding`, `ticket-detail`).
- List containers: `<entity>-list` (`tickets-list`, `findings-list`, `lessons-list`, `audit-log`).
- List rows: `<entity>-row-<id>`.
- Actions: `<action>-<entity>` (`rereview-button`, `cancel-jobs-button`, `lesson-save`, `teach-yaaos`).
- Status badges: `<entity>-status` (`github-status`, `apikey-status`).
- Form fields: `<form>-<field>` (`gh-app-id`, `anthropic-key`, `teach-title`).
- Review cards carry `data-state="<status>"` — query via `[data-testid^="agent-card-"][data-state="posted"]`.

## Tooling

| Concern | Tool |
|---|---|
| Lint + format | Biome (`apps/web/biome.json`) |
| Type check | `tsc --noEmit` |
| Unit tests | Vitest |
| Build | Vite |

`apps/web/bin/ci` runs all four.

## Module documentation

Every shipped module has one `apps/web/docs/<layer>_<module>.md` with this fixed structure:

1. **Purpose** — what the module owns; what it doesn't.
2. **Public interface** — exports from `index.ts(x)`. No internals.
3. **Module architecture** — Entities · Key value objects · Core user flows · State machines (omit if none; `from → to` notation).
4. **Data owned** — query keys, notable local state.
5. **How it's tested** — e2e coverage.

Terse, bullets, no code snippets, no `Decisions` section, link don't repeat.

## Auth + tenancy

- `apiFetch` auto-injects `X-Org-Slug` from `core/api/org-context.ts`. Domain hooks are org-agnostic at the call site.
- Use `<RequireMembership orgSlug="..." role="admin">` for UI role gates — hint only; backend `require(action)` is the authority.
- Every domain page is under `/orgs/$slug/...`. The `/` route probes `/api/auth/me` and redirects.

## Sidebar nav config

- `core/sidebar/nav-config.ts` defines the `NavConfig` type (`link | group`). Route paths are relative (`/dashboard`); renderer prefixes `/orgs/{slug}`.
- `role: "admin"` on a link or group hides it for non-admins; a group disappears when no child survives the filter.
- Collapse state lives in `localStorage` via `use-collapse-state.ts`, syncs across tabs. A group is expanded only while a child is the active route; navigating away auto-collapses it.
- Rail-mode groups open a right-anchored Popover. Active items use `bg-accent` only — no layout shift on select.

## Org Settings shell

- `OrgSettingsLayout` is a passthrough `<div>` — no top chrome, no tab bar. Per-page role gating in each settings page.
- Coding-agent plugin settings dispatch through `apps/web/src/domain/org_settings/coding_agents/plugin_registry.ts`. First-party plugins register at module load via side-effect import (`claude_code`); unregistered plugins get the built-in placeholder.
- `PluginPicker` (`shared/plugin_picker/`) is shared between the VCS empty-state and Coding Agents Add flow. Backed by `useAvailablePlugins(type)` → `GET /api/plugins/available?type=...`.

## Dumb frontend

The SPA renders data and dispatches actions — it owns no rules the backend doesn't also enforce.

- Verdicts, statuses, counts, permissions: server-supplied. Never derived client-side.
- Cache invalidation: driven by mutation responses and SSE events. No client heuristics.
- Client-side filter/sort: fine for UX over a fetched list. Any operation that changes which rows the user *acts on* goes through the API.

If a FE change could alter stored/posted/counted state without a corresponding API change, the logic is in the wrong place.

## Query keys

Module-scoped arrays. Canonical keys:

- `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`
- `["reviewer", "jobs", ticket_id]`, `["reviewer", "metrics"]`, `["reviewer", "agents"]`
- `["lessons", repo]`
- `["github", "installation"]`, `["github", "repositories"]`
- `["plugin-health", pluginId]`
- `["onboarding"]`, `["health"]`

Mutations and the SSE subscriber ([core_sse.md](core_sse.md)) invalidate exactly the keys they affect.

## Time and dates

Backend emits ISO-8601 UTC (`Z`). FE renders in browser local timezone via `apps/web/src/shared/utils/ago.ts`:

- `ago(ts)` — relative duration (`"12s ago"`).
- `formatTime(ts)` — local `HH:MM:SS` (audit-log rows).
- `formatDateTime(ts)` — full local date + time.

Anti-pattern: `new Date(ts).toISOString()` — always UTC, never use for display.

## API client

Two surfaces in `core/api/client.ts`: `apiClient` (typed `openapi-fetch`) and `apiFetch<T>` (generic helper, throws on non-2xx). Every hook wraps one of these. Full surface: [core_api.md](core_api.md).

## Error handling at the API boundary

- **Mutations** — expose `isPending`/`isSuccess`/`isError`; forms show inline "Saving…" / "Saved." / red error text.
- **Queries** — components handle loading + error inline; `data-testid` slots on primitives let e2e assert state.
- **Validation errors** — 4xx field-keyed map surfaces under the relevant input.

## Code style

- Function components only. Hooks for shared logic; no HOCs unless forced by a library.
- TanStack Query for server state — no `useEffect(() => fetch(...))`.
- Tailwind only. Color tokens in `core/layout/theme.ts` (oklch).
- `tsc` strict — warnings are CI errors.
- Forms: React state + manual validation. No `react-hook-form` / `zod`.
