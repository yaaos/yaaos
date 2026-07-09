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
- Coding-agent CLI stdout parsed by `plugin.parse_result(terminal_event_payload)` into `RunResult`; the run-sink writes this to `coding_agent_runs`. `RunResult.output` is the structured response JSON from the stream-json `result` field; the engine calls `CodingAgentCommand.handle_response(output, ctx)` on `completed_success` to validate it against `ExpectedResponse` and produce a typed `Outcome`.
- Audit payloads — every `kind` has a corresponding Pydantic class.
- Internal cross-module calls: plain types/dataclasses fine where they fit.

### Exceptions

Don't catch where raised. Let them propagate. Catch only at top-level boundaries:
- HTTP middleware (converts to 500 JSON).
- `core/observability.spawn()` wrapper (records exception on the span + logs the failure; the coro is responsible for marking its row failed before raising).
- Thin retry wrapper around vendor SDK calls.
- Tests.

Domain functions succeed or raise. No translation unless translation is genuinely the function's job.

### Filesystem + processes via `core/workspace`

Never touch the filesystem (`open()`, `pathlib`) or spawn processes (`subprocess`) directly for repo/code work. Workspace operations go through the remote agent via `core/coding_agent.dispatch_invocation` (which enqueues via `core/agent_gateway`). Consumers never see internal paths; the Protocol exposes operations, not paths.

Exceptions: `core/database` (Postgres connections), `core/observability` (log files).

### Imports

