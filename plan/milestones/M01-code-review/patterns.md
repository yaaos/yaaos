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
# Examples: registering a plugin (register_vcs_plugin, register_coding_agent_plugin, register_workspace_provider),
# registering HTTP routes (register_routes(RouteSpec(...)) — done in web.py),
# subscribing to SSE event types, etc.
# Background coroutines are NOT registered by name — they're spawned directly
# via core/primitives.spawn(name, coro) at the call site.
```

Rules:

- `__all__` is **always** present and lists the module's public API. Tach uses it to compute the interface.
- All implementation lives in named submodules (`service.py`, `models.py`, `web.py`, etc.). **No business logic in `__init__.py`.**
- Re-exports come first. Then `__all__`. Then registration calls.
- No lazy/conditional imports except for heavy ML model loading; those require an explicit `# noqa: PLC0415` with a one-line reason.

### `web.py` (when the module has routes)

If a module exposes HTTP routes, they live in `web.py`:

```python
# domain/foo/web.py
from fastapi import APIRouter
from app.core.webserver import RouteSpec, register_routes
from app.domain.foo import module

# Router carries NO prefix — core/webserver applies it from RouteSpec.
router = APIRouter()

@router.get("/")
async def list_foos(): ...

register_routes(RouteSpec(
    module_name=module.get_module_name(),
    router=router,
    # url_prefix defaults to f"/api/{module_name}". Override only if needed.
    # on_startup=[_startup_recovery] for modules with crash-recovery hooks.
))
```

