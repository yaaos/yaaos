# M01 — Patterns and Conventions (planned)

> Code-style conventions and testing/tooling discipline.
> Cross-cutting: applies to every module on both backend and frontend. Not M01-specific.
> Promoted to `docs/patterns.md` when M01 ships.

## Code style

### Functional first

Functions are the default unit of work. Classes are used only for:

- **Pydantic models** (request/response schemas, task inputs, config dataclasses).
- **Exceptions** (typed hierarchies).
- **Adapters / protocol shims** (small wrappers translating between interfaces).
- **State containers** that genuinely need methods + state coupled (rare in this codebase).

If a piece of business logic can be expressed as one or more module-level async functions, that is the right shape. Resist "service classes" or "manager classes."

### Standard module file layout

Every module follows this skeleton:

```
domain/foo/                       (or core/foo/ or plugins/foo/)
├── __init__.py     # public interface — re-exports + __all__ + registration calls
├── module.py       # module identity (name); imported wherever the module needs to refer to itself
├── web.py          # (if the module has HTTP routes) FastAPI router + handlers
├── service.py      # business-logic functions (or split into more files as needed)
├── models.py       # SQLAlchemy + Pydantic types owned by this module
├── tasks.py        # (if the module registers background tasks) handler functions
└── test/           # tests live inside the module
```

`models.py` / `service.py` / etc. are *conventions*, not enforced; they can be split into more files as a module grows. `__init__.py`, `module.py`, and `web.py` (when there are routes) are **standard** — every module that fits each role uses these filenames.

### `module.py`

Every module has a `module.py` that exports its name:

```python
# domain/foo/module.py
def get_module_name() -> str:
    return "foo"
```

Used wherever the module needs to refer to itself by name (registration calls, audit-log entries, span attributes, log fields). Centralizing the string in one function means renames touch one file.

### `__init__.py` shape

Every module's `__init__.py` follows this template:

```python
"""One-line module docstring."""

from app.domain.foo.service import public_function, PublicType
from app.domain.foo import module

__all__ = ["public_function", "PublicType"]

# Registration side effects (if any) go HERE, at the bottom,
# AFTER all imports — never inside a function.
# Examples: registering a plugin (register_vcs_plugin, register_llm_provider),
# registering HTTP routes (register_domain_routes — done in web.py),
# subscribing to SSE event types, etc.
# Background-task handlers are NOT registered by name — they're referenced
# directly when enqueued: `await enqueue(handler, payload)`.
```

Rules:

- `__all__` is **always** present and lists the module's public API. Tach uses it to compute the interface.
- All implementation lives in named submodules (`service.py`, `models.py`, `web.py`, `tasks.py`, etc.). **No business logic in `__init__.py`.**
- Re-exports come first. Then `__all__`. Then registration calls.
- No lazy/conditional imports except for heavy ML model loading; those require an explicit `# noqa: PLC0415` with a one-line reason.

### `web.py` (when the module has routes)

If a module exposes HTTP routes, they live in `web.py`:

```python
# domain/foo/web.py
from fastapi import APIRouter
from app.core.webserver import DomainRoutes, register_domain_routes
from app.domain.foo import module

router = APIRouter(prefix=f"/api/{module.get_module_name()}", tags=[module.get_module_name()])

@router.get("/")
async def list_foos(): ...

routes = DomainRoutes(module=module.get_module_name())
routes.add_router(router)
register_domain_routes(routes)
```

`web.py` is imported by `__init__.py` so the registration runs at module load.

### Async-first

- All task entry points are `async def`, even when the body is synchronous. Enforced at registration time.
- All HTTP handlers are `async def`.
- All DB calls go through the async SQLAlchemy session.
- Where blocking work is unavoidable (e.g., a vendor SDK without an async API), wrap it in `asyncio.to_thread()` at the boundary.

### Pydantic at every boundary

- Task inputs are Pydantic models, validated by the task runner before the handler runs.
- HTTP request/response bodies are Pydantic models (FastAPI handles this).
- Webhook payloads are parsed into Pydantic models before any business logic runs.
- Internal cross-module calls do not need Pydantic — use plain types/dataclasses where it makes sense.

### Imports

- **Absolute imports only.** No relative imports.
- **Module-level imports only.** No inline `import` statements except the heavy-ML exception above.
- **No `__init__.py` self-imports.** Don't `from app.core.foo import bar` from within `app.core.foo/*.py` — use direct internal paths.

### Exceptions

- **Don't catch exceptions where they're raised.** Let them propagate.
- Catch only at a few designated **top-level boundaries**:
  - The HTTP middleware (converts unhandled exceptions to 500 JSON; logs).
  - The `core/tasks` task runner wrapper (marks job failed, writes audit log entry, surfaces to UI status).
  - A thin retry wrapper around vendor SDK calls (retries `VCSTransientError`, `VCSRateLimitError`, model-API 5xx).
  - Tests.
