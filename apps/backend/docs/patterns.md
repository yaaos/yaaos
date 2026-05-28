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
- `core/observability.spawn()` wrapper (logs the failure; the coro is responsible for marking its row failed before raising).
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

### `core/observability.spawn()`

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

### Session management + atomicity

Transactional service functions take a required `session: AsyncSession` parameter and never commit. The caller — an *orchestrator* — opens `db_session()`, calls services, commits once at the end. This makes audit rows land atomically with the state change they describe and lets services compose inside a single transaction. Type signature is the documentation: if a function takes `session: AsyncSession`, it's a service; if it doesn't, it's an orchestrator (endpoint handler, `spawn()` task body, periodic-task entrypoint).

```python
# Service — required session, never commits.
async def create_lesson(..., *, session: AsyncSession) -> Lesson:
    row = LessonRow(...)
    session.add(row)
    await session.flush()
    await audit_for_lesson(row.id, "lesson.created", ..., session=session)
    return Lesson.from_row(row)

# Orchestrator — opens, calls, commits.
@router.post("/lessons")
async def post_lesson(...) -> Lesson:
    async with db_session() as s:
        lesson = await create_lesson(..., session=s)
        await s.commit()
    return lesson
```

Rules:

- Service modules never write `session: AsyncSession | None`, never check `if session is None`, never call `db_session()` themselves. Semgrep rule `apps/backend/.semgrep/no_optional_session.yaml` enforces this.
- Read-only services follow the same rule — required session, no commits — so callers can compose snapshot-consistent read-then-write.
- Orchestrators (endpoint handlers, `spawn()` task bodies, periodic-task entrypoints) are the only places that open `db_session()`. No `_owns_session` naming suffix needed — the type signature is the contract.
- `core/audit_log.audit()` and every `audit_for_*` helper require `session=`. The audit row flushes inside the caller's transaction so it can never diverge from the state change it describes.

### Service-fn session-handling convention

Two valid shapes for service functions:

- **Shape (a) — takes `session` first positional, never commits.** Use when real callers compose the function with sibling writes inside one `async with db_session() as s:` block (e.g. creating an org + membership + install in a single transaction). Signature: `async def create_org(session: AsyncSession, *, slug: str, ...) -> Org`.
- **Shape (b) — opens own session, returns value.** Use for single-row writes or read-only fetches that never need to compose with other writes in the same transaction. Signature: `async def get_org(org_id: UUID) -> Org | None`. `lessons.create` follows shape (b) — callers seed it standalone.

Pick shape (a) only when callers genuinely compose with sibling writes. Don't add a `session` parameter speculatively. The rule above (service modules never call `db_session()` themselves) applies only to shape (a) functions; shape (b) functions are orchestrators-in-disguise and are the exceptions that own their own session.

### e2e seed paths use public APIs

`app/testing/e2e_setup` chains real public service-layer calls — no `*Row` constructors, no cross-module model imports. Deliberate consequence: seeds emit the same audit rows and events as production writes, acting as a free smoke test for the full call path.

The only DB-wide primitive is `core.database.truncate_all_tables(session)`. Call it from within an `async with db_session() as s:` block followed by `await s.commit()`.

### Idempotent migrations

`core/database.migration_helpers` wraps `op.*` with idempotent variants (`create_table_if_not_exists`, `add_column_if_not_exists`, `create_index_if_not_exists`, `drop_column_if_exists`). Every migration uses these helpers; re-running a half-applied migration is always safe.

### Per-migration tracking

`schema_migrations` records every applied version. The runner is `core/database.migrate()`, not stock `alembic upgrade head`: reads applied versions, scans `alembic/versions/*.py`, applies any file whose `revision` isn't in the table. Robust to branch-switching and multiple heads.

Alembic CLI is only used for `alembic revision --autogenerate -m "..."`. Direct `alembic upgrade` is forbidden.

## Durable tasks via `core/tasks`

Use [`core/tasks`](core_tasks.md) when work must survive backend restarts, has retry policy, or participates in a workflow. Use [`core/observability.spawn()`](core_observability.md) for fire-and-forget request-scoped background work without durability needs.

