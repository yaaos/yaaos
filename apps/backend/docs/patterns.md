# Backend patterns

Conventions applying to every backend module. For cross-app conventions (UTC, audit-log shape, HMAC) see [`docs/system-architecture.md`](../../../docs/system-architecture.md).

## Module documentation

Every shipped module has one `apps/backend/docs/<layer>_<module>.md` following this fixed template, in order:

1. **Purpose** — one paragraph. What the module owns; what it does not.
2. **Public interface** — what's exported from `__init__.py`, plus HTTP routes if any. No internals.
3. **Module architecture** — the internal shape, in this order:
   - **Entities** — DDD entities owned by this module. One bullet per entity: what it represents and what gives it identity.
   - **Key value objects** — only the load-bearing ones. One bullet, one sentence each.
   - **Core user flows** — short numbered steps for the main ways callers exercise this module. Prose; no code.
   - **State machines** — if any. States as bullets, transitions as a small table or `from → to` arrow notation.
4. **Data owned** — tables / persistent state owned by this module. Per-column purpose only when non-obvious.
5. **How it's tested** — unit / integration / e2e coverage. Where fixtures live.

Discipline still applies: terse, bullets, no code snippets, no `Decisions` section, link don't repeat. Modules with no entities / no state machines just omit those sub-sections — don't write "N/A".

## Code style

### Functional first

Functions are the default. Classes only for:
- Pydantic models (request/response, audit payloads, background-task inputs).
- Typed exception hierarchies (`VCSError`, `CodingAgentError`, `WorkspaceError`).
- Adapters / protocol shims.
- State containers with genuinely coupled methods + state (rare).

No "service classes". A module-level `async def` is the right shape for business logic.

### Async first

- All HTTP handlers `async def`.
- All DB access via the async SQLAlchemy session.
- Wrap unavoidable blocking work at the boundary with `asyncio.to_thread()`.

### Pydantic at every boundary

- HTTP bodies (FastAPI handles this).
- Webhook payloads parsed into Pydantic models before any business logic.
- Coding-agent CLI stdout parsed into a plugin-internal Pydantic model, then converted to vendor-neutral domain types (`vcs.Finding`).
- Audit payloads — every `kind` has a corresponding Pydantic class.
- Internal cross-module calls: plain types/dataclasses fine where they fit.

### Exceptions

Don't catch where raised. Let them propagate. Catch only at top-level boundaries:
- HTTP middleware (converts to 500 JSON).
- `core/primitives.spawn()` wrapper (logs the failure; the coro is responsible for marking its row failed before raising).
- Thin retry wrapper around vendor SDK calls.
- Tests.

Domain functions succeed or raise. No translation unless translation is genuinely the function's job.

### Filesystem + processes via `core/workspace`

Never touch the filesystem (`open()`, `pathlib`) or spawn processes (`subprocess`) directly for repo/code work. Always go through `with_workspace(...)` — the workspace decides where/how the CLI runs. Consumers never see internal paths; the Protocol exposes operations, not paths.

Exceptions: `core/database` (Postgres connections), `core/observability` (log files).

### Imports

- Absolute imports only.
- Module-level only (heavy-ML exception requires `# noqa: PLC0415`).
- Other modules import only `__all__` exports. Internal cross-module imports are Tach-rejected.

## Background work

### `core/primitives.spawn()`

Every fire-and-forget background coroutine goes through this single helper. Behaviour:
- Wraps the coro in an OTel span `spawn:{name}`.
- Propagates structlog ContextVars (request_id, trace_id) to the spawned coroutine.
- On exception: logs `kind='spawn.crashed'` at ERROR with traceback. Does NOT re-raise. The coro is responsible for marking its domain-row state to `failed` BEFORE raising — once `spawn()` catches, the domain row is the durable record.
- Cancellation: DB state flip + cooperative polling.
- Holds the `asyncio.Task` in a module-level set until completion so GC doesn't collect it mid-flight.