- Domain functions either succeed and return, or raise. They do not `try/except` to translate one error into another unless that translation is genuinely the function's responsibility.
- Use typed exception hierarchies (`VCSError` + subclasses, `LLMError` + subclasses) so callers that *do* catch can dispatch by type.

## Frontend conventions

These apply to `apps/web/`. Backend conventions above use Python-specific examples; FE has analogous rules with TypeScript idioms.

### Standard module file layout (FE)

```
src/domain/foo/                       (or src/core/foo/)
├── index.ts        # public interface — re-exports the module's named exports
├── routes.tsx      # (if the module has routes) TanStack Router route definitions
├── pages/          # one file per top-level page in this module
├── components/     # module-local components
├── hooks/          # module-local hooks (queries, mutations, custom logic)
├── api.ts          # TanStack Query hooks wrapping `core/api`'s client for this module's endpoints
├── store.ts        # (if the module needs cross-component client state) Zustand slice
├── types.ts        # types owned by this module
└── test/           # *.test.ts / *.test.tsx
```

`index.ts` is the only file other modules import from. Everything inside is internal.

### `index.ts` shape

```typescript
// src/domain/foo/index.ts
export { FooListPage, FooDetailPage } from "./pages";
export { useFooList, useFoo } from "./hooks";
export type { Foo } from "./types";

// Route registration (runs at import time)
import { registerRoutes } from "@core/routing";
import { fooRoutes } from "./routes";
registerRoutes(fooRoutes);
```

Pattern mirrors backend `__init__.py`: re-exports + registration side effects.

### API client usage

- `core/api` exposes the `openapi-fetch` client and the shared `QueryClient`.
- Each domain module wraps the endpoints it needs in TanStack Query hooks in its own `api.ts`:

```typescript
// src/domain/tickets/api.ts
import { useQuery } from "@tanstack/react-query";
import { client } from "@core/api";

export function useTicketList(filters: TicketFilters) {
  return useQuery({
    queryKey: ["tickets", filters],
    queryFn: async () => {
      const { data, error } = await client.GET("/api/tickets", { params: { query: filters } });
      if (error) throw error;
      return data;
    },
  });
}
```

- Cache keys are arrays starting with the module name (`["tickets", ...]`, `["repos", ...]`) so invalidations are scoped.

### OpenAPI codegen workflow

- The backend exposes its schema at `/openapi.json` in dev.
- `pnpm dev` runs an `openapi-typescript` watch step that regenerates `apps/web/src/core/api/generated/openapi.d.ts` whenever the backend's schema changes.
- The generated file IS committed (so anyone can read types without running codegen).
- CI runs the same generation and fails if the committed file differs from the freshly-generated one. Drift = your PR needs to include the regenerated types.

### Error handling at the API boundary

- **Generic toast on every non-2xx** for queries and mutations that don't have a more specific handler. One global `onError` on the `QueryClient` triggers a `notify.error(...)` from `core/notifications` saying "Something went wrong; try again."
- **Form validation errors (4xx with field-level details)** are caught by the mutation handler, parsed, and applied to the corresponding react-hook-form fields via `setError`. The form displays inline messages; no toast.
- **Component-local errors** (a single failed query in a list view) render an inline error state in the component, with a "Retry" button calling `refetch()`. Toast only when the error is global (mutation, navigation, etc.) and no inline UI makes sense.
- **Boundary catches** (`<ErrorBoundary>`) wrap each route at `core/routing`. Catches unhandled render errors; shows a "this page broke" fallback.

### Functional first (FE)

- Function components only. No class components.
- Hooks for shared logic.
- Zustand for cross-component client state; React state for component-local state; TanStack Query for server state.
- No HOCs unless a library forces it.

### Async-first (FE)

- All data fetching through TanStack Query hooks; never raw `useEffect(() => { fetch(...) })`.
- All mutations through `useMutation`.

### Imports (FE)

- Absolute imports only, via the workspace TS path aliases (`@core/...`, `@domain/...`, `@shared/...`).
- No deep imports across modules — only what's in another module's `index.ts`.

## Decoupling

### Registry pattern

The only mechanism for `core` to invoke `domain` (or `domain` to invoke `plugins`).

- `core` defines a registry (a dict, a list, a function that appends to global state).
- `domain` modules call `register_X(...)` at import time, at the bottom of their `__init__.py`.
- Bootstrap is responsible for importing the right modules so registration runs.

Used for: background tasks, HTTP routers, SSE event types, plugin instances (`VCSPlugin`, `LLMProvider`, `ExecutorPlugin`), audit-log writers, anywhere else "core needs to call domain."

### Bootstrap composition order

