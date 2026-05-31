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
| `VCSPlugin` | `domain/vcs` | `plugins/github` |
| `CodingAgentPlugin` | `domain/coding_agent` | `plugins/claude_code` |
| `WorkspaceProvider` | `core/workspace` | `core/workspace/remote_provider` (`remote_agent`) |

Each plugin exposes `meta: PluginMeta` (`id`, `type`, `display_name`, `description`, `docs_url`). The `id` is the registry key, URL prefix, and canonical accessor.

## Structural patterns

- **Workflow engine** — every review run passes through [`core/workflow`](core_workflow.md); commands are typed Pydantic steps dispatched by category (Workspace / Local / HITL).
- **`PRReviewAggregate`** — durable layer in [`domain/reviewer`](domain_reviewer.md); survives restarts; owns `Review` / `Finding` state across runs.
- **Plugin registry** — ContextVar-bound instances (`CodingAgentRegistry`, `VCSRegistry`, `WorkspaceRegistry`) keyed by `meta.id`; per-test isolation binds a fresh copy so tests never share state.
- **Two process lifecycles** — web (`app/web.py`) and worker (`app/worker.py`); each registers shutdown hooks independently via `app.core.shutdown_registry`.
- **Composition roots** — `app/web.py` and `app/worker.py` own all side-effect imports; bootstrap order is load-bearing (see [`patterns.md § Bootstrap composition order`](patterns.md#bootstrap-composition-order)).

## Key flows

Each flow is a labeled hop-list. Module docs have the detail.

**Review lifecycle** (PR ready → findings posted):
`plugins/github` webhook → [`domain/intake`](domain_intake.md) filter → [`domain/reviewer`](domain_reviewer.md) `start_pr_review` → `core/workflow` dispatch → [`domain/coding_agent`](domain_coding_agent.md) → admission → [`domain/vcs`](domain_vcs.md) `post_review`

**Push → incremental review**:
`plugins/github` push event → `domain/intake` → `domain/reviewer` incremental path → `core/workflow` → `domain/coding_agent` `incremental_review` → admission → `domain/vcs` `post_review`

**Session / auth chain** (inbound request):
[`core/auth`](core_auth.md) middleware classify → [`core/sessions`](core_sessions.md) `require(Action.X)` → [`core/tenancy`](core_tenancy.md) `resolve_auth_org` → handler

**Workflow-engine step dispatch**:
[`core/tasks`](core_tasks.md) worker dequeues `route_workflow` → [`core/workflow`](core_workflow.md) resolves next command → Workspace/Local/HITL branch → outcome persisted → enqueues next step

**SSE fanout**:
domain module publishes `ActivityEvent` → [`core/sse`](core_sse.md) Redis pub/sub → SSE subscriber generators → browser