Used by: reviewer, github plugin catch-up, workspace reaper.

Not used for anything a caller will `await` — that's a normal async call.

### Long-running work is first-class domain state

No generic task layer. State of in-flight work lives in the owning domain's table (`review_jobs` carries `status`, `started_at`, `last_heartbeat_at`, `current_step`; `workspaces` carries `state`, `expires_at`). Cancellation = DB state flip + cooperative polling. Crash recovery = per-module `RouteSpec.on_startup` hook marking pre-restart `running` rows as `failed`. Periodic loops live in `lifespan`.

## DB

### Session factory

Single async SQLAlchemy session factory in `core/database`. Consumed via `async with session() as s:`. Transactions scoped to the HTTP request or the background task.

### Idempotent migrations

`core/database.migration_helpers` wraps `op.*` with idempotent variants (`create_table_if_not_exists`, `add_column_if_not_exists`, `create_index_if_not_exists`, `drop_column_if_exists`). Every migration uses these helpers; re-running a half-applied migration is always safe.

### Per-migration tracking

`schema_migrations` records every applied version. The runner is `core/database.migrate()`, not stock `alembic upgrade head`: reads applied versions, scans `alembic/versions/*.py`, applies any file whose `revision` isn't in the table. Robust to branch-switching and multiple heads.

Alembic CLI is only used for `alembic revision --autogenerate -m "..."`. Direct `alembic upgrade` is forbidden.

## Audit log discipline

Three sinks — one event may legitimately appear in all three:

| Sink | Purpose | Lifetime |
|---|---|---|
| Log (structlog → stdout) | Ephemeral signal for ops debugging. | Days; retention-truncated. |
| Trace (OTel spans) | Causal request graph. | Days; sampled. |
| Audit (`audit_log` table) | Durable record of business-meaningful state changes. | 90 days. |

Rules:
- Every log line carries trace + span IDs.
- Audit is for state changes with business meaning, not debugging. A failed DB read is a log line; a successful prompt update is an audit entry.
- Reads never write to `audit_log`.
- When in doubt, log. If "would an operator want to know this happened to entity X?" is yes, also audit.

Audit: user-initiated mutations (prompt edits, lesson CRUD, "re-review"), agent-initiated actions (review/reply posted), state transitions with business meaning (review_job queued→running→posted; ticket in_review→complete).

Don't audit: internal helpers' progress steps, reads, routine sweeps that changed nothing.

Row shape:
- `kind` follows `<entity>.<verb_past>` — lowercase, dotted, past tense.
- `actor` is the `Actor` value object. Required.
- `payload` is a Pydantic model owned by the writing module. Plain dicts rejected.
- One entry per business event — not three for "started, did it, finished".

## Org scoping

Every domain function takes `org_id` kwarg; every query filters by it. One org today; discipline makes future RBAC retrofit a check, not a refactor.

## Idempotency at external boundaries

Handlers triggered by external events MUST be idempotent under retry.

- Deduplicate by external event id. `plugins/github` inserts into `github_webhook_events` with `ON CONFLICT DO NOTHING`; skips dispatch if not inserted.
- Upserts use `ON CONFLICT`, not "check then insert".
- State-transition functions are safe to call twice. `mark_failed` on an already-failed job is a no-op.
- "Already processed" returns 2xx — tells the sender to stop retrying.

## Secrets

- Stored encrypted at rest in the owning plugin's settings table. Encryption key is `YAAOS_ENCRYPTION_KEY` (32 bytes URL-safe base64).
- Decrypted only at the call site. No "decrypted credentials" cache; no passing across module boundaries when not needed.
- Never logged, echoed in errors, or placed in audit payloads. Redact before logging if an exception message could contain a secret.

## Testing

### Categories