`apps/backend/app/main.py` (or equivalent entrypoint module) MUST follow this order:

1. **Load environment** (multi-file `.env` precedence, see below).
2. **Configure core infrastructure** (database engine, OTel SDK, structlog).
3. **Initialize cross-cutting runtimes** — `core/tasks` scheduler + `core/events` pub/sub bus. Must be ready before any domain module attempts to register tasks or subscribe to events.
4. **Import domain modules** — this triggers their registration side effects.
5. **Import plugin modules** — this registers them into the `vcs`, `llm`, and `executor` registries.
6. **Mount routers** — after all `register_domain_routes(...)` calls have run.
7. **Start the server** (M01) **or server + worker** (M02+ when TaskIQ is wired).

If you flip steps 3 and 5, you'll mount a router before its domain module has registered itself. The order is load-bearing.

## Configuration

### Multi-file `.env` precedence

```
.env.{ENV}.local   # gitignored, developer-specific overrides
.env.{ENV}         # checked in per environment (dev/test/staging/prod)
.env               # checked in, defaults
.env.sample        # checked in, documentation
```

Earlier files win. `pydantic-settings` reads them in that order with `override=False`.

### Runtime config in DB

Anything user-editable through the UI (model API keys, agent prompts, repo allowlist, lessons) lives in Postgres, not env vars. Sensitive columns (model API keys) are encrypted at rest with a key from the boot-time env.

## Database

### Session factory

Single async SQLAlchemy session factory in `core/database`. Domain modules consume it via a `get_session()` dependency. Transactions are scoped to the HTTP request or to the background task.

### Idempotent migration helpers

`core/database` exposes idempotent helpers that wrap Alembic's `op.*` operations:

```python
from app.core.database.migration_helpers import (
    create_table_if_not_exists,
    add_column_if_not_exists,
    create_index_if_not_exists,
    drop_column_if_exists,
    # ...
)
```

Every Alembic migration uses these helpers instead of raw `op.create_table` / `op.add_column`. Re-running a half-applied migration is always safe — operations check existence before acting.

### Per-migration tracking

Use a per-migration tracking model: a `schema_migrations` table records every applied migration by version. When a different branch is deployed (e.g., an older branch without the latest migrations), the system reads tracked versions, compares against migration files on disk, ignores missing files (already applied), and only executes versions not yet tracked. This is more robust than Alembic's default single-pointer-revision model for monorepo / multi-developer workflows where multiple migration heads can briefly exist.

## Testing

### Three categories

| Category | Where | What it tests | External deps |
|---|---|---|---|
| **Unit** | Inside the module: `<module>/test/test_*.py` | Pure logic (parsers, formatters, rule evaluators). Used sparingly — only for tricky pure code. | None |
| **Integration** | Inside the module: `<module>/test/test_*.py` | A module's public interface end-to-end. **The primary form** for backend logic. | Real Postgres (transactional rollback). Outbound HTTP mocked via `pytest-httpx`. **No external services need to be running.** |
| **E2E** | Top-level `apps/e2e/` workspace | Full stack via browser: SPA → backend → DB → real external services. Smoke + critical user flows. | **Real everything.** Real backend process, real Postgres, real Anthropic, real GitHub (test org / test App / test repos). No mocks. |

### Integration tests (the default)