- Absolute imports only.
- Module-level only (heavy-ML exception requires `# noqa: PLC0415`).
- Other modules import only `__all__` exports. Internal cross-module imports are Tach-rejected.
- **No `*Row` types in `__all__`.** SQLAlchemy Row/mapped classes never appear in any module's `__all__` or `tach expose` list. Every public API that surfaces persisted state returns the module's Pydantic value object, not the Row. Foreign table access via an imported Row name fails tach `check --interfaces` — the intended path is the owning module's public service API.
- **No circular module dependencies.** `forbid_circular_dependencies = true` is emitted by `bin/sync_modules` into `tach.toml`; tach rejects any new cycle under `tach check --interfaces` (the CI command). Canary `test_injected_cycle_is_rejected` in `apps/backend/bin/test_module_boundaries.py` verifies the guard fires.
- **Layer ordering: `core < domain < plugins < testing`.** Enforced by `check_layering()` in `bin/sync_modules` — tach's `--interfaces` mode silently ignores tach-native `layers` config, so the Python check is the sole enforcer. The allowlist `PERMITTED_CROSS_LAYER_EDGES` is empty (`frozenset()`); no permitted cross-layer edges exist. Canary `test_injected_core_to_domain_is_rejected` in `apps/backend/bin/test_module_boundaries.py` verifies the guard fires.
- **`bin/sync_modules` enforces a full ladder of AST-level rules at every CI run.** Every rule is import-free, env-free, `# noqa`-immune. Each has at least one canary in `apps/backend/bin/test_module_boundaries.py` (typically an injection test + a "clean-tree-stays-clean" test).
  - **Rule-1** — a name in `__all__` that resolves to a SQLAlchemy mapped/Row class (inherits *directly* from `Base` — the project's single declarative base in `app/core/database/service.py` — or any name imported `from <any>.models`) is rejected. The base-class match is exact, so Pydantic `BaseModel` / `BaseSettings` subclasses are not false-flagged. Canary: `test_row_readded_to_all_is_rejected`.
  - **Rule-5** — a function listed in `__all__` whose return annotation or parameter annotations reference a Row type is rejected. The check follows `from app.<m> import X` re-exports back to the definition site (depth-bounded at 8 hops, cycle-safe) before inspecting annotations, so a Row-returning helper defined in a sibling submodule and re-exported from `__init__.py` is caught at the package boundary. Per-file Row-name resolution closes transitively over: module-level type aliases (`X = Y`, `X: TypeAlias = Y`, PEP 695 `type X = Y`, including subscripted RHS like `list[Y]`); ClassDef inheritance chains (`class Sub(SomeRow)` joins the set); aliased Row imports (`from .models import UserRow as UserResult`); and cross-file Row imports (`from app.<other> import X` where X resolves to a Row in `<other>`). Renaming a Row at the boundary cannot launder past the gate. Canaries: `test_row_in_public_signature_is_rejected` (inline) + the `test_rule5_*` family covering 1-hop, 2-hop, aliased re-export, local/PEP-695/TypeAlias/chained/subscripted aliases, subclass-of-Row, aliased Row import, parameter annotations, clean re-export, and cycle safety.
  - **Test-seam export rule** — a `core`/`domain`/`plugins` `__all__` symbol is rejected iff its name matches a test-seam pattern (`reset_*`, `clear_*`, `scoped_*`, `*_for_tests`, `_seed_*`, `set_*_override`, `set_test_*`, `get_test_*`) AND it has zero production importers (no importer outside `*/test/`, `app/testing/`, and the top-level `conftest.py`; `app/web.py` and `app/worker.py` always count as production). The intersection is precise: name-only would false-positive on real production APIs (`clear_cookie_attrs`); usage-only would false-positive on public types tests construct. Sanctioned exception: `set_*_for_tests` names (`_SANCTIONED_TEST_BINDER_PATTERN`) are exempt from the production-importer check — a registry module may export `set_X_for_tests` without any production caller because its only callers are autouse isolation fixtures in `app/testing/`. Canary: `test_test_helper_export_is_rejected` + `test_clean_tree_has_no_test_helper_exports` + `test_set_for_tests_export_is_allowed_without_production_importer` + `test_other_seam_names_still_require_production_importer`.
  - **Rule-6** — cross-module submodule imports are rejected. A file in module A may import only `app.B` (the module's package root) from module B — not `app.B.<sub>`. Three shapes: `import app.B.sub`, `from app.B.sub import X`, and `from app.B import sub` when `sub` is a submodule namespace (disambiguated by the AST classifier — see Rule-9). Composition roots (`app/web.py`, `app/worker.py`) are exempt; they side-effect-import submodules to wire bootstrap. Canary: `test_injected_submodule_import_is_rejected` + `test_injected_submodule_from_import_is_rejected` + `test_case_collision_actor_is_not_flagged`.
  - **Rule-7** — private-attribute reach on cross-module receivers is rejected. Taint-based AST visitor covers four receiver shapes: bare alias (`alias._private`), call return (`cross_mod_call()._private`), walrus (`(eng := cross_mod_call())._private`), and subscript (`cross_mod_call()["x"]._private`). Dunders are exempt (Python protocol, not module-private state). Canary: `test_injected_private_attr_via_alias_is_rejected` + `test_injected_private_attr_via_return_taint_is_rejected` + `test_injected_walrus_private_reach_is_rejected` + `test_injected_subscript_private_reach_is_rejected`.
  - **Rule-9** — submodule namespace handles in `__all__` are rejected. The AST classifier reads `__init__.py` and tags each entry as `symbol_reexport` (OK — `from app.X.sub import name`), `inline_def` (OK — `def`/`class`/value in the file), or `namespace_handle` (REJECT — `from app.X import sub` where `sub` is a sibling submodule). Re-exported functions/classes whose name matches a sibling file (`Actor`, `spawn`, `set_if_absent`) are correctly classified as `symbol_reexport` and pass. Canary: `test_injected_namespace_handle_in_all_is_rejected`.
  - **Rule-10** — `ContextVar(...)` bindings in `__all__` are rejected. A ContextVar IS the storage of the owned singleton — exporting it defeats the Cardinal rule (§ Module structure). Canary: `test_injected_contextvar_in_all_is_rejected` + `test_clean_tree_has_no_contextvar_in_all`.
  - **Rule-12** — module-level instance-literal bindings in `__all__` are rejected (`engine = _Registry()` exported). Data-type instance literals (Pydantic `BaseModel`, `Enum`, `Workflow`, `dataclass`, `TypedDict`) are exempt — they are vocabulary, not state. Canary: `test_injected_instance_literal_in_all_is_rejected` + `test_data_type_literal_in_all_is_allowed` + `test_clean_tree_has_no_instance_literal_in_all`.
  - **Rule-15** — factory functions in `__all__` whose body is just `return <module-singleton>` or `return <ContextVar>.get()` are rejected. Catches `def get_pubsub(): return _pubsub_var.get()` — the "export the live singleton" Cardinal-rule violation. Canary: `test_injected_factory_returns_singleton_is_rejected` + `test_clean_tree_has_no_factory_returns_singleton`.
  - **Rule-16** — mutable container literals in `__all__` are rejected (`REGISTRY: dict = {}`, `CACHE = []`, `KEYS = set()`). Shared mutable state across module boundaries is the same Cardinal-rule violation as an instance export. Tuples, frozensets, `Final[<frozen>]` exempt. Canary: `test_injected_mutable_container_in_all_is_rejected` + `test_clean_tree_has_no_mutable_container_in_all`.
  - **Rule-17** — `bind_*` entries in `__all__` are rejected. Production composition roots use the eager-default ContextVar pattern (or lazy `_get()` for settings-dependent singletons); the only legitimate binding swap is the test-only `set_*_for_tests` context manager. `register_*` is NOT covered — it's the plugin Protocol entry-point pattern. Canary: `test_injected_bind_in_all_is_rejected`.
  - **Rule-18** — `def` and `class` declarations in `__init__.py` are rejected. All implementation lives in named submodules. Allowed in `__init__.py`: imports, `__all__`, docstring, `if TYPE_CHECKING:` blocks, bare side-effect calls (`register_routes(...)`), simple variable bindings (data-type instance literals). Canary: `test_injected_def_in_init_is_rejected`.
  - **Rule-19** — dynamically-computed `__all__` is rejected. Must be a literal `list` or `tuple` of string constants — concatenation, comprehensions, function calls all fail. The static gate can't validate a runtime-computed `__all__`. Canary: `test_injected_dynamic_all_is_rejected` + `test_clean_tree_has_no_dynamic_all`.
  - **Private name in `__all__`** — underscore-prefixed entries (excluding dunders) are rejected. The public surface is public by name; `_private` in `__all__` is a contradiction. Canary: `test_injected_private_name_in_all_is_rejected`.
  - **`__getattr__` in `__init__.py`** — PEP 562 module-level `__getattr__` lets a module return attributes that aren't in `__all__`. Banned outright. Canary: `test_injected_dunder_getattr_in_init_is_rejected` + `test_clean_tree_has_no_dunder_getattr_in_init`.
  - **Anchor imports** — 1- and 2-segment `app.*` imports (`import app`, `from app import core`, `from app.core import identity`) are rejected. Composition roots exempt. They bypass `_resolve_module_target` (which requires 3 segments) and tach's interface check. Canary: `test_injected_anchor_import_is_rejected` + `test_injected_bare_app_import_is_rejected`.
  - **Relative imports** — `from .foo` / `from ..bar` anywhere under `app/` is rejected. Absolute imports are mandatory per § Imports above. Canary: `test_injected_relative_import_is_rejected` + `test_clean_tree_has_no_relative_imports`.
  - **Dynamic imports** — every literal call to `importlib.import_module`, `__import__`, `exec`, `eval` is rejected anywhere under `app/`. Any of these can construct an import string at runtime, bypassing every static check. No exemptions; replace the call site with a static `import`. Canary: `test_injected_importlib_call_is_rejected` + `test_injected_dunder_import_call_is_rejected`.
  - **Star imports** — `from X import *` is rejected anywhere under `app/`. Star imports defeat `__all__`-based static analysis (the target's `__all__` resolves at runtime). Replace with explicit re-exports. Canary: `test_injected_star_import_is_rejected` + `test_clean_tree_has_no_star_imports`.
  - **`no_dynamic_attr_access` semgrep rule** — `getattr(obj, non_literal)` and `setattr(obj, non_literal, val)` with a non-literal name are rejected. Dynamic attribute access bypasses type checking and module-boundary rules; use explicit typed keyword arguments or a typed model. Exception: `core/observability/service.py` (path-excluded) uses `setattr` for stdlib `logging.LogRecord` fields — a framework-required pattern.
  - **`no_bind_call_outside_composition_root` semgrep rule** — `bind_*($INSTANCE)` calls may appear only in `web.py` and `worker.py` (path-excluded). Calling a registry binder outside the composition roots re-binds a singleton in a non-startup context — a Cardinal-rule violation. `app/testing/` is excluded via the `--exclude` CLI flag in `bin/ci`.
  - **`no_test_only_imports_outside_tests` semgrep rule** — importing a test-seam symbol (`set_*_for_tests`, `_reset_*`, `_*_for_tests`) in production code is rejected. Test scaffolding must not appear in the production or worker modules. `app/testing/` and `test_*.py` are exempt via the `--exclude` CLI flag in `bin/ci`; `**/__init__.py` (re-export barrels — separately gated by `bin/sync_modules` Rule-17) is exempt via the rule's own `paths.exclude`.
  - **Semgrep invocation split** — `bin/ci` runs the third-party rule packs (`p/python`, `p/owasp-top-ten`) and the project's own `.semgrep/` rules in two separate invocations. Both invocations pass the same `--exclude app/testing --exclude test_*.py` CLI flags. The split exists so a future project rule can opt in to scanning `app/testing/` independently of the third-party packs (which always false-positive on test fixtures); today no project rule needs that, so both invocations carry identical excludes. Per-rule `paths.exclude` for the same paths is unreliable — semgrep strips the target arg (`app`) from the path it matches against, so a glob like `**/app/testing/**` never fires.
  - **Runtime layer** — the static checks above cover what source code says. `apps/backend/bin/import_audit.py` is a meta-path finder installed by `apps/backend/conftest.py` that catches dynamic-Python bypasses at runtime: `importlib.import_module(constructed_string)`, `getattr`-triggered lazy submodule loads, plugin registries built from strings. Recorded violations are dumped to `tmp/import_audit_violations.json` and fail `bin/ci` via the sentinel. Two carve-ins: composition roots (D1 — `app/web.py`, `app/worker.py`, and `alembic/env.py`, whose model sweep imports every `models.py` by design) and targets whose dotted path contains the segment `"test"` (D2 — pytest discovery + fixture lookup of test files are structurally within-module).
- **`bin/check_table_access` enforces two additional rules that tach cannot see:**
  - **Raw-SQL ownership** — AST-parses every `app/**/models.py` to build `table_name → owning_module`, then scans every production `.py` under `app/` (excluding `test/` dirs and `app/testing/`) for `text(...)` / `sa_text(...)` calls. Any call that references a table owned by a different module fails. Non-literal args (f-strings, variables) also fail — all auditable raw SQL must be a string literal.
  - **Suppression guard** — fails on any `# tach-ignore` directive in any `.py` under `app/` (prod + tests). One suppression reopens the import hole the tach interface check depends on.
  - Only `app/core/database/**` is allowlisted (owns `Base`, runs migrations, advisory locks, schema introspection). No other module may use raw SQL against a foreign table.
  - `apps/backend/bin/test_check_table_access.py` carries four canary tests asserting non-zero exit for each violation kind.
- **`# tach-ignore` directives are banned everywhere in `app/`.** The suppression guard in `bin/check_table_access` enforces this at every CI run.
- Tests obey the same import rules. A test needing another module's persisted state drives the same service API real callers use, or constructs a VO directly (in-memory). No `*Row` constructor across module boundaries.

## Module structure

### Conventional files

Each subdirectory of a layer is a module. Standard files:

- `__init__.py` — public interface: re-exports + `__all__` + registration side effects.
- `module.py` — exports `get_module_name()` for registrations and audit kinds.
- `service.py` — business-logic functions (split as the module grows).
- `models.py` — SQLAlchemy + Pydantic types owned by the module.
- `web.py` — FastAPI router + handlers (only if the module exposes HTTP routes).
- `test/` — tests live inside the module.

### `__init__.py` rules

- `__all__` is always present. `bin/sync_modules` derives the tach interface from it.
- All implementation lives in named submodules. No business logic in `__init__.py`.
- Order: re-exports, then `__all__`, then registration calls.
- No lazy/conditional imports (rare heavy-ML case: `# noqa: PLC0415`).
- No self-imports — internal files use direct submodule paths, not `from app.domain.foo import bar` within the same module.

### `web.py` routing convention

- Router carries no prefix. `RouteSpec.url_prefix` (defaulting to `/api/{module_name}`) is applied by `core/webserver`.
- Call `register_routes(RouteSpec(...))` at the bottom; one prefix per module, enforced at boot.
- See [core_webserver.md](core_webserver.md) for the full `RouteSpec` registry contract.

### Side-effect-only `web` / `*_web` submodules

- `web.py`, `user_web.py`, `org_settings_web.py`, `sso_web.py`, `audit_web.py`, `vcs_web.py`, `api_keys_routes.py`, `installs_web.py` are **side-effect-only** route-registration modules — never imported for their symbols.
- They are NOT exported in their owning module's `__all__`. Adding them violates Rule-9 (submodule namespace handle in `__all__`).
- Route registration fires via a bare body-level import inside the owning `__init__.py`: `import app.<layer>.<module>.<web_file>  # noqa: F401`. The `noqa: F401` tags the import as intentional-side-effect; the `__init__.py` body's import order keeps composition deterministic.
- Cross-module callers (tests + production) that need a `*_web` module's routes loaded use the same shape — `import app.<layer>.<module>  # noqa: F401` (the package's `__init__.py` side-effect already triggers every `*_web` registration). Never `from app.<layer>.<module> import <web_file>` — that's the Rule-6 violation the C9 sweep retired.

### `bin/sync_modules` workflow

Runs the full module-sync sequence. All checks run in one pass and accumulate violations — every rule that fires is reported in the same exit-2 output, so a contributor sees the full surface in one run.

1. Discover modules under each layer.
2. Write `tach.toml` — `[[modules]]` entries + `[[interfaces]]` blocks (expose lists from `__all__`).
3. Run all rule checks (full list under § Imports above): `__init__.py` syntax errors, layering, Rule-1/5/6/7/9/10/12/15/16/17/18/19, test-seam exports, private name in `__all__`, `__getattr__` in `__init__.py`, anchor imports, relative imports, dynamic imports, star imports.
4. If any rule fires, print one section per rule with its violation count + per-violation details, then a `<total> total violations across all rules.` footer; exit 2.
5. If clean, run `tach check --interfaces` and inherit its exit code.

Fail-loud behaviors: `parse_module_interface` prints a diagnostic to stderr and returns a sentinel on `SyntaxError` (so a broken `__init__.py` is surfaced as a gate error, not silently skipped); `run_tach_check` returns exit 2 and prints an install hint when `uv` is not on PATH (previously returned 0 and skipped the tach check silently). Canaries: `test_parse_module_interface_fails_loud_on_syntax_error` + `test_run_tach_check_fails_when_uv_missing`.

Never hand-edit `tach.toml`. Re-run `bin/sync_modules` after adding or changing a module interface.

### Cardinal rule — NEVER EXPORT THE INSTANCE

A module **MUST NOT** export a class instance, a `ContextVar`, an accessor function that returns the live singleton (`get_X()`, `current_X()`, `default_X()`), or any other handle to internal state. The public interface is **behavior-only**: free functions that act on the module's internal state. Callers operate through the module's functions; they never hold a reference to the thing the module owns.

Mechanically enforced by Rules 10 (no ContextVar in `__all__`), 12 (no instance literal in `__all__`), 15 (no factory-returns-singleton in `__all__`), 16 (no mutable container in `__all__`), and 17 (no `bind_*` in `__all__`).

The one explicit carve-out is `set_X_for_tests` — a context manager exported in `__all__` that yields the bound instance for the test's use (assert state, drive behavior — both valid). The carve-out is permitted because the yielded instance is scoped to the `with` block, auto-restored on exit, and reachable only from test scope (seam name matches the test-seam glob).

Registry-shape pattern for any singleton-owning module:

- Module-private `_X_var: ContextVar[X] = ContextVar("_X_var", default=X())` (eager default; lazy `_get()` for settings-dependent singletons like the DB engine + taskiq broker).
- Module-private `_get() -> X` accessor.
- Module-level free functions delegating to `_get()` — these are the public surface.
- One `set_X_for_tests(*, scenario: Literal[...] = "default")` context manager in `__all__`.

Production composition roots do nothing — the registry is usable the moment the module is imported. Autouse fixtures in `app/testing/isolation.py` are one `with set_X_for_tests(): yield` block per registry.

### Adding a new module

1. Create the directory under the appropriate layer.
2. Add `__init__.py` (re-exports + `__all__`) and `module.py` (`get_module_name`).
3. If exposing HTTP routes: add `web.py`, call `register_routes` at bottom, ensure `__init__.py` imports `web` so the side effect runs.
4. For a new plugin: ensure `app/web.py` imports the plugin package.
5. Run `bin/sync_modules`.
6. Add `apps/backend/docs/<layer>_<module>.md` following the per-module template.

## Background work

### `core/observability.spawn()`

Every fire-and-forget background coroutine goes through this single helper. Behaviour:
- Wraps the coro in an OTel span `spawn:{name}`.
- On exception: calls `span.record_exception(exc)` + `span.set_status(ERROR)`, then logs `spawn.crashed` at ERROR with traceback. Does NOT re-raise. The coro is responsible for marking its domain-row state to `failed` BEFORE raising — once `spawn()` catches, the domain row is the durable record.
- Holds the `asyncio.Task` in a module-level set until completion so GC doesn't collect it mid-flight.

Used by: `core/sse`'s after-commit general-event publish.

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
- Orchestrators (endpoint handlers, `spawn()` task bodies, periodic-task entrypoints, and the run engine's per-stage dispatch taskiq bodies) are the only places that open `db_session()`. No `_owns_session` naming suffix needed — the type signature is the contract. The run engine is a unique orchestrator: e.g. `domain/pipelines.engine._run_action_stage` opens one session, wraps the `Action.execute` call inside a SAVEPOINT, and passes the outer session so the action's writes + stage-execution state + outbox enqueue commit atomically.
- `core/audit_log.audit()` and every `audit_for_*` helper require `session=`. The audit row flushes inside the caller's transaction so it can never diverge from the state change it describes.

### Service-fn session-handling convention

Two valid shapes for service functions:

- **Shape (a) — takes `session` first positional, never commits.** Use when real callers compose the function with sibling writes inside one `async with db_session() as s:` block (e.g. creating an org + membership + install in a single transaction). Signature: `async def create_org(session: AsyncSession, *, slug: str, ...) -> Org`.
- **Shape (b) — opens own session, returns value.** Use for single-row writes or read-only fetches that never need to compose with other writes in the same transaction. Signature: `async def get_org(org_id: UUID) -> Org | None`. `lessons.create` follows shape (b) — callers seed it standalone.

Pick shape (a) only when callers genuinely compose with sibling writes. Don't add a `session` parameter speculatively. The rule above (service modules never call `db_session()` themselves) applies only to shape (a) functions; shape (b) functions are orchestrators-in-disguise and are the exceptions that own their own session.

### e2e seed paths use public APIs

`app/testing/e2e_setup` chains real public service-layer calls — no `*Row` constructors, no cross-module model imports. Deliberate consequence: seeds emit the same audit rows and events as production writes, acting as a free smoke test for the full call path.

The only DB-wide primitive is `core.database.truncate_all_tables(session)`. Call it from within an `async with db_session() as s:` block followed by `await s.commit()`.

### `/api/testing/*` shim pattern

`app/testing/e2e_setup` exposes thin HTTP shims under `/api/testing/*` (mounted only on non-prod; see bootstrap steps 9–10 in § Bootstrap composition order). Each endpoint is a one-liner that calls a corresponding `service.py` function and returns a JSON dict with the seeded object's identity fields.

- `POST /api/testing/seed-agent` → `seed_agent(org_id=...)`: inserts a `workspace_agents` row via `agent_gateway.ensure_agent_row`. Returns `{"id": ..., "instance_id": ..., "org_id": ...}`.
- `POST /api/testing/seed-workspace` → `seed_workspace(...)`: inserts a `workspaces` row via raw SQL. Returns `{"workspace_id": ...}`.
- `DELETE /api/testing/user/{user_id}/artifacts` → `delete_user(user_id)`: deletes the user row and cascades to child rows owned by `core/identity`.
- Other seed helpers (`seed_github_install`, `seed_lesson`, etc.) follow the same shape.

All `service.py` functions open their own session and commit — each seed call is an independent committed write. Playwright specs that need isolation call the `DELETE /api/testing/reset` endpoint (via `truncate_all_tables`) at the start of each spec. Service tests drive `service.py` directly (not via HTTP) and use the `db_session` fixture for transactional rollback.

### Idempotent migrations

Alembic tracks applied revisions in `alembic_version`; a revision already at head is a no-op. Revisions that create tables or add columns should use `IF NOT EXISTS` variants (`op.execute(text("CREATE TABLE IF NOT EXISTS ..."))`, or Alembic's `create_table_if_not_exists` / `add_column_if_not_exists`) where the DDL may have been partially applied, so re-running a migration after a partial failure is always safe.

### Per-migration tracking

`alembic_version` records every applied revision (managed by Alembic). `core/database.migrate()` calls `alembic upgrade head` programmatically — it stashes the caller's sync connection on the Alembic config so no second engine is opened. The advisory lock in `migrate()` serializes concurrent callers (web + worker startup race).

Alembic CLI (`alembic revision --autogenerate -m "..."`) is the only supported way to create new revisions. Direct `alembic upgrade` at runtime is not used — the programmatic path is the contract.

### UUID primary keys

- Every UUID PK column carries `server_default=text("uuidv7()")`. No `default=uuid.uuid4` — if the Python default is set, SQLAlchemy fills the value app-side and the DB default is dead.
- Services and repositories never pass `id=` to a Row constructor. Drop it; the DB mints a v7 UUID on INSERT.
- Call `await session.flush()` before reading `row.id` if the PK is needed before commit (audit-log FK, child-row FK, return value). Where the row is added and never read before commit, no flush is needed beyond what the transaction already provides.
- **Exception — app-side identity ownership:** When a component owns an aggregate's identity in-memory before persistence, it mints the PK via `uuid.uuid7()` — never `uuid.uuid4()` — and passes it explicitly to the Row constructor. Two cases in the tree:
  - Agent-command producers (`core/agent_gateway._build_config_update_dto`, `core/workspace/commands.py`, `core/workspace/remote_provider.py`) minting `command_id`, and `core/workspace/commands.py` minting `workspace_id`. These IDs must exist app-side before the row is inserted — the `command_id` rides the wire to the agent and gates the atomic single-flight `current_command_id` claim, and the `workspace_id` is the agent's lifecycle handle. `command_id` becomes the `agent_commands` PK, which is the FIFO claim sort key (`claim_next` orders by `id`), so a random `uuid4` would scramble delivery order; `workspace_id` becomes the `workspaces` PK. Both inherit the column's `server_default=text("uuidv7()")` only on the rare insert that omits `id`.
  - `domain/pipelines.create_pipeline` minting `PipelineRow.id`. The `pipelines` table's `id` column carries no `server_default` (unlike its sibling tables `pipeline_runs`/`stage_executions`/`run_pauses` in the same migration) — a pipeline's id is part of its `PipelineDefinition` (referenced by other pipelines' `PipelineCallStage.pipeline_id`, and shipped templates carry pinned ids), so it must exist before the row is ever inserted. In practice the mint happens one level removed: `PipelineDefinition.id` defaults to a fresh `uuid7()` via Pydantic `Field(default_factory=...)` at request-parse time, and `create_pipeline` passes that already-minted `definition.id` straight through to `PipelineRow(id=definition.id, ...)`.
- Enforced by `apps/backend/.semgrep/uuid_pk_discipline.yaml` (two rules: `uuid-pk-no-python-default`, `uuid-pk-no-explicit-id-in-row-constructor`). Both red-fail CI. The taint rule treats both `uuid4(...)` and `uuid7(...)` as sources; no path exclusions are needed today — the mint and the `Row(id=...)` sink sit in different functions for every app-side-identity case in the tree, and the taint rule does not follow that interprocedural DTO-field hop, so the discipline there is convention-enforced, not linter-enforced.

## Durable tasks via `core/tasks`

Use [`core/tasks`](core_tasks.md) when work must survive backend restarts, has retry policy, or participates in a run. Use [`core/observability.spawn()`](core_observability.md) for fire-and-forget request-scoped background work without durability needs.

`@task` registers a body; `enqueue(task_ref, args, *, session)` writes a `taskiq_enqueue` row to `outbox_entries` in the caller's session. The drain (in `apps/backend/app/worker.py`) pushes outbox rows to Redis after commit. The atomic-in-session contract: task is durable iff the caller's transaction commits. The outbox table is private to `core/tasks` — domain modules never import it directly.

`enqueue` auto-stamps `current_traceparent()` into `TaskMetadata.traceparent`. `TaskSpanMiddleware` on the consumer side extracts it and uses it as the parent context for `task:<name>` spans — so all task spans land in the producer's trace rather than orphan per-task traces. No caller action required; the pipe is automatic.

Task bodies must be idempotent — a drain crash between dispatch and `dispatched_at` stamp can redispatch. Bodies look up state from DB (don't carry "do this once" semantics in the args).

## Multi-pod safe patterns

Canonical shapes for work that can run concurrently across multiple backend pods. Use these patterns — not ad-hoc alternatives — when adding new claims, periodic slots, or at-least-once bodies.

### Single-flight workspace claim

- **Pattern:** atomic conditional `UPDATE … WHERE current_command_id IS NULL AND status='active'`; caller checks `rowcount`. If `rowcount=0` the workspace is busy or inactive — back off, do not dispatch.
- **Why:** a `SELECT` then `UPDATE` is a TOCTOU race across pods; the single-statement UPDATE is the sole correct gate.
- **Reference:** `core/workspace.try_claim` at `apps/backend/app/core/workspace/dispatch.py:57`.
- **Invariant:** every `try_claim` call is paired with `release_claim(workspace_id, command_id=…)` after the terminal event is observed (failure-report-precedes-disposal).

### Migration runner advisory lock

- **Pattern:** `migrate()` holds a Postgres session-scoped advisory lock (`pg_advisory_lock`) on a fixed bigint key for the duration of `alembic upgrade head`.
- **Why:** web + worker start concurrently; without the lock the two processes race on the same `alembic_version` row and can double-apply a migration.
- **Reference:** `core/database.migrate()` at `apps/backend/app/core/database/service.py:177`.

### Recurring-task per-slot dedup (`@scheduled`)

- **Pattern:** every worker runs `scheduler_loop`; per matching cron slot the tick attempts `INSERT INTO scheduled_runs (schedule_id, fire_time) VALUES (…) ON CONFLICT DO NOTHING`. Only the pod whose insert wins (`rowcount=1`) calls `enqueue(…)`.
- **Why:** no leader election; cluster safety is the `INSERT … ON CONFLICT` atomicity guarantee. Mirrors the `github_webhook_events` dedup precedent.
- **Reference:** `core/tasks._try_claim` at `apps/backend/app/core/tasks/scheduler.py:159`.
- **See also:** [core_tasks.md § Multi-pod safe patterns](core_tasks.md#multi-pod-safe-patterns).

### At-least-once `@task` body contract

- **Contract:** the drain stamps `dispatched_at` only after a successful Redis push; a crash between push and stamp causes redispatch. Every `@task` body **must** be idempotent — it may execute more than once for a single logical event.
- **Idempotency rule:** read the durable row at body entry; no-op when state already indicates done or in-progress. Do not carry "do this once" semantics in the task args alone.
- **New body discipline:** a body that is not naturally idempotent (e.g. triggers an external side effect without a dedup key) requires a replay-safety test that delivers the task twice and asserts the effect occurs exactly once.

## Secrets

Every sensitive value crosses module boundaries as Pydantic `SecretStr`: encryption keys, OAuth client secrets + access/refresh tokens, TOTP master key, session tokens, invitation tokens, SMTP password, third-party API keys (Braintrust, Anthropic via `core/api_keys`), GitHub App private keys. `SecretStr` renders as `'**********'` in `repr`, `str`, `model_dump`, and `model_dump_json` so logs / tracebacks / audit payloads never carry plaintext.

`SecretStr` applies at **every** module boundary, not just Settings:

- **Request schemas** — any Pydantic `BaseModel` field that carries a user-submitted credential (API key set endpoints, OAuth callback bodies, etc.).
- **Value objects + dataclasses** — `Tokens.access_token`, `ProviderConfig.client_secret`, any frozen-dataclass field that holds a token, key, or secret in flight.
- **Function signatures** — parameters that pass a secret between modules, including provider Protocol methods (`validate(access_token: SecretStr)`) and constructors of typed contexts.

Call `.get_secret_value()` only at the byte boundary — Fernet construction, JWT sign, HTTP `Authorization` header, subprocess argv, broker payload heading out the door, the env dict of a wire-bound exec block. Never put a raw secret into: a log call, a Pydantic `model_dump` output, an exception message, an outbox payload, an audit-log entry, or an SSE event.

When decrypting a ciphertext column for use, wrap the plaintext in `SecretStr(...)` immediately on emergence so the rest of the call chain stays uniform.

## Dispatch helper discipline

Three layers gate every AgentCommand enqueue in the dispatch path:

- **Layer 1 (`enqueue_command`)** — raw primitive in `core/agent_gateway`. Only `core/workspace.dispatch_provision` (which has no workspace row yet) and `dispatch_via_workspace` call it directly.
- **Layer 2 (`dispatch_via_workspace`)** — `core/workspace/dispatch.py`. Loads the workspace row, calls `enqueue_command`, pins to the owning agent, optionally claims. `dispatch_cleanup` and `dispatch_auth_refresh` route here with `claim_workspace=False`.
- **Layer 3 (`coding_agent.dispatch_invocation`)** — `core/coding_agent/service.py`. Builds the `InvokeClaudeCodeCommand` from a high-level `Invocation`, calls Layer 2 with `claim_workspace=True`, inserts a `coding_agent_runs` row. `domain/pipelines`' skill-stage dispatch routes here.

`apps/backend/.semgrep/dispatch_helper_discipline.yaml` enforces this: direct calls to `enqueue_command`, `pin_command_to_agent`, `try_claim`, or `create_run` inside any `app/domain/*/commands/*.py` file fail CI. Canary: `app/core/workspace/test/test_dispatch_discipline_semgrep_canary.py`.

### Single-flight per workspace

The workspace state machine accepts one in-flight AgentCommand at a time. [`core/workspace.try_claim`](core_workspace.md) is an atomic conditional UPDATE that succeeds iff `current_command_id IS NULL` AND `status='active'`. Concurrent dispatch attempts see `rowcount=0` and back off. Pair every claim with `release_claim(workspace_id, command_id=…)` once the terminal event has been observed.

### Failure-report-precedes-disposal invariant

`release_claim` clears `current_command_id` but **preserves** `owning_agent_id` on the workspace row for observability. Command-to-run correlation lives on `agent_commands.run_id`, which is stamped by `dispatch` at enqueue time and read directly by `record_agent_event` and `failsafe_agent_loss`, so terminal events resolve their run after the workspace has been torn down.

### Recovery — auth-expired retry

`domain/pipelines`' run engine handles skill-stage `auth_expired` failures directly: it dispatches `core/workspace.dispatch_auth_refresh`, then retries the failing skill stage once (a one-retry cap tracked in `pipeline_runs.sendback_counts`). No standing recovery-policy registry — the engine owns the whole recovery flow inline.

## WorkspaceProvider contract

[`core/workspace`](core_workspace.md) declares the `WorkspaceProvider` Protocol; the only shipped implementation is `RemoteAgentWorkspaceProvider` (`remote_agent`, in `core/workspace/remote_provider.py`), which dispatches via [`core/agent_gateway`](core_agent_gateway.md). The Protocol is the seam between the control plane and the remote agent — the single-flight and failure-report-precedes-disposal invariants both enforce here.

The Protocol's `run_coding_agent_cli` is synchronous-shaped, but for the remote provider the workspace dispatch helpers enqueue AgentCommands and the run engine awaits terminal events through `handle_agent_event`. The Protocol shape is preserved so `app/testing/stub_workspace` can wrap the registered implementation without importing provider internals.

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
- **`log.info` is for business-meaningful state changes; routine progress uses `log.debug`.** `LOG_LEVEL=INFO` is the prod default — every `.info()` line ships to stdout and OTLP. Reserve `.info()` for: successful state transitions on domain entities, user-initiated mutations, webhook/event acceptance, configuration changes, and first-time lifecycle events (boot, worker started). Demote to `.debug()`: per-iteration sweep outputs, per-step progress inside a multi-step flow, guard skips ("skip_not_running"), stale-claim rejections, per-event confirmations that duplicate durable audit rows. Background errors are visible on traces (via `spawn()` exception recording), so demoting their accompanying progress logs is safe.

Audit: user-initiated mutations (prompt edits, lesson CRUD, "re-review"), agent-initiated actions (review/reply posted), state transitions with business meaning (review_job queued→running→posted; ticket in_review→complete).

Don't audit: internal helpers' progress steps, reads, routine sweeps that changed nothing.

Row shape:
- `kind` follows `<entity>.<verb_past>` — lowercase, dotted, past tense.
- `actor` is the `Actor` value object. Required.
- `payload` is a Pydantic model owned by the writing module. Plain dicts rejected.
- One entry per business event — not three for "started, did it, finished".

## Org scoping

Every domain function takes `org_id` kwarg or reads it from the `org_id_var` contextvar; every query filters by it. Two-track rule:

- **HTTP request handlers** — `Depends(require(Action.X))` resolves `X-Yaaos-Org-Slug` and sets the contextvar. Handlers can read it via `current_org_id()`.
- **Background work** — every non-HTTP entry point opens `with org_context(org_id, actor_kind, actor_id=None)` from [`core/auth`](core_auth.md). This sets the same contextvars + OTel span attrs (`yaaos.org_id`, `yaaos.actor_kind`, `yaaos.actor_id`) + structlog bound vars so background log lines + audit rows attribute correctly. Wrapped today: GitHub catch-up poller, `core/agent_gateway`'s AgentEvent/heartbeat endpoint handlers (`actor_kind=workspace`), taskiq task bodies (`actor_kind=SYSTEM` — via `OrgContextMiddleware` in `core/tasks`, not manual wrapping in each body). Scheduler cleanup jobs that don't emit audit rows + don't read from org-scoped tables (session/invitation/totp/audit purges) do NOT need a wrap — they're global by design.
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
- Per-(org, provider) API keys go through [`core/api_keys`](core_api_keys.md); provider plugins register their `validate(key) -> bool` callable via `api_keys.register_validator(provider, callable)` at bootstrap so `core/api_keys` stays free of plugin imports.

## Bearer token discipline

Every yaaos-issued bearer follows the same shape — adopted in for sessions, in again for signed invitations, and extended in for MCP review tokens:

- **Mint** with `secrets.token_urlsafe(32)` (32 random bytes, URL-safe base64). Return the raw token to the caller exactly once.
- **Store** `sha256(raw_token)` as the primary key. Raw tokens never persist.
- **Lookup** by hashing the inbound bearer + selecting by hash + checking `expires_at > now()`. Constant-time-safe because the hash is the PK.
- **Own one table per consumer.** `sessions`, `mcp_review_tokens`, and (via sha256-on-write) `invitations.token_hash` are separate; one bearer can't be substituted for another.
- **Expire by absolute time.** Each consumer owns its TTL — sessions 14d, MCP review tokens 2h, invitations 7d. The periodic cleanup task in `core/identity/scheduler` (or a module-local equivalent) deletes expired rows; production code also checks `expires_at` on every read.

## Intra-core layer order

`core/auth < core/tenancy < core/identity < core/sessions`. Each level may import from levels below it; reverse imports are forbidden. `core/auth` is the leaf — it holds `Role`, `Action`, `_REQUIRED_ROLE`, middleware, and contextvars with no domain knowledge.

## Route security declarations

Every `/api/*` path classifies as one of three `RouteSecurity` categories: `PUBLIC` (no auth), `USER_SCOPED` (session, no org), or `ORG_SCOPED` (session + `X-Yaaos-Org-Slug` + role check). The classifier `classify_route(path, method)` and the prefix/exact lists live in `app/core/auth/types.py`; the middleware enforces `X-Yaaos-Org-Slug` and CSRF based on the category. Route dependencies: `Depends(require(Action.X))` for `ORG_SCOPED`, `Depends(require_session)` (or `Depends(public_route)`) for `USER_SCOPED` handlers that read the session cookie, `Depends(public_route)` for `PUBLIC`. The post-response middleware guard returns 500 if a 2xx response left `route_security_resolved` unset. Action → minimum-role map lives in `app/core/auth/role_policy._REQUIRED_ROLE`; adding a new action is a code change, not config. Adding a new URL prefix requires placing it in exactly one of the three category sets in `app/core/auth/types.py`.

## Testing

### Categories

| Category | Where | What | External deps |
|---|---|---|---|
| Unit | `<module>/test/test_*.py` | Pure logic, one function/class. Used sparingly. | None |
| Integration | `<module>/test/test_*.py` | Module's public interface end-to-end. **Primary form.** | Real Postgres (transactional rollback); `apps/fake-github`; coding-agent CLI stub. |
| Service | `<module>/test/test_*_service.py` | Cross-module flow (3+ modules) driven from an entry point, in-process. | Real Postgres; stub plugins. |
| E2E | `apps/e2e/` | Browser-visible behavior — SSE updates, cookies, OAuth redirects, route navigation. | `docker-compose.test.yml`. |

### Service tests

When a backend flow crosses **3+ modules** (e.g. webhook → intake → pipelines → vcs.post_finding → audit), write ONE service test that drives the entry-point function or HTTP route end-to-end and asserts the durable state across every module it touches. Service tests are the **default** for backend-only flows; reach for Playwright only when the contract is browser-visible.

Mechanics:

- **Real Postgres via `db_session`.** Transactional rollback per test — production code's `session()` hits the override; inner `commit()` calls become SAVEPOINT releases; outer transaction rolls back on teardown. Empty DB at start of each test.
- **Stub plugins from `app/testing/`.** `YAAOS_CODING_AGENT_STUB=1` (set by `conftest.py`) wraps registered coding-agent plugins with `StubCodingAgentPlugin` that implements `compile_invocation` + `parse_result` without spawning any CLI. `app.testing.stub_workspace.wrap_all_registered_workspace_providers()` wraps the registered `WorkspaceProvider` with a no-op `StubWorkspaceProvider` (used by the dev stub mode; not used in service tests, which simulate agent events directly).
- **HTTP routes via `httpx.ASGITransport`.** Drive endpoints in-process without a network listener. The pattern is already used by `app/domain/integrations/test/test_endpoints.py`, `app/domain/mcp_proxy/test/test_dispatch.py`, etc.
- **Seed helpers from `app/testing/e2e_setup/`.** `seed_github_install`, `seed_lesson`, etc. are HTTP shims around the same domain calls a Playwright spec would hit — reuse them from pytest.

Naming: `test_<flow>_service.py` in the owning module's `test/` directory. Owner is whichever module holds the entry-point function (the one you `await` first in the test body).

Marker: every service test is decorated `@pytest.mark.service`. Run only the service tier with `pytest -m service`; run the fast unit-only loop with `pytest -m "not service"`. The default `bin/ci` invocation runs both — the marker is for developer ergonomics, not a CI skip.

Assert on the **durable state production reads** — audit rows by kind, posted-comment count via the stub vcs plugin, finding state in the aggregate, `last_refresh_status`, the email inbox (via `app.domain.orgs.read_sent_emails()`), event-bus publications. Don't assert on intermediate log lines unless the log is the contract.

### Integration test pattern

- Exercise public interface, not internals.
- Real Postgres. Each test runs inside a transaction rolled back at teardown. Empty DB at start.
- Inbound HTTP: `fastapi.testclient.TestClient` or `httpx.ASGITransport` in-process.
- Outbound HTTP: routed to `apps/fake-github` via `GITHUB_API_BASE_URL`. Real plugin code paths run.
- Coding-agent: `YAAOS_CODING_AGENT_STUB=1` swaps in `testing/stub_coding_agent`.

### Module boundaries in tests

Tests obey the **same import rules as production code** — enforced by `tach check --interfaces` in CI, which covers `app/testing/` as well as production code. Violations fail CI.

- Import only `__all__` exports — `from app.<module> import X`, never `from app.<module>.<submodule> import X` across module boundaries. Within a module's own test directory, direct submodule imports are allowed.
- No `*Row` types in cross-module imports. If a test in module B needs to inspect persisted state owned by module A, use module A's targeted public read function (e.g. `get_token_by_hash`, `find_session_by_hash`) or assert on the observable outcome instead.
- No test-only seams that bypass module interfaces. If a seam is needed, it belongs in `app/testing/` — but `app/testing/` is itself tach-governed; it may only import from `__all__`-gated module paths.
- Service tests of multi-hop pipelines are sliced per-hop: each service test exercises one entry point end-to-end; chain tests by asserting on the durable state that the next hop reads, not by calling internal functions of the next module.
- Singleton reset for test isolation: never poke private state via a submodule attribute (`mod._svc._singleton = None`). Use a named helper instead.
  - **Intra-module reach only** (module's own `test/` directory) → private `_*_for_tests` helper in the module's `service.py` (or sibling submodule), NOT in `__all__`, NOT in tach `expose`. Tests reach it via direct submodule import — intra-module, tach-permitted. Example: `redis._reset_clients_for_tests`, `orgs.onboarding._reset_contributors_for_tests`.
  - **Cross-module test machinery** (isolation fixtures, seed/cleanup, test harnesses) → lives in `app/testing/`, which calls each module's *production* `bind_*`/`register_*` APIs only. A test helper must NEVER be reachable across modules — not in `__all__`, not imported from another module's tests.
  - **ContextVar-bound holders** — for process-local in-memory singletons (Redis pubsub, agent dispatch queues, subscriber registry, email inbox) the preferred isolation pattern is ContextVar + `bind_*` production DI seam + autouse fixture in `app/testing/isolation`. No explicit reset is needed in individual tests — the autouse fixture binds a fresh instance per test. See `app/core/redis/pubsub.py` as the reference implementation.

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
| `YAAOS_RUN_STALL_THRESHOLD_SECONDS` | 300 | `domain/pipelines.resume_stalled_runs` grace window before a `running` pipeline run with no pending agent command (or a pending command already `done`) is treated as stalled. |

### Pytest plugin entry-point

Cross-cutting fixtures shared across every module's tests (transactional `db_session`, `fake_github_base_url`) live in the top-level `apps/backend/conftest.py` — pytest's own directory-hierarchy auto-discovery, not module-local. A genuine `[project.entry-points."pytest11"]`-registered plugin (`app.core.llm.pytest_plugin`, adding `--allow-llm-calls` + the LLM response cache) is the exception, used only where a CLI option is needed.

## Observability

### Structured logging

`structlog` everywhere; JSON to stdout. A `Logger` wrapper in `core/observability` injects request/trace context via a structlog filter.

### Context-variable threading

The identity contextvars (`org_id_var`, `user_id_var`, `actor_kind_var`, `actor_id_var`, `run_id_var`, `command_id_var`; see [core_auth.md](core_auth.md)) carry request/run context through async code. Web middleware sets them per request; `org_context(...)` sets the background-job equivalents. `spawn()` propagates the parent's context into the spawned coroutine. Log filters and span attributes read from them.

### When to add a manual span

Auto-instrumentation covers most paths (HTTP + SQLAlchemy via OTel contrib; background coroutines via `spawn`; httpx via `HTTPXClientInstrumentor` installed in `core/observability._configure_otel`). Add manual spans only at meaningful boundaries:

- Every external call — VCS API, coding-agent CLI, webhook signature verification. **VCS calls are already spanned by `core/vcs` dispatch helpers** (`vcs.{plugin_id}.{op}`); callers must use those helpers rather than calling `get_plugin(id).method(...)` directly. The httpx calls inside each plugin method appear as auto-instrumented child spans under the VCS dispatch span. **Coding-agent IO calls are already spanned by `core/coding_agent` dispatch functions** (`coding_agent.{plugin_id}.{op}`); callers must use `core/coding_agent.review(...)` and siblings rather than calling `get_plugin(id).review(...)` directly. See [core_coding_agent.md § Dispatch spans](core_coding_agent.md#dispatch-spans). **AgentCommand dispatches are already spanned by `core/agent_gateway.enqueue_command`** (`agent_command.dispatch.{kind}`); all callers must go through `enqueue_command` — no direct inserts into `agent_commands`. See [core_agent_gateway.md § Dispatch spans](core_agent_gateway.md#dispatch-spans).
- Every plugin entry point — `WorkspaceProvider.provision` (VCS and coding-agent Protocol methods are covered by the dispatch helpers above).
- Long phases inside a background coro — review_job phase transitions each get a span so the trace shows where wall time went.

Don't wrap every domain function — noise hurts more than detail helps.

### Exception visibility — `record_exception` rule

**Any `except` clause that does not re-raise must call `span.record_exception(exc)` + `span.set_status(ERROR, ...)` on the active span before returning.** Without this, OTel sees the span close without status and the error is invisible in traces even though a log line was emitted.

Canonical shape: `span.record_exception(exc)` → `span.set_status(StatusCode.ERROR, str(exc))` → `log.exception(...)`. Reference: `apps/backend/app/core/observability/spawn.py:56`.

Enforced site:

- **FastAPI catch-all in `core/webserver/app_factory.py`** — the `@app.exception_handler(Exception)` handler calls `trace.get_current_span().record_exception(exc)` + `set_status(ERROR, "internal_server_error")` before logging and returning the 500 JSON. This marks the HTTP request span red in Dash0 so unhandled server errors are visible in traces, not just logs.

Signal-selection order when adding observability to a new catch site:

1. **Attribute** — if the error is expected and queryable (e.g. auth failures with a typed exception), set a span attribute (`span.set_attribute("error.kind", "auth_failure")`).
2. **Child span** — for a unit of work with its own identity (e.g. a per-stage dispatch call), open a child span and record the error there; propagate status to the outer span.
3. **Log** — always emit at least a log line (exception log for unexpected errors; warn for expected but notable).

**Grep recipe** to find non-re-raising catches that may be missing span recording: `rg "except Exception|except:" apps/backend/app/`. Review each hit — if it doesn't call `record_exception`, it should (or the exception should be re-raised).

**Test infra** — `app.testing.observability.span_capture()` is the standard context manager for asserting span state in service tests. It installs an `InMemorySpanExporter` with a `SimpleSpanProcessor` on the current global `TracerProvider` and yields the exporter. Call `.get_finished_spans()` after the `with` block. Never import `span_capture` from production code — it lives under `app/testing/` exclusively.

### Two bearer tokens — never cross

The backend's own OTLP bearer (`YAAOS_BACKEND_DASH0_BEARER_TOKEN` → `Settings.yaaos_backend_dash0_bearer_token`) and the agent's OTLP bearer (`YAAOS_AGENT_DASH0_BEARER_TOKEN` → `Settings.yaaos_agent_dash0_bearer_token`) serve distinct principals and must never be swapped. Both are `SecretStr | None`; both unwrap only at their respective wire-encode boundaries. The backend bearer is consumed only inside `core/observability._configure_otel`; the agent bearer is consumed only inside `core/agent_gateway._build_config_update_dto` (forwarded to the agent as `AgentConfig.otlp_token` on the ConfigUpdate row enqueued at identity exchange).

## ContextVar-bound registries — test isolation model

The three plugin registries (`CodingAgentRegistry`, `VCSRegistry`, `WorkspaceRegistry`) and the process singletons (`RedisPubsub`, `SubscriberRegistry`, email inbox, SSE shutdown event) are all held in `ContextVar`s. Production never calls `bind_*()` — each module holds a module-level default that captures import-time `bootstrap()` registrations. Test isolation is structural: bind a fresh copy per test, no restore needed.

Session-scoped `_canonical_registries` fixture (in `app/testing/isolation.py`): imports the three plugin packages (triggering import-time bootstrap), optionally wraps with stubs, then snapshots the bound registries via `.copy()`. Runs once per session.

Function-scoped autouse `plugin_registries_isolation` fixture: calls `set_X_for_tests()` with a `.copy()` of each canonical snapshot before each test. A test that mutates a registry only affects its own copy; the next test gets a fresh canonical copy — no restore, no leak, no order dependence.

Function-scoped autouse `sse_shutdown_event_isolation` fixture: calls `set_shutdown_event_for_tests()` (from `app.core.sse`) before each test so every test starts with a fresh unset event. A test that calls `shutdown()` cannot leak a stale set-event into the next test.

`core.vcs.set_vcs_for_tests(plugin=X)` — context manager for ad-hoc per-test VCS swaps; binds a fresh copy of the current registry with the plugin replaced and restores the prior binding on exit. Import from `app.core.vcs`.

`core.tasks.service.scoped_task_registration(task_ref)` — intra-module helper; lives in `service.py`, not re-exported from the package `__all__`. Tests inside `core/tasks/test/` import it via direct submodule import. Call `@task(name)(fn)` to get a `TaskRef`, then wrap the test body in `with scoped_task_registration(ref)`. On exit the name is popped from the broker registry so subsequent tests can reuse the same name.

Rules:
- No wholesale-wipe or `unregister_*` loop between tests. The autouse fixture handles isolation structurally.
- `set_vcs_for_tests` binds on entry, restores prior singleton on exit. The yielded value is the bound instance.
- Never alias the canonical registry dict in a helper — always `.copy()` to prevent leakage.

## Subscription self-cleanup (async generator pattern)

An async generator whose `finally` clause does its own cleanup is the canonical subscriber pattern. The generator registers a queue on entry; `finally` pops it on any consumer exit — normal return, `break`, exception, or `aclose()`. Callers never call an explicit `unsubscribe()`. `core/sse` uses this pattern for SSE stream subscribers.

Preferred test shapes for consuming one event then exiting:
- `async for ev in subscribe(filter): ...; return` — `return` exits the coroutine; the event loop's async-gen finalizer schedules `aclose()`. Yield one event-loop tick (`await asyncio.sleep(0)`) after the consumer finishes if the test asserts `subscriber_count() == 0`.
- `async with aclosing(subscribe(filter)) as gen: ev = await gen.__anext__()` — `aclosing.__aexit__` awaits `gen.aclose()` synchronously, so cleanup is guaranteed before the `async with` block exits. Preferred when early exit is needed and the test asserts cleanup immediately.

Use this pattern over a `register/unregister` pair whenever the consumer naturally iterates — the single-seam generator is simpler and harder to misuse.

## Module lifecycle — `shutdown()` convention

Every runtime-state module exposes a public `async def shutdown()` in `__all__`. Naming is uniform; internals may delegate to library-conventional names (`aclose` for Redis, `dispose` for SQLAlchemy, taskiq broker close). Modules self-register at import time (after `__all__` is defined) with the relevant process registry by calling `register_web_shutdown_hook(shutdown)` and/or `register_worker_shutdown_hook(shutdown)` from `app.core.shutdown_registry`.

Categorization rule:
- Web-presence only (SSE, WebSocket) → register with web registry.
- Worker-presence only → register with worker registry.
- Shared infra (redis, database, tasks) → register with both.

The registries live in `app.core.shutdown_registry` (a zero-dependency standalone module) to avoid circular imports between modules that import each other.

## Two process lifecycles, two registries

Web and worker are separate OS processes with separate shutdown cadences. `app.core.shutdown_registry` owns both:

- `register_web_shutdown_hook` / `iter_web_shutdown_hooks` — used by the web process.
- `register_worker_shutdown_hook` / `iter_worker_shutdown_hooks` — used by the worker process.

Both registries are re-exported from `core.webserver` and `core.tasks` for convenience; the canonical source is `app.core.shutdown_registry`.

FastAPI lifespan teardown (in `core/webserver/app_factory.py`) iterates `iter_web_shutdown_hooks()` in reverse order. Worker runtime teardown (in `core/tasks/runtime.py`) iterates `iter_worker_shutdown_hooks()` in reverse order. Reverse order means the most-recently-registered (most-dependent) modules shut down first.

`app/web.py` and `app/worker.py` pin the foundational shutdown order by explicitly importing `app.core.database` and `app.core.redis` near the top of step 2, before any module that depends on them. That guarantees those two register their hooks first and therefore shut down last — anything imported transitively later (tasks, sse, agent_gateway) shuts down before them. Don't rely on transitive imports for hook ordering; pin the ones that matter.

Both loops wrap each hook call in `try/except` (web) or `contextlib.suppress` (worker) so one failing hook does not abort the sequence.

## Composition roots — `app/web.py` and `app/worker.py`

Both composition roots live inside `app/` so they're importable as regular Python modules and testable without exec tricks.

- `app/web.py` — web process entry. See § Bootstrap composition order. Ends with `app = webserver.create_app()`. When run directly (`python apps/backend/app/web.py`) the `if __name__ == "__main__"` block calls `uvicorn.run(app, ...)` with all server flags in Python — no flags scattered across Dockerfile CMDs. It passes the built `app` **object**, not the `"app.web:app"` import string: a string makes uvicorn re-import the module (distinct from the running `__main__`), executing the whole composition root — every module-level registration — a second time. Passing the object runs the bootstrap once. Cost: no uvicorn reload/multi-worker (both need an import string), unused since the backend runs single-process per container. Local dev that wants reload runs uvicorn directly (`uvicorn app.web:app --reload`), which imports the module once and never executes `__main__`.
- `app/worker.py` — worker process entry. Side-effect imports (the run-engine modules, plugins, workspace providers) + `asyncio.run(core.tasks.runtime.run())`. When run directly the `if __name__ == "__main__"` block is the sole entry point.

Dockerfile CMDs are exec-form `["python", "apps/backend/app/web.py"]` / `["python", "apps/backend/app/worker.py"]`. tini is PID 1 (image-level `ENTRYPOINT ["/usr/bin/tini", "--"]`) and forwards SIGTERM to the Python child, triggering graceful shutdown via the Phase-1 shutdown registries.

`bin/worker` is gone — that path now lives at `app/worker.py`.

## Bootstrap composition order

`app/web.py` is load-bearing. Don't reorder.

1. Load environment — `app.core.config`.
2. Configure core infra — `app.core.database`, `app.core.observability`.
3. Import webserver registry — `app.core.webserver` *before any module registers routes*.
4. Core modules with plugin Protocols — `app.core.audit_log`, `app.core.coding_agent`, `app.core.vcs`, `app.core.workspace`.
5. Domain modules in dependency order — types first (lessons), then leaf domain modules, then dependents.
6. Plugins — `claude_code`, `github`.
7. Test-mode wrapping (conditional) — when `YAAOS_CODING_AGENT_STUB=1`, import `app.testing.stub_*` and call `wrap_all_registered_*()`.
8. Build the FastAPI app — `webserver.create_app()`.
9. Test-mode HTTP surface (conditional, non-prod only) — `webserver.mount_testing_endpoints(app, settings)` (production-safety gate; raises if `is_production`), then `e2e_setup.mount(app)` (direct `app.include_router` call — registers `/api/testing/*` routes immediately so they appear in `app.routes` before the liveness check). `core/webserver` cannot import `app.testing` (layering: `core < testing`), so the actual registration happens here in the composition root.
10. Defense-in-depth — `webserver.assert_no_testing_routes_in_prod(app, settings)` sweeps `app.routes` and raises if a `/api/testing/` path is present while `is_production=True`.

Each module imported in steps 2–6 appends its `shutdown()` hook to the relevant process registry as a side effect of import. By step 8, all hooks are registered before `create_app()` wires them into the lifespan.