| Category | Where | What | External deps |
|---|---|---|---|
| Unit | `<module>/test/test_*.py` | Pure logic. Used sparingly. | None |
| Integration | `<module>/test/test_*.py` | Module's public interface end-to-end. **Primary form.** | Real Postgres (transactional rollback); `apps/fake-github`; coding-agent CLI stub. |
| E2E | `apps/e2e/` | Full stack via browser. | `docker-compose.test.yml`. |

### Integration test pattern

- Exercise public interface, not internals.
- Real Postgres. Each test runs inside a transaction rolled back at teardown. Empty DB at start.
- Inbound HTTP: `fastapi.testclient.TestClient` in-process.
- Outbound HTTP: routed to `apps/fake-github` via `GITHUB_API_BASE_URL`. Real plugin code paths run.
- Coding-agent: `YAAOS_CODING_AGENT_STUB=1` swaps in `testing/stub_coding_agent`.

### DI over `@patch`

`@patch` / `mock.patch` / `mocker.patch` banned by ruff TID251. Substitute dependencies by injection. Rare legitimate cases use a per-line `# noqa: TID251` with explanation.

### Time controls

Each wall-clock wait has an env var. Code reads from `core/config` — never hardcoded.

| Variable | Default | Description |
|---|---|---|
| `YAAOS_REVIEW_DEBOUNCE_SECONDS` | 30 | Reviewer wait before starting a job. Tests: 0. |
| `YAAOS_REAPER_INTERVAL_SECONDS` | 30 | Workspace reaper sweep interval. Tests: 1. |
| `YAAOS_HEARTBEAT_INTERVAL_SECONDS` | 10 | Review-job heartbeat interval. |
| `YAAOS_CATCHUP_DELAY_SECONDS` | 10 | Boot delay before the GitHub catch-up coro. |

### Pytest plugin entry-point

Cross-cutting fixtures (transactional DB session, fake-github base URL) live in a small in-repo pytest plugin registered via `[project.entry-points."pytest11"]` so it auto-loads.

## Observability

### Structured logging

`structlog` everywhere; JSON to stdout. A `Logger` wrapper in `core/observability` injects request/trace context via a structlog filter.

### Context-variable threading

A single `request_meta_var: ContextVar` carries `{request_id, workflow, user, ...}` through async code. Web middleware sets it per request. `spawn()` propagates the parent's context into the spawned coroutine. Log filters and span attributes read from it.

### When to add a manual span

Auto-instrumentation covers most paths (HTTP + SQLAlchemy via OTel contrib; background coroutines via `spawn`). Add manual spans only at meaningful boundaries:

- Every external call — VCS API, coding-agent CLI, webhook signature verification.
- Every plugin entry point — `VCSPlugin.post_review`, `CodingAgentPlugin.review`, `WorkspaceProvider.provision`.
- Long phases inside a background coro — review_job phase transitions each get a span so the trace shows where wall time went.

Don't wrap every domain function — noise hurts more than detail helps.

## Bootstrap composition order

`app/main.py` is load-bearing. If steps 3–4 swap with 6 you'll mount a router before its module has registered or subscribe to an event before the bus exists. Don't reorder.

1. Load environment — `app.core.config`.
2. Configure core infra — `app.core.database`, `app.core.observability`, `app.core.primitives`.
3. Initialize events bus — `app.core.events` *before any domain subscribes*.
4. Import webserver registry — `app.core.webserver` *before any module registers routes*.
5. Core modules with plugin Protocols — `app.core.audit_log`, `app.core.workspace`.
6. Domain modules in dependency order — types first (vcs, memory), then coding_agent, then leaf domain modules, then dependents.
7. Plugins — `in_process_workspace`, `claude_code`, `github`.
8. Test-mode wrapping (conditional) — when `YAAOS_CODING_AGENT_STUB=1`, import `app.testing.stub_*` and call `wrap_all_registered_*()`. When `yaaos_env == "dev"`, import `app.testing.e2e_setup` so `/api/testing/*` mounts.
9. Build the FastAPI app — `webserver.create_app()`.