`web.py` is imported by `__init__.py` so the registration runs at module load. See [internals/webserver.md § One URL prefix per module](internals/webserver.md#one-url-prefix-per-module-enforced) for the validation rules `register_routes` enforces.

### Async-first

- All task entry points are `async def`, even when the body is synchronous. Enforced at registration time.
- All HTTP handlers are `async def`.
- All DB calls go through the async SQLAlchemy session.
- Where blocking work is unavoidable (e.g., a vendor SDK without an async API), wrap it in `asyncio.to_thread()` at the boundary.

### Filesystem + process work happens inside a workspace, never in yaaof's process

- Never touch the filesystem directly (`open()`, `pathlib`, etc.) for repo/code content.
- Never spawn processes directly (`subprocess`, `asyncio.subprocess`) for repo/code work.
- Always go through `core/workspace`: `async with with_workspace(provider_id, spec) as ws:`, then call `await ws.run_coding_agent_cli(argv=..., env=..., stdin=..., timeout_seconds=...)`. The workspace decides where/how the CLI runs (host tempdir today, container later) and owns subprocess + timeout + process-group semantics. Consumers never see internal paths like `working_dir`; the Protocol exposes operations, not paths.
- The yaaof Python process itself only does pure-Python work — HTTP, DB queries, in-memory orchestration. Anything needing git, tests, builds, or untrusted code execution lives inside a workspace (spawned by the coding-agent plugin via `run_coding_agent_cli`, never directly by domain code).
- Exceptions: `core/database` (Postgres connections), `core/observability` (writing log files), and the few other infrastructure modules that genuinely need direct OS access. None of those touch repo content.

### Pydantic at every boundary

- HTTP request/response bodies are Pydantic models (FastAPI handles this).
- Webhook payloads are parsed into Pydantic models before any business logic runs.
- Coding-agent CLI stdout is parsed into a plugin-internal Pydantic class by the plugin, then converted to vendor-neutral domain types (`vcs.Finding` for reviews) before returning to the caller. Consumers never see plugin-internal shapes.
- Audit-log payloads are Pydantic models (see [Audit log discipline](#audit-log-discipline)); plain dicts are rejected.
- Background coroutines spawned via `core/primitives.spawn()` take a Pydantic input model as their argument when one is passed (see the `ReviewJobInput` example in [internals/reviewer.md](internals/reviewer.md)).
- Internal cross-module calls do not need Pydantic — use plain types/dataclasses where it makes sense.

### Imports

- **Absolute imports only.** No relative imports.
- **Module-level imports only.** No inline `import` statements except the heavy-ML exception above.
- **No `__init__.py` self-imports.** Don't `from app.core.foo import bar` from within `app.core.foo/*.py` — use direct internal paths.

### Exceptions

- **Don't catch exceptions where they're raised.** Let them propagate.
- Catch only at a few designated **top-level boundaries**:
  - The HTTP middleware (converts unhandled exceptions to 500 JSON; logs).
  - The `core/primitives.spawn()` wrapper around background coroutines (logs the failure with structured context + OTel span; the spawned coro itself is responsible for marking its domain row failed before raising).
  - A thin retry wrapper around vendor SDK calls (retries `VCSTransientError`, `VCSRateLimitError`, model-API 5xx).
  - Tests.
- Domain functions either succeed and return, or raise. They do not `try/except` to translate one error into another unless that translation is genuinely the function's responsibility.
- Use typed exception hierarchies (`VCSError` + subclasses, `CodingAgentError` + subclasses, `WorkspaceError` + subclasses) so callers that *do* catch can dispatch by type.

## Frontend conventions

These apply to `apps/web/`. Backend conventions above use Python-specific examples; FE has analogous rules with TypeScript idioms.

### Dumb frontend — no business logic

The SPA renders data and dispatches actions; it does not own, compute, or decide anything about yaaof's behavior. **Any rule the backend doesn't also enforce is not a rule.** See [architecture.md § Dumb frontend](architecture.md#dumb-frontend-all-business-logic-in-the-api) for the full statement.

Practically:

- **Forms** — frontend validations (zod schemas, react-hook-form rules) exist for input immediacy. The backend re-validates with its own Pydantic schema; the API returns 4xx with a field-keyed error map on failure, and the form surfaces those errors. Don't ship a rule on the frontend that the backend doesn't also have.
- **Verdicts / status / derivations** — never computed client-side. If the UI needs a "verdict" label, the API returns it. If the UI needs a count, the API returns it.
- **Permissions** — show/hide based on a server-supplied capability flag (`can_edit: bool` on the resource), never on a client-side rule. M01 has no auth so every flag is `true`; the shape is in place for the future.
- **Cache invalidation** — TanStack Query keys are invalidated by mutation responses and SSE events from the backend. No client-side "I bet this is stale now" heuristics.
- **Search / filter / sort** — client-side filtering of an already-fetched list is fine for snappy UX (e.g., filter-as-you-type over the current page). Filtering that changes which rows the user *acts on* (bulk-select, delete-all-matching) goes through the API so the server applies the same filter.

If a frontend change could alter what gets stored, posted, or counted without a corresponding API change, the logic is in the wrong place.

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

Used for: HTTP routers (`core/webserver.register_routes`), SSE event types, plugin instances (`VCSPlugin`, `CodingAgentPlugin`, `WorkspaceProvider`), startup-recovery hooks (via `RouteSpec.on_startup`), anywhere else "core needs to call domain."

### Bootstrap composition order

`apps/backend/app/main.py` (or equivalent entrypoint module) MUST follow this order. Each step's side effects happen *at import time* of the named modules — Python's import semantics do the sequencing, but the import order is load-bearing.

1. **Load environment.** `core/config` runs first (pydantic-settings reads `.env` + process env, validates).
2. **Configure core infrastructure.** Import `core/database` (engine creation), `core/observability` (structlog + conditional OTel SDK — only initialized if `OTEL_EXPORTER_OTLP_ENDPOINT` is set), `core/primitives` (no side effects, but other modules import from it).
3. **Initialize the events bus.** Import `core/events` *before any domain module*. The pub/sub registry must exist before domain modules subscribe to events at their own import time.
4. **Import the webserver registry.** Import `core/webserver` *before any domain module*. The `_specs` dict must exist before `register_routes(RouteSpec(...))` calls fire.
5. **Import domain modules.** All eight: `vcs`, `settings`, `repos`, `intake`, `tickets`, `pull_requests`, `memory`, `reviewer`. Each `__init__.py` runs its registration side effects (route specs, event subscribers, plugin Protocol registries).
6. **Import plugin modules.** All three: `plugins/github`, `plugins/claude_code`, `plugins/in_process_workspace`. Each calls `register_vcs_plugin` / `register_coding_agent_plugin` / `register_workspace_provider`.
7. **Optionally wrap with test scaffolding.** If `YAAOF_CODING_AGENT_STUB` (or any future testing-layer flag) is set, conditionally import the relevant `app.testing.*` module and call its wrap helper. This is the **only** import of `app.testing.*` from production bootstrap code; tach forbids `core/`, `domain/`, and `plugins/` from depending on `testing/`. In a stripped production wheel (no `app/testing/` present), the import fails loud rather than silently allowing test-mode behavior.
8. **Construct the FastAPI app.** Call `core.webserver.create_app()`. The lifespan body mounts routers from `_specs`, then runs `on_startup` hooks, then yields.
9. **Run the server.** `uvicorn` (or equivalent) takes over.

Example `main.py` skeleton:

```python
# apps/backend/app/main.py
from app.core import config          # 1
from app.core import database        # 2
from app.core import observability   # 2
from app.core import primitives      # 2
from app.core import events          # 3
from app.core import webserver       # 4
from app.domain import vcs, settings, repos, intake, tickets, pull_requests, memory, reviewer   # 5
from app.plugins import github, claude_code, in_process_workspace                                # 6

import os                                                                                        # 7
if os.environ.get("YAAOF_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
    from app.testing.stub_coding_agent import wrap_all_registered_plugins
    wrap_all_registered_plugins()

from app.core.webserver import create_app

app = create_app()                   # 8
# uvicorn entrypoint runs `app`     # 9
```

If you flip steps 3–4 with step 5, you'll mount a router before its domain module has registered itself, or subscribe to an event before the bus exists. The order is load-bearing.

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

**Implementation shape:**
- Migration files live in `apps/backend/alembic/versions/` and use Alembic's file format + autogeneration tooling.
- The runner is **not** stock `alembic upgrade head` — `core/database` exposes a `migrate()` function that: reads applied versions from `schema_migrations`, scans `alembic/versions/*.py`, and applies any file whose `revision` isn't in the table (in `down_revision` order), inserting into `schema_migrations` after each.
- The stock Alembic CLI is **only** used for `alembic revision --autogenerate -m "..."` to scaffold new migration files. Running `alembic upgrade` directly is forbidden — use `core/database.migrate()` (or `bin/migrate` which wraps it).
- The `schema_migrations` table is created by an idempotent bootstrap step in `core/database` on first connect; no migration creates it.

## Testing

yaaof tests run entirely against a **self-contained Docker test stack** — no real GitHub, no real Anthropic, no shared test environment. The stack consists of:

1. **Postgres 16** — pre-seeded with realistic fixture data (see below).
2. **`apps/fake-github`** — a fake GitHub HTTP service (peer app) that fakes every `api.github.com` endpoint yaaof calls. JWT auth, HMAC-signed webhook dispatch to yaaof, in-memory state for what yaaof has posted. Same env var (`GITHUB_API_BASE_URL`) points yaaof at it; the github plugin code is unchanged.
3. **Coding-agent CLI cache** — file-colocated `.coding_agent_cache.json` per test module. Captures `(method, ReviewContext|ReplyContext) → (ReviewResult|ReplyResult)` on first run with `--allow-coding-agent-calls`; replays on every subsequent run. Real Claude Code CLI is **never** invoked in CI.

This means tests run anywhere, offline, deterministically, with no rate-limit exposure and no credential management.

### Three categories

| Category | Where | What it tests | External deps |
|---|---|---|---|
| **Unit** | Inside the module: `<module>/test/test_*.py` | Pure logic (parsers, formatters, rule evaluators). Used sparingly — only for tricky pure code. | None |
| **Integration** | Inside the module: `<module>/test/test_*.py` | A module's public interface end-to-end. **The primary form** for backend logic. | Real Postgres (transactional rollback, empty DB). `apps/fake-github` for any GitHub interaction. CLI cache for any coding-agent invocation. |
| **E2E** | Top-level `apps/e2e/` workspace | Full stack via browser: SPA → backend → DB → fake-github → cached coding-agent. Smoke + critical user flows. | Real backend process. Real Postgres, **pre-seeded** with fixture data. `apps/fake-github` for GitHub. CLI cache. Everything containerized via `docker-compose.test.yml`. |

### Integration tests (the default)

- Exercise a module's public interface, not its internals. Test what consumers actually call.
- **DB:** real Postgres. Each test runs inside a transaction that is rolled back at the end. The session fixture begins a transaction, yields the session, and rolls back unconditionally. **Empty DB at the start of each test** — no fixture data preloaded. Tests insert what they need.
- **Inbound HTTP (testing yaaof's endpoints):** `fastapi.testclient.TestClient`. No network; runs in-process against the ASGI app.
- **Outbound HTTP (yaaof calling GitHub):** routed to `apps/fake-github` via `GITHUB_API_BASE_URL`. Real plugin code paths, including JWT signing + HMAC verification. The fake service is shared across all integration tests in a test session; tests use the fake's `POST /__test/...` control endpoints to seed expected responses where needed.
- **Coding-agent CLI invocations:** the `claude_code` plugin is tested via the coding-agent CLI cache. Tests are deterministic and offline after the cache is populated.

### E2E tests

- Live in `apps/e2e/` (pnpm workspace; TypeScript Playwright).
- **Run against the test stack:** `docker compose -f docker/docker-compose.test.yml up` brings up Postgres (seeded) + fake-github + yaaof. Playwright drives the browser; assertions check the UI + DB + audit log.
- **Pre-seeded DB.** The compose stack runs `apps/backend/bin/seed_test_data` after migrations: 2 repos, 5 tickets across states, 4 lessons, 3 reviewer agents (the built-ins), a "configured" GitHub App install row, a "configured" Anthropic API key, some audit entries. Seed data evolves with the schema via the ORM models — no SQL fixtures.
- **Triggering flows:** tests call `apps/fake-github`'s `POST /__test/dispatch_webhook` to simulate "a PR was opened on the configured repo." Fake-github signs the payload with the shared HMAC secret and POSTs to yaaof. The full real flow runs (intake → reviewer → coding-agent invocation via CLI cache → vcs.post_review → fake-github records the post).
- Cover **golden-path user flows and critical regressions**, not exhaustive coverage. Each e2e test is cheap (no real LLM tokens, no real GitHub quota, seconds of wall time) but still slower than integration — keep the set small.
- **Assertions are behavioral.** "A review was posted under the architecture identity." "Verdict is CHANGES_REQUESTED." "An audit-log entry exists for the review attempt." Avoid asserting on exact rendered text where templates may evolve.
- **The reason e2e exists despite integration tests:** integration tests run in-process; e2e tests run the real ASGI server, real browser, real cross-container HTTP — catching wire-format bugs, lifespan-order bugs, and frontend ↔ backend integration cracks that in-process tests don't.
- `apps/e2e/bin/ci` brings up the test stack, runs Playwright, tears down. No external credentials required. Always runs in CI.

### Claude Code CLI contract — manual quarterly regeneration

The CLI cache makes tests deterministic, but it also means **CI never invokes real Claude Code**. If Anthropic ships a Claude Code release with a new JSON output shape, our cache stays green while production breaks.

**Process:** quarterly (or before any major release), a developer with a real Anthropic key reruns the test suite with `--allow-coding-agent-calls`, eyeballs the diff in the cache files, and commits the regenerated cache. This catches drift within ~3 months. Accepted trade-off: zero CI cost, occasional manual ritual.

### **Every new feature must ship with appropriate tests**

The exact mix depends on the feature, but every PR that adds user-facing behavior must include:

1. **Integration tests** for the backend logic the feature touches.
2. **E2E test(s)** covering the user-visible flow when the feature is reachable from the UI. If a feature is purely backend (e.g., a new background coroutine or periodic loop), an integration test that exercises the full module pipeline is sufficient and no e2e is required.

When in doubt: add the e2e test. They catch the integration cracks unit and integration tests miss.

### DI over `@patch`

- **No `@patch` decorators**, no `mock.patch()` context managers, no `mocker.patch()`. Enforced by ruff's `flake8-tidy-imports.banned-api` rule (TID251), configured in `apps/backend/pyproject.toml` to ban `unittest.mock.patch`, `unittest.mock.patch.object`, `unittest.mock.patch.multiple`, and `mock.patch`. `mocker.patch` is impossible by construction: `pytest-mock` is not a dependency, so the `mocker` fixture doesn't exist.
- Substitute dependencies by **dependency injection**: pass collaborators in as arguments (constructor for the rare classes, function parameters for the common case).
- The few times an override is genuinely needed (e.g., a singleton that's hard to inject around): use a per-line `# noqa: TID251` with an explanation, or `# ruff: noqa: TID251` at the top of the file to opt the whole file out.

### Coding-agent CLI cache

yaaof doesn't call LLM APIs directly in M01; it shells out to the Claude Code CLI. Tests don't invoke the real CLI (slow, costly, non-deterministic, requires Anthropic credentials), so the `claude_code` plugin runs against a **CLI invocation cache** in every test layer:

- A test-mode wrapper around the plugin's `review` / `reply` methods captures `(method, context) → result` on first run.
- Cache is **file-colocated** alongside the test module (`<test_dir>/.coding_agent_cache.json`).
- Workflow: write the test → run with `--allow-coding-agent-calls` to populate the cache → commit the cache file → subsequent runs are deterministic and offline.
- Cache invalidation triggers: any change to the `ReviewContext` / `ReplyContext` (PR, diff, lessons, persona, agent_config). The plugin's prompt assembly may change without invalidating the cache, since the cache key is over the input context, not the assembled prompt — that's deliberate: prompt-engineering changes inside the plugin don't force a recapture of every test.
- **Both integration AND e2e tests use the cache.** No layer invokes the real CLI in CI.
- CLI contract drift is caught by the quarterly manual regeneration ritual described above.

When `core/llm` returns in M02+ (for non-CLI LLM calls yaaof makes itself — summarization, lesson scoring, etc.), it gets a sibling cache with the same shape.

### Time controls (configurable timings)

Several flows have wall-clock waits that production wants long (reasonable batching) but tests want short or zero. Each gets an env var with prod defaults:

| Variable | Default | Description |
|---|---|---|
| `YAAOF_REVIEW_DEBOUNCE_SECONDS` | `30` | Reviewer waits this long before starting a review_job. Tests set to `0`. |
| `YAAOF_REAPER_INTERVAL_SECONDS` | `30` | How often the workspace reaper sweeps. Tests set to `1` or call the sweep function directly. |
| `YAAOF_HEARTBEAT_INTERVAL_SECONDS` | `10` | How often the review-job heartbeat coro bumps `last_heartbeat_at`. |
| `YAAOF_CATCHUP_DELAY_SECONDS` | `10` | Boot-time delay before the GitHub catch-up coro runs (lets the rest of the app initialize first). |

Test stacks set all four to their fast values via the compose file. Code that uses these timings reads them from `core/config` settings, never hardcodes them.

### Pytest plugin entry-point pattern

Define cross-cutting fixtures (transactional DB session, coding-agent CLI cache setup, fake-github base URL) in a small in-repo pytest plugin module. Register via `[project.entry-points."pytest11"]` in `pyproject.toml` so it auto-loads — no conftest gymnastics.

## Observability

### Structured logging

- `structlog` everywhere. JSON output to stdout.
- A small `Logger` wrapper in `core/observability` injects request/trace context via a structlog filter.

### Context-variable threading

- A single `request_meta_var: ContextVar` carries `{request_id, workflow, user, …}` through async code.
- Web middleware sets it on every request. `core/primitives.spawn()` propagates the parent's context vars into the spawned coroutine. Log filters read from it on every log line. Span attributes read from it on every span.

### OpenTelemetry

- OTel SDK initialized in `core/observability`.
- HTTP, SQLAlchemy, and background coroutines all auto-instrumented (HTTP + SQLAlchemy via OTel contrib; coroutines get a span attached by `core/primitives.spawn()` at spawn time).
- Trace + span IDs attached to every log line.

### What to instrument with a manual span

Auto-instrumentation covers most things. Add a **manual span** only at meaningful boundaries:

- **Every external call** — VCS API requests, coding-agent CLI invocations, webhook signature verification, any network egress. Attributes: vendor, endpoint, retry-count, outcome.
- **Every plugin entry point** — `VCSPlugin.post_review`, `CodingAgentPlugin.invoke`, `WorkspaceProvider.provision`. Attributes: `plugin_id`.
- **Long phases inside a background coro** — for review_jobs, the `assembling_prompt` / `invoking_agent` / `posting_review` phases each get a span so the trace shows where the wall time went.

**Don't** wrap every domain function in a manual span — auto-instrumentation already covers the HTTP-to-DB path, and noise hurts more than detail helps. If a function isn't an external call, a plugin boundary, or a long phase, it doesn't need a span.

## Cross-cutting discipline

These rules apply to every module. They're short because each is meant to fit in your head.

### Three sinks: log / trace / audit

Three sinks, three purposes. One event may legitimately appear in all three — the rules differ.

| Sink | Purpose | Lifetime | Audience |
|---|---|---|---|
| **Log** (`structlog` → stdout) | Ephemeral signal for ops debugging — every request, every retry, every transient hiccup. | Days; truncated by log retention. | On-call engineer reading recent activity. |
| **Trace** (OTel spans) | Causal request graph — who called whom, how long, with what attributes. | Days; sampled by the collector. | Engineer debugging latency or causal chain. |
| **Audit** (`audit_log` table) | Durable record of business-meaningful state changes — who did what, when, to which entity. | 90 days (M01 deferred); permanent for now. | Operator / user reviewing what happened. |

Rules:
- **Every log line carries trace + span IDs** (already done by the structlog filter) so the three sinks correlate.
- **The audit log is for state changes with business meaning, not for debugging.** A failed DB read is a log line. A successful prompt update is an audit entry. A 500 is a log line and a span; it is *not* an audit entry.
- **Reads never write to `audit_log`.** Only mutations.
- **Tracing covers everything; logging covers most; auditing covers the smallest set.** When in doubt, log. When the answer to "would an operator want to know this happened to entity X?" is yes, also audit.

### Audit log discipline

Every domain module that owns an entity is responsible for writing audit entries on its mutations. The policy:

- **What gets an audit entry:**
  - Every user-initiated mutation (prompt edit, lesson create/edit/delete, repo add/remove, "re-review" button click).
  - Every agent-initiated action (review posted, reply posted, finding logged).
  - Every state transition with business meaning (review_job `queued → running → posted/failed/cancelled`; workspace `active → expired → destroyed`; ticket `in_review → complete`).
- **What doesn't:**
  - Internal helpers' progress steps (those are logs).
  - Reads.
  - Routine periodic sweeps that didn't do anything (the reaper that found zero workspaces to destroy).
- **The `kind` field follows `<entity>.<verb_past>`** — `review_job.scheduled`, `lesson.deleted`, `workspace.destroy_failed`. Lowercase, dotted, past tense. Grep-friendly.
- **The `actor` field is required and uses the `Actor` value object from `core/primitives`.** Kind is one of `github_user` / `agent` / `system`. Never put PII or secrets in the actor.
- **The `payload` is a Pydantic model**, not a raw dict. Each `kind` has a corresponding payload class. **Payload classes live in `<module>/audit_payloads.py`** in the owning module, named in PascalCase by the kind suffix (e.g., `review_job.posted` → `ReviewJobPostedPayload`). The class's fields are exactly the fields listed in that module's "Audit log entries" table in its `internals/<module>.md` deep-dive — that table is authoritative; the class definitions mirror it. This keeps the audit log self-describing and makes UI rendering type-safe.
- **One audit entry per business event.** Don't write three entries for "started, did the thing, finished" — write one entry for the outcome (or one per genuine state transition).

### Org scoping

Every domain function takes `org_id` as a kwarg. Every query is filtered by `org_id`. No exceptions, even in M01 where the value is constant.

```python
async def get_review_job(review_job_id: UUID, *, org_id: UUID) -> ReviewJob: ...
```

This is the discipline that makes the future RBAC retrofit a check, not a refactor. A function that "forgot" `org_id` in M01 is a security bug waiting in M02.

A future lint rule will flag domain functions that don't take `org_id`. For now, code review catches it.

### Idempotency at external boundaries

Any handler triggered by an external event MUST be idempotent under retry. Externals retry — networks drop, webhook deliveries duplicate, the operator clicks twice.

Patterns:
- **Deduplicate by external event id.** `plugins/github` already does this: `INSERT INTO github_webhook_events ... ON CONFLICT (source_event_id) DO NOTHING; if not inserted, skip dispatch`. Every plugin that receives external events follows the same shape.
- **Upserts use `ON CONFLICT`**, not "check then insert." Two requests racing must produce one row, not two.
- **State-transition functions must be safe to call twice.** `mark_failed(job_id)` on an already-failed job is a no-op, not an error. Compute the transition off the current state; only write if a transition is actually needed.
- **Treat "already processed" as success.** Returning 2xx to a duplicate webhook tells the sender to stop retrying.

### Input validation at boundaries

- **Every HTTP request body is a Pydantic model.** FastAPI validates on entry; handlers receive typed inputs. No `request.json()` raw-dict access.
- **Every external API response is parsed into a Pydantic model** before crossing back into yaaof code. VCS plugin parses GitHub JSON into `PullRequest` / `Finding` / etc. Coding-agent plugin parses CLI stdout into a plugin-internal model, then converts to vendor-neutral domain types before returning. If parsing fails, it's a known status value (`PARSE_FAILURE`), not an exception.
- **Past the boundary, every value is typed.** No raw dicts crossing module boundaries. If you need a "kitchen sink" shape internally, that's a Pydantic model with explicit fields, not `dict[str, Any]`.
- **Frontend validation is UX-only** (see [Dumb frontend](#dumb-frontend--no-business-logic)). The backend re-validates with its own Pydantic schema; the API returns 4xx with a field-keyed error map on failure.

### Time

- **UTC everywhere.** No local-time computation in backend code. Frontend converts to local time for display only.
- **All timestamp columns are `timestamptz`.** Naïve `timestamp` columns are forbidden.
- **`datetime.now(UTC)` in code, never `datetime.now()`** (which returns a naïve local-time datetime). A linter rule can enforce this when one of us adds it.
- **ISO 8601 over the wire** with explicit `Z` or `+00:00`. Pydantic's default serializer does this for `datetime`.
- **Durations are seconds (int or float)**, not strings. "30 seconds" is `30`, never `"30s"`.

### Secrets and PII

**Secrets:**
- Stored encrypted at rest in the owning plugin's settings table (Anthropic API key, GitHub App PEM + webhook secret). Encryption key is boot-time env.
- **Decrypted only at the call site that needs it.** No global "decrypted credentials" cache, no passing decrypted secrets across module boundaries when they're not needed.
- **Never logged, never echoed in error messages, never included in an audit entry payload.** If an exception message would contain a secret (e.g., HTTP client logging the request), redact before logging.
- A vendor SDK that wants the key takes it as a function argument at the call site. Plugins that wrap a vendor SDK construct the client per call with the decrypted secret, or hold it in a private attribute that's clearly scoped.

**PII:**
- yaaof doesn't intentionally store PII. Commit text, PR titles, agent comments, and human-supplied lesson bodies may contain incidental PII (names, emails).
- **Don't replicate user-supplied content into log lines.** Log identifiers (`pr_id`, `comment_external_id`), not content. The content is already durable in its primary table; the log doesn't need a copy.
- **Audit payloads keep what's needed for accountability** — actor identity, what changed, before/after if relevant. Not "the full prompt text" (use a hash; see the reviewer's `review_job.prompt_sent` payload for the pattern).

## Decisions

### 2026-05-13 — Core conventions locked
Registry-only decoupling between layers; functional-first code style; `__all__` in every `__init__.py`; Pydantic at every boundary; async everywhere; absolute imports only.

### 2026-05-13 — No shared workspace package
Portable primitives live in `apps/backend/app/core/`. No `packages/yaaof_core`.
**Why:** one Python service. Extract only if a second one appears.

### 2026-05-13 — Test discipline + no DB mocking
DI-over-patch ban (via ruff TID251), coding-agent CLI cache (file-colocated, captures CLI subprocess invocations), pytest plugin entry-point. All DB tests run inside a transaction rolled back at teardown.

### 2026-05-14 — Three test categories: unit / integration / e2e
Integration tests are the primary form for backend logic — real Postgres (transactional, empty start), `apps/fake-github` for outbound GitHub, CLI cache for coding-agent. E2E lives in `apps/e2e/` (Playwright) and runs against `docker-compose.test.yml` — real backend, pre-seeded Postgres, `apps/fake-github`, CLI cache.

### 2026-05-15 — Tests run entirely against a self-contained Docker stack; no real external services
No real GitHub, no real Anthropic, no shared test environment. `apps/fake-github` is a peer app that fakes every GitHub endpoint yaaof calls (JWT auth, HMAC-signed webhook dispatch, REST endpoints, test-control routes). Coding-agent CLI invocations replay from file-colocated caches. Pre-seeded Postgres provides realistic UI fixtures for e2e.
**Why:** real-services testing isn't operationally feasible (token management, rate limits, blowing through API quotas, shared-resource flakiness). A fake service is ~400 LOC of one-time work that gives us fast, offline, deterministic tests forever. The CLI contract is verified by a quarterly manual cache regeneration with real Anthropic credentials — accepted small risk for huge cost savings.

### 2026-05-14 — Every new feature ships with appropriate tests
Integration tests for the backend logic touched; e2e tests for user-visible flows. When in doubt, add the e2e test.

### 2026-05-13 — Idempotent migrations from day one
Port `*_if_not_exists` helpers. Every migration is safely re-runnable.

### 2026-05-13 — Per-migration tracking (Ecto-style)
Adopt a `schema_migrations` table pattern rather than Alembic's single-pointer model.
**Why:** safe branch switching without orphan-recovery pain.

### 2026-05-15 — Cross-cutting discipline locked
Three sinks (log / trace / audit) with distinct purposes and rules; audit entries only for business-meaningful mutations with `<entity>.<verb_past>` kinds and Pydantic payloads; every domain function takes `org_id` as a kwarg; external-trigger handlers are idempotent under retry; Pydantic at every boundary (in *and* out); UTC + `timestamptz` everywhere; secrets decrypted at call site, never logged / echoed / audited.
**Why:** these are the rules that prevent the slow drift toward inconsistent logging, partial audit coverage, multi-tenancy bugs, double-processed webhooks, naïve-datetime time bombs, and secret leakage. Each rule is short and grep-able; collected in one place so a new module author has one section to read instead of seven scattered conventions.

### 2026-05-15 — Manual spans only at meaningful boundaries
External calls, plugin entry points, and long phases inside a background coro get manual spans. Domain functions don't. Auto-instrumentation handles the HTTP-to-DB path.
**Why:** wrapping every function in a span turns the trace into noise. Detail at the boundaries is where latency questions actually get answered.

### 2026-05-14 — Standard per-module file layout: `__init__.py`, `module.py`, `web.py`
`module.py` provides `get_module_name()`; `web.py` is the standard location for FastAPI routes when a module exposes them; `__init__.py` is interface declaration + registration only.
**Why:** consistency across modules; renames touch one file; tooling and humans can find routes / module identity in known locations.

### 2026-05-16 — DI-over-@patch enforced by ruff TID251, not a custom AST scanner
Previously a custom Python AST scanner (`bin/check_patch_usage`) walked test files and flagged `mock.patch` / `mocker.patch` / `unittest.mock.patch`. Replaced by ruff's `flake8-tidy-imports.banned-api` rule, configured in `apps/backend/pyproject.toml`. Opt-out becomes standard `# noqa: TID251` (or `# ruff: noqa: TID251` for whole-file).
**Why:** ruff already does this; one fewer custom script to maintain; the opt-out mechanism is the standard one developers already know. `mocker.patch` is impossible by construction since `pytest-mock` isn't a dependency.
