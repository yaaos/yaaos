# Backend architecture

> Internal structure of the FastAPI backend: layers, extension points, structural patterns, and key cross-module runtime flows.

## Layers

Four layers. Each may depend only on lower layers. `tach` (via `bin/sync_modules --check`) enforces edges; `bin/check_table_access` enforces table ownership.

| Layer | Path | Role |
|---|---|---|
| `core` | `app/core/` | Infrastructure + technical primitives. No business logic. |
| `domain` | `app/domain/` | Business logic. Defines plugin Protocols. Vendor-neutral. |
| `plugins` | `app/plugins/` | Vendor-specific Protocol implementations. |
| `testing` | `app/testing/` | Test-only scaffolding. Excluded from prod wheel. |

`core` may define domain-aware *data types* (e.g., `Actor` references the agent concept) but never *behaviour* encoding business decisions. No `core→domain` edges exist — the audited allowlist `PERMITTED_CROSS_LAYER_EDGES` in `bin/sync_modules` is empty (`frozenset()`); the tenancy split removed every former audited edge. All org/membership lookups from `core` go through [`core/tenancy`](core_tenancy.md).

No circular dependencies exist in the module graph. `forbid_circular_dependencies = true` in `tach.toml` (emitted by `bin/sync_modules`) makes tach reject any new cycle at CI time. Layer ordering (`core < domain < plugins < testing`) is enforced by `check_layering()` in `bin/sync_modules` — tach's `--interfaces` mode does not enforce layers, so the Python check is the sole layer enforcer. Both are canary-tested in `apps/backend/bin/test_module_boundaries.py`.

## Extension points

Protocols define seams; plugins implement them. Registration happens at import time (in the plugin's `__init__.py`); `app/web.py` imports every active plugin package.

| Protocol | Hosted in | Implemented by |
|---|---|---|
| `VCSPlugin` | `core/vcs` | `plugins/github` |
| `CodingAgentPlugin` | `core/coding_agent` | `plugins/claude_code` |
| `WorkspaceProvider` | `core/workspace` | `core/workspace/remote_provider` (`remote_agent`) |

Each plugin exposes `plugin_id: str`. The `plugin_id` is the registry key, URL prefix, and canonical accessor.

## Structural patterns

- **Run engine** — every pipeline run passes through [`domain/pipelines`](domain_pipelines.md); stages (`skill`/`review`/`action`/`call`) are data-defined and dispatched by kind.
- **Durable findings** — `domain/findings` owns the ticket-level `pipeline_findings` table; survives restarts; a review stage's reported findings materialize immediately, independent of the run.
- **Plugin registry** — ContextVar-bound instances (`CodingAgentRegistry`, `VCSRegistry`, `WorkspaceRegistry`) keyed by `plugin_id`; per-test isolation binds a fresh copy so tests never share state.
- **Two process lifecycles** — web (`app/web.py`) and worker (`app/worker.py`); each registers shutdown hooks independently via `app.core.shutdown_registry`.
- **Composition roots** — `app/web.py` and `app/worker.py` own all side-effect imports; bootstrap order is load-bearing (see [`patterns.md § Bootstrap composition order`](patterns.md#bootstrap-composition-order)).

## Key flows

Each flow is a labeled hop-list. Module docs have the detail.

**Pipeline run lifecycle** (PR ready → findings posted):
`plugins/github` webhook → [`core/intake`](core_intake.md) filter → `domain/repos` trigger-binding lookup → ticket created → `domain/pipelines.start_run(ticket_id=…)` → the run engine drives the flattened stage list (skill/review stages dispatch a coding-agent invocation and park on the terminal AgentEvent; action stages run synchronously) → a review stage's reported findings materialize as durable `domain/findings` rows → an action stage (e.g. `github:create_pr`) posts them via [`core/vcs`](core_vcs.md). The skill owns all filtering — there is no admission pipeline.

**Session / auth chain** (inbound request):
[`core/auth`](core_auth.md) middleware classify → [`core/sessions`](core_sessions.md) `require(Action.X)` → [`core/tenancy`](core_tenancy.md) `resolve_auth_org` → handler

**Run-engine stage dispatch**:
[`core/tasks`](core_tasks.md) worker dequeues `ROUTE_RUN` → [`domain/pipelines`](domain_pipelines.md) resolves the next stage → `START_STAGE` dispatches it (skill/review/action/system) → outcome persisted → enqueues the next `ROUTE_RUN`

**SSE fanout**:
domain module publishes `ActivityEvent` → [`core/sse`](core_sse.md) Redis pub/sub → SSE subscriber generators → browser