`@task` registers a body; `enqueue(task_ref, args, *, session)` writes a `taskiq_enqueue` row to `outbox_entries` in the caller's session. The drain (in `apps/backend/app/worker.py`) pushes outbox rows to Redis after commit. The atomic-in-session contract: task is durable iff the caller's transaction commits. The outbox table is private to `core/tasks` — domain modules never import it directly.

Task bodies must be idempotent — a drain crash between dispatch and `dispatched_at` stamp can redispatch. Bodies look up state from DB (don't carry "do this once" semantics in the args).

## Secrets

Every sensitive value crosses module boundaries as Pydantic `SecretStr`: encryption keys, OAuth client secrets + access/refresh tokens, TOTP master key, session tokens, invitation tokens, SMTP password, third-party API keys (Braintrust, Anthropic via BYOK), GitHub App private keys. `SecretStr` renders as `'**********'` in `repr`, `str`, `model_dump`, and `model_dump_json` so logs / tracebacks / audit payloads never carry plaintext.

`SecretStr` applies at **every** module boundary, not just Settings:

- **Request schemas** — any Pydantic `BaseModel` field that carries a user-submitted credential (API key set endpoints, OAuth callback bodies, etc.).
- **Value objects + dataclasses** — `Tokens.access_token`, `ProviderConfig.client_secret`, any frozen-dataclass field that holds a token, key, or secret in flight.
- **Function signatures** — parameters that pass a secret between modules, including provider Protocol methods (`validate(access_token: SecretStr)`) and constructors of typed contexts.

Call `.get_secret_value()` only at the byte boundary — Fernet construction, JWT sign, HTTP `Authorization` header, subprocess argv, broker payload heading out the door, the env dict of a wire-bound exec block. Never put a raw secret into: a log call, a Pydantic `model_dump` output, an exception message, an outbox payload, an audit-log entry, or an SSE event.

When decrypting a ciphertext column for use, wrap the plaintext in `SecretStr(...)` immediately on emergence so the rest of the call chain stays uniform.

## WorkflowCommand discipline

Engine in [`core/workflow`](core_workflow.md). Workflows are typed Pydantic data structures registered at startup; commands fall into three categories with a single `execute(inputs, ctx) -> Outcome` shape:

- **Workspace** — issues one or more AgentCommands. `start_step` dispatches and parks the workflow in `awaiting_agent`; `handle_agent_event` resumes when the terminal event arrives. Worker never blocks on the agent.
- **Local** — runs in the worker process; persists outcome inline and enqueues `route_workflow` in the same transaction.
- **HITL** — returns `Outcome.hitl_pending(question=…)`; the engine writes a `pending_human_decisions` row and parks in `awaiting_human`. `resume_hitl()` is the resume API.

Commands take a typed `inputs` Pydantic model + a `CommandContext`. They never read `workflow_executions.step_state` directly — input resolution is the router's job. Outputs go on the Outcome.

### Single-flight per workspace

The workspace state machine accepts one in-flight AgentCommand at a time. [`core/workspace.try_claim`](core_workspace.md) is an atomic conditional UPDATE that succeeds iff `current_command_id IS NULL` AND `status='active'`. Concurrent dispatch attempts see `rowcount=0` and back off. Pair every claim with `release_claim(workspace_id, command_id=…)` once the terminal event has been observed.

### Failure-report-precedes-disposal invariant

`release_claim` clears `current_command_id` but **preserves** `current_holder_workflow_id`. The terminal AgentEvent must arrive before the workspace row is disposed. This means reconciliation lookups can always resolve `command_id → workspace → current_holder_workflow_id → workflow_execution` — even after the workspace is being torn down.

### Recovery policy registry

AgentCommand failure labels (e.g. `auth_expired`) map to lifecycle WorkflowCommand kinds (e.g. `RefreshWorkspaceAuth`) via `core/workspace.register_recovery_policy`. The engine consults the registry on a recoverable failure and inserts the recovery command before re-dispatching the original.

## WorkspaceProvider contract

[`core/workspace`](core_workspace.md) declares the `WorkspaceProvider` Protocol; two implementations ship in : `InMemoryWorkspaceProvider` (existing in-process plugin) and `RemoteAgentWorkspaceProvider` (dispatches via [`core/agent_gateway`](core_agent_gateway.md)). The Protocol is the single seam between control plane and provider — both implementations enforce the same invariants (single-flight, failure-report-precedes-disposal, recovery). Per-org selection lives on `orgs.workspace_provider`.

The Protocol's `run_coding_agent_cli` is synchronous-shaped — natural for the in-process provider, awkward for the remote provider. `RemoteAgentWorkspaceProvider` raises on those methods; the Workspace WorkflowCommands enqueue AgentCommands directly and the engine handles awaits through `handle_agent_event`. The Protocol shape is preserved for the in-process path's callers.

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

Every domain function takes `org_id` kwarg or reads it from the `org_id_var` contextvar; every query filters by it. Two-track rule:

- **HTTP request handlers** — `Depends(require(Action.X))` resolves `X-Org-Slug` and sets the contextvar. Handlers can read it via `current_org_id()`.
- **Background work** — every non-HTTP entry point opens `with org_context(org_id, actor_kind, actor_id=None)` from [`core/auth`](core_auth.md). This sets the same contextvars + OTel span attrs (`yaaos.org_id`, `yaaos.actor_kind`, `yaaos.actor_id`) + structlog bound vars so background log lines + audit rows attribute correctly. Wrapped today: GitHub catch-up poller, reviewer worker (`actor_kind=workspace`). Scheduler cleanup jobs that don't emit audit rows + don't read from org-scoped tables (session/invitation/totp/audit purges) do NOT need a wrap — they're global by design.
- **Discipline rule** — any function reading from an org-scoped table must either (a) take `org_id` as an explicit kwarg, or (b) call `require_org_context()` to assert the contextvar is set. The assertion surfaces forgotten-wrap bugs loudly instead of silently leaking cross-org data.

## Idempotency at external boundaries

Handlers triggered by external events MUST be idempotent under retry.

- Deduplicate by external event id. `plugins/github` inserts into `github_webhook_events` with `ON CONFLICT DO NOTHING`; skips dispatch if not inserted.
- Upserts use `ON CONFLICT`, not "check then insert".
- State-transition functions are safe to call twice. `mark_failed` on an already-failed job is a no-op.
- "Already processed" returns 2xx — tells the sender to stop retrying.

## Secrets

- Single Fernet wrapper in [`core/secrets`](core_secrets.md); master key from `YAAOS_TOTP_MASTER_KEY` (fallback `YAAOS_ENCRYPTION_KEY` in non-prod). Callers `encrypt(plaintext)` / `decrypt(ciphertext)` — never construct `Fernet` directly.
- Decrypted only at the call site. No "decrypted credentials" cache; no passing across module boundaries when not needed.
- Never logged, echoed in errors, or placed in audit payloads. Redact before logging if an exception message could contain a secret.
- Per-(org, provider) API keys go through [`core/byok`](core_byok.md); provider plugins register their `validate(key) -> bool` callable via `byok.register_validator(provider, callable)` at bootstrap so `core/byok` stays free of plugin imports.

## Bearer token discipline

Every yaaos-issued bearer follows the same shape — adopted in for sessions, in again for signed invitations, and extended in for MCP review tokens:

- **Mint** with `secrets.token_urlsafe(32)` (32 random bytes, URL-safe base64). Return the raw token to the caller exactly once.
- **Store** `sha256(raw_token)` as the primary key. Raw tokens never persist.
- **Lookup** by hashing the inbound bearer + selecting by hash + checking `expires_at > now()`. Constant-time-safe because the hash is the PK.
- **Own one table per consumer.** `sessions`, `mcp_review_tokens`, and (via sha256-on-write) `invitations.token_hash` are separate; one bearer can't be substituted for another.
- **Expire by absolute time.** Each consumer owns its TTL — sessions 14d, MCP review tokens 2h, invitations 7d. The periodic cleanup task in `domain/identity/scheduler` (or a domain-local equivalent) deletes expired rows; production code also checks `expires_at` on every read.

## Route security declarations

Every `/api/*` path classifies as one of three `RouteSecurity` categories: `PUBLIC` (no auth), `USER_SCOPED` (session, no org), or `ORG_SCOPED` (session + `X-Org-Slug` + role check). The classifier `classify_route(path, method)` and the prefix/exact lists live in `app/core/auth/types.py`; the middleware enforces `X-Org-Slug` and CSRF based on the category. Route dependencies: `Depends(require(Action.X))` for `ORG_SCOPED`, `Depends(require_session)` (or `Depends(public_route)`) for `USER_SCOPED` handlers that read the session cookie, `Depends(public_route)` for `PUBLIC`. The post-response middleware guard returns 500 if a 2xx response left `route_security_resolved` unset. Action → minimum-role map lives in `app/domain/sessions/dependencies.py:_REQUIRED_ROLE`; adding a new action is a code change, not config. Adding a new URL prefix requires placing it in exactly one of the three category sets in `app/core/auth/types.py`.

## Testing

### Categories

| Category | Where | What | External deps |
|---|---|---|---|
| Unit | `<module>/test/test_*.py` | Pure logic, one function/class. Used sparingly. | None |
| Integration | `<module>/test/test_*.py` | Module's public interface end-to-end. **Primary form.** | Real Postgres (transactional rollback); `apps/fake-github`; coding-agent CLI stub. |
| Service | `<module>/test/test_*_service.py` | Cross-module flow (3+ modules) driven from an entry point, in-process. | Real Postgres; stub plugins. |
| E2E | `apps/e2e/` | Browser-visible behavior — SSE updates, cookies, OAuth redirects, route navigation. | `docker-compose.test.yml`. |

### Service tests

When a backend flow crosses **3+ modules** (e.g. webhook → intake → reviewer → vcs.post_review → audit), write ONE service test that drives the entry-point function or HTTP route end-to-end and asserts the durable state across every module it touches. Service tests are the **default** for backend-only flows; reach for Playwright only when the contract is browser-visible.

Mechanics:

- **Real Postgres via `db_session`.** Transactional rollback per test — production code's `session()` hits the override; inner `commit()` calls become SAVEPOINT releases; outer transaction rolls back on teardown. Empty DB at start of each test.
- **Stub plugins from `app/testing/`.** `YAAOS_CODING_AGENT_STUB=1` (set by `conftest.py`) wraps registered coding-agent plugins with `StubCodingAgentPlugin` that returns a canned `ReviewResult`. `app.testing.stub_workspace.wrap_all_registered_workspace_providers()` swaps the workspace providers for flows that provision a workspace.
- **HTTP routes via `httpx.ASGITransport`.** Drive endpoints in-process without a network listener. The pattern is already used by `app/domain/integrations/test/test_endpoints.py`, `app/domain/mcp_proxy/test/test_dispatch.py`, etc.
- **Seed helpers from `app/testing/e2e_setup/`.** `seed_github_install`, `seed_lesson`, etc. are HTTP shims around the same domain calls a Playwright spec would hit — reuse them from pytest.

Naming: `test_<flow>_service.py` in the owning module's `test/` directory. Owner is whichever module holds the entry-point function (the one you `await` first in the test body).

Marker: every service test is decorated `@pytest.mark.service`. Run only the service tier with `pytest -m service`; run the fast unit-only loop with `pytest -m "not service"`. The default `bin/ci` invocation runs both — the marker is for developer ergonomics, not a CI skip.

Assert on the **durable state production reads** — audit rows by kind, posted-comment count via the stub vcs plugin, finding state in the aggregate, `last_refresh_status`, the test inbox (`get_test_inbox()`), event-bus publications. Don't assert on intermediate log lines unless the log is the contract.

### Integration test pattern

- Exercise public interface, not internals.
- Real Postgres. Each test runs inside a transaction rolled back at teardown. Empty DB at start.
- Inbound HTTP: `fastapi.testclient.TestClient` or `httpx.ASGITransport` in-process.
- Outbound HTTP: routed to `apps/fake-github` via `GITHUB_API_BASE_URL`. Real plugin code paths run.
- Coding-agent: `YAAOS_CODING_AGENT_STUB=1` swaps in `testing/stub_coding_agent`.

### Module boundaries in tests

Tests obey the **same import rules as production code** — enforced by `tach check --interfaces` in CI, which covers `app/testing/` as well as production code. Violations fail CI.

- Import only `__all__` exports — `from app.<module> import X`, never `from app.<module>.<submodule> import X` across module boundaries. Within a module's own test directory, direct submodule imports are allowed.
- No `*Row` types in cross-module imports. If a test in module B needs to inspect persisted state owned by module A, use module A's targeted public read function (e.g. `get_token_by_hash`, `get_session_by_hash`) or assert on the observable outcome instead.
- No test-only seams that bypass module interfaces. If a seam is needed, it belongs in `app/testing/` — but `app/testing/` is itself tach-governed; it may only import from `__all__`-gated module paths.
- Service tests of multi-hop pipelines are sliced per-hop: each service test exercises one entry point end-to-end; chain tests by asserting on the durable state that the next hop reads, not by calling internal functions of the next module.
- Singleton reset for test isolation: never poke private state via a submodule attribute (`mod._svc._singleton = None`). Use a named helper instead. Two flavors by reach:
  - **Cross-module reach** (module A's tests reset module B's state) → public symbol in B's `__all__` and tach interface. Example: `sse_pubsub.reset_pubsub()` — called from reviewer / orgs / agent_gateway tests.
  - **Intra-module reach only** (module's own `test/` directory) → private `_*_for_tests` helper in B's `service.py` (or sibling submodule), NOT in `__all__`, NOT in tach `expose`. Tests reach it via direct submodule import — intra-module, tach-permitted. Examples: `redis._reset_clients_for_tests`, `agent_gateway.subscribers._reset_subscriber_singleton_for_tests`, `orgs.onboarding._reset_contributors_for_tests`.

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

## `scoped_*` context managers for import-time registries

Modules with import-time registries expose `register_*`, `unregister_*`, and `scoped_*` in `__all__`. Tests use `with scoped_*(...)` for temporary registrations — cleanup is automatic on block exit, even on exception.

Modules with this pattern today: `core.workflow` (`scoped_engine`, `scoped_workflow`), `domain.vcs` (`scoped_vcs_plugin`), `domain.coding_agent` (`scoped_coding_agent`), `core.tasks` (`scoped_task_registration`).

`core.workflow.scoped_engine()` is the standard test-isolation helper for tests that register workflows or commands. It saves the current engine, creates a fresh one, yields it, then restores the prior one on exit — replacing the former `svc._engine = None; eng = get_engine(); svc._engine = eng; ... svc._engine = None` pattern.

Rules:
- No wholesale-wipe between tests. Test exactly what you need, clean it up with the scoped helper.
- `unregister_*` is a no-op if the id is absent — safe to call in finally blocks.
- `scoped_*` registers on entry, unregisters on exit. The yielded value is the same object passed in.
- `scoped_task_registration(task_ref)` is the tasks variant: call `@task(name)(fn)` to get a `TaskRef`, then wrap the test body in `with scoped_task_registration(ref)`. On exit the name is popped from the broker registry so subsequent tests can reuse the same name.

## Subscription self-cleanup (async generator pattern)

`core.events.subscribe()` is the canonical example of an async generator whose `finally` clause does its own cleanup. The generator registers a subscriber queue on entry; `finally` pops it on any consumer exit — normal return, `break`, exception, or `aclose()`. Callers never call an explicit `unsubscribe()`.

Preferred test shapes for consuming one event then exiting:
- `async for ev in subscribe(filter): ...; return` — `return` exits the coroutine; the event loop's async-gen finalizer schedules `aclose()`. Yield one event-loop tick (`await asyncio.sleep(0)`) after the consumer finishes if the test asserts `subscriber_count() == 0`.
- `async with aclosing(subscribe(filter)) as gen: ev = await gen.__anext__()` — `aclosing.__aexit__` awaits `gen.aclose()` synchronously, so cleanup is guaranteed before the `async with` block exits. Preferred when early exit is needed and the test asserts cleanup immediately.

Use this pattern over a `register/unregister` pair whenever the consumer naturally iterates — the single-seam generator is simpler and harder to misuse.

## Module lifecycle — `shutdown()` convention

Every runtime-state module exposes a public `async def shutdown()` in `__all__`. Naming is uniform; internals may delegate to library-conventional names (`aclose` for Redis, `dispose` for SQLAlchemy, taskiq broker close). Modules self-register at import time (after `__all__` is defined) with the relevant process registry by calling `register_web_shutdown_hook(shutdown)` and/or `register_worker_shutdown_hook(shutdown)` from `app.core.shutdown_registry`.

Categorization rule:
- Web-presence only (SSE, WebSocket) → register with web registry.
- Worker-presence only → register with worker registry.
- Shared infra (redis, database, events, tasks) → register with both.

The registries live in `app.core.shutdown_registry` (a zero-dependency standalone module) to avoid circular imports between modules that import each other.

## Two process lifecycles, two registries

Web and worker are separate OS processes with separate shutdown cadences. `app.core.shutdown_registry` owns both:

- `register_web_shutdown_hook` / `iter_web_shutdown_hooks` — used by the web process.
- `register_worker_shutdown_hook` / `iter_worker_shutdown_hooks` — used by the worker process.

Both registries are re-exported from `core.webserver` and `core.tasks` for convenience; the canonical source is `app.core.shutdown_registry`.

FastAPI lifespan teardown (in `core/webserver/app_factory.py`) iterates `iter_web_shutdown_hooks()` in reverse order. Worker runtime teardown (in `core/tasks/runtime.py`) iterates `iter_worker_shutdown_hooks()` in reverse order. Reverse order means the most-recently-registered (most-dependent) modules shut down first.

`app/web.py` and `app/worker.py` pin the foundational shutdown order by explicitly importing `app.core.database` and `app.core.redis` near the top of step 2, before any module that depends on them. That guarantees those two register their hooks first and therefore shut down last — anything imported transitively later (tasks, sse_pubsub, agent_gateway) shuts down before them. Don't rely on transitive imports for hook ordering; pin the ones that matter.

Both loops wrap each hook call in `try/except` (web) or `contextlib.suppress` (worker) so one failing hook does not abort the sequence.

## Composition roots — `app/web.py` and `app/worker.py`

Both composition roots live inside `app/` so they're importable as regular Python modules and testable without exec tricks.

- `app/web.py` — web process entry. Same bootstrap import order as before (see § Bootstrap composition order). Ends with `app = webserver.create_app()`. When run directly (`python apps/backend/app/web.py`) the `if __name__ == "__main__"` block calls `uvicorn.run(...)` with all server flags in Python — no flags scattered across Dockerfile CMDs.
- `app/worker.py` — worker process entry. Side-effect imports (workflow commands, plugins, workspace providers) + `asyncio.run(core.tasks.runtime.run())`. When run directly the `if __name__ == "__main__"` block is the sole entry point.

Dockerfile CMDs are exec-form `["python", "apps/backend/app/web.py"]` / `["python", "apps/backend/app/worker.py"]`. tini is PID 1 (image-level `ENTRYPOINT ["/usr/bin/tini", "--"]`) and forwards SIGTERM to the Python child, triggering graceful shutdown via the Phase-1 shutdown registries.

`bin/worker` is gone — that path now lives at `app/worker.py`.

## Bootstrap composition order

`app/web.py` is load-bearing. If steps 3–4 swap with 6 you'll mount a router before its module has registered or subscribe to an event before the bus exists. Don't reorder.

1. Load environment — `app.core.config`.
2. Configure core infra — `app.core.database`, `app.core.observability`.
3. Initialize events bus — `app.core.events` *before any domain subscribes*.
4. Import webserver registry — `app.core.webserver` *before any module registers routes*.
5. Core modules with plugin Protocols — `app.core.audit_log`, `app.core.workspace`.
6. Domain modules in dependency order — types first (vcs, lessons), then coding_agent, then leaf domain modules, then dependents.
7. Plugins — `in_memory_workspace`, `claude_code`, `github`.
8. Test-mode wrapping (conditional) — when `YAAOS_CODING_AGENT_STUB=1`, import `app.testing.stub_*` and call `wrap_all_registered_*()`. When `yaaos_env == "dev"`, import `app.testing.e2e_setup` so `/api/testing/*` mounts.
9. Build the FastAPI app — `webserver.create_app()`.

Each module imported in steps 2–7 appends its `shutdown()` hook to the relevant process registry as a side effect of import. By step 9, all hooks are registered before `create_app()` wires them into the lifespan.