- Exercise a module's public interface, not its internals. Test what consumers actually call.
- **DB:** real Postgres. Each test runs inside a transaction that is rolled back at the end. The session fixture begins a transaction, yields the session, and rolls back unconditionally. No cleanup code; no inter-test state.
- **Inbound HTTP (testing yaaof's endpoints):** `fastapi.testclient.TestClient`. No network; runs in-process against the ASGI app.
- **Outbound HTTP (yaaof calling GitHub, Anthropic):** mocked with `pytest-httpx`. Define expected requests + canned responses per test. No real network calls.
- **LLM calls:** cached on disk via the LLM cache (see below). Tests are deterministic and offline after cache is populated.

### E2E tests

- Live in `apps/e2e/` (pnpm workspace; TypeScript Playwright).
- **Full environment, no mocks.** Real backend, real Postgres, real Anthropic API, real GitHub (test org / test App / test repos). Whatever yaaof talks to in production, e2e talks to too.
- The test environment is provisioned ahead of CI runs: a dedicated test GitHub org with a yaaof-test App installed, a dedicated Anthropic API key with a budget cap, a fresh Postgres per run.
- Cover **golden-path user flows and critical regressions**, not exhaustive coverage. Each e2e test is expensive (real LLM tokens, real GitHub API quota, minutes of wall time) — keep the set small.
- **Assertions are behavioral, not exact.** "A review was posted under the architecture identity." "Verdict is CHANGES_REQUESTED." "An audit-log entry exists for the review attempt." Avoid asserting on exact LLM output text — it's non-deterministic.
- **The reason e2e exists despite integration tests:** integration tests mock external HTTP, so they can't catch breaks in our real GitHub App auth, webhook signature handling, Anthropic SDK contract changes, or any production-only oddity. E2E catches those.

### **Every new feature must ship with appropriate tests**

The exact mix depends on the feature, but every PR that adds user-facing behavior must include:

1. **Integration tests** for the backend logic the feature touches.
2. **E2E test(s)** covering the user-visible flow when the feature is reachable from the UI. If a feature is purely backend (e.g., a new background-task type), an integration test that exercises the full module pipeline is sufficient and no e2e is required.

When in doubt: add the e2e test. They catch the integration cracks unit and integration tests miss.

### DI over `@patch`

- **No `@patch` decorators**, no `mock.patch()` context managers, no `mocker.patch()`. Enforced by an AST scanner (`bin/check_patch_usage` or similar) wired into CI that flags any use of `unittest.mock.patch`, `mock.patch`, or `mocker.patch`.
- Substitute dependencies by **dependency injection**: pass collaborators in as arguments (constructor for the rare classes, function parameters for the common case).
- The few times an override is genuinely needed (e.g., a singleton that's hard to inject around): use a file-level `# override-patch-prevention:file` comment with an explanation.

### LLM testing cache

- An `LLMTestCache` wraps the Anthropic SDK and records request/response pairs to disk on first run.
- Cache is **file-colocated** alongside the test module (`<test_dir>/.llm_cache.json`).
- Workflow: write the test → run with `--allow-llm-calls` to populate the cache → commit the cache file → subsequent runs are deterministic and offline.
- Cache invalidation triggers: prompt change, model parameter change, input content change.

### Pytest plugin entry-point pattern

Define cross-cutting fixtures (transactional DB session, LLM cache setup, `pytest-httpx` setup) in a small in-repo pytest plugin module. Register via `[project.entry-points."pytest11"]` in `pyproject.toml` so it auto-loads — no conftest gymnastics.

## Observability

### Structured logging

- `structlog` everywhere. JSON output to stdout.
- A small `Logger` wrapper in `core/observability` injects request/trace context via a structlog filter.

### Context-variable threading

- A single `request_meta_var: ContextVar` carries `{request_id, workflow, user, …}` through async code.
- Web middleware sets it on every request. Workers set it before dispatching a task. Log filters read from it on every log line. Span attributes read from it on every span.

### OpenTelemetry

- OTel SDK initialized in `core/observability`.
- HTTP, SQLAlchemy, and background tasks all auto-instrumented (HTTP + SQLAlchemy via OTel contrib; tasks instrumented inside `core/tasks`).
- Trace + span IDs attached to every log line.

## Decisions

### 2026-05-13 — Core conventions locked
Registry-only decoupling between layers; functional-first code style; `__all__` in every `__init__.py`; Pydantic at every boundary; async everywhere; absolute imports only.

### 2026-05-13 — No shared workspace package
Portable primitives live in `apps/backend/app/core/`. No `packages/yaaof_core`.
**Why:** one Python service. Extract only if a second one appears.

### 2026-05-13 — Test discipline + no DB mocking
DI-over-patch ban (via the `check_patch_usage` AST scanner), LLM cache (file-colocated, Anthropic-shaped), pytest plugin entry-point. All DB tests run inside a transaction rolled back at teardown.

### 2026-05-14 — Three test categories: unit / integration / e2e
Integration tests are the primary form for backend logic (real Postgres transactional, outbound HTTP mocked with `pytest-httpx`, inbound via `fastapi.testclient.TestClient`, **no external services need to be running**). E2E tests live in `apps/e2e/` (TypeScript Playwright) and cover golden-path user flows against the **full real stack — real Anthropic, real GitHub test org, no mocks**. Earlier wording called integration tests "service tests" — that was wrong terminology.

### 2026-05-14 — Every new feature ships with appropriate tests
Integration tests for the backend logic touched; e2e tests for user-visible flows. When in doubt, add the e2e test.

### 2026-05-13 — Idempotent migrations from day one
Port `*_if_not_exists` helpers. Every migration is safely re-runnable.

### 2026-05-13 — Per-migration tracking (Ecto-style)
Adopt a `schema_migrations` table pattern rather than Alembic's single-pointer model.
**Why:** safe branch switching without orphan-recovery pain.

### 2026-05-14 — Standard per-module file layout: `__init__.py`, `module.py`, `web.py`
`module.py` provides `get_module_name()`; `web.py` is the standard location for FastAPI routes when a module exposes them; `__init__.py` is interface declaration + registration only.
**Why:** consistency across modules; renames touch one file; tooling and humans can find routes / module identity in known locations.
