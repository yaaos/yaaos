# core/workspace

> Provisioned environments for code work — DB-backed lifecycle, plugin actuators, and a reaper.

## Purpose

Owns the centralized lifecycle for every workspace yaaos creates. Defines the `Workspace` and `WorkspaceProvider` Protocols, holds the plugin registry, persists every workspace cradle-to-grave in `workspaces`, and runs the reaper background loop enforcing wall-clock caps, retrying plugin destroys, and escalating `destroy_failed` rows. Plugins are dumb actuators (provision + run + destroy + health-check); lifecycle policy lives here. The `Workspace` Protocol exposes operations (`run_coding_agent_cli`) — not paths — so future Docker/K8s plugins drop in without breaking consumers.

## Public interface

Exports value objects (`WorkspaceSpec`, `WorkspaceInfo`, `WorkspaceStatus`, `ResourceCaps`, `NetworkPolicy`, `RepoRefForSpec`, `CodingAgentCliResult`, `HealthStatus`), Protocols (`Workspace`, `WorkspaceProvider`), ORM row (`WorkspaceRow`), functions (`register_workspace_provider`, `get_provider`, `create_workspace`, `with_workspace`, `close_workspace`, `force_close_all`, `get_workspace_info`, `start_reaper`, `startup_recovery`, `health_check_all`), error types, and `_reset_providers_for_tests`. See `apps/backend/app/core/workspace/__init__.py`.

HTTP routes registered by the module under `/api/workspaces/*` (list, get, force-close, force-close-all, retry-destroy). The explicit `url_prefix` overrides the default `/api/workspace` to use the plural form.

## Module architecture

### Value objects

`WorkspaceSpec` describes what to provision: `repo`, `sha` (head), optional `branch_name` (head), optional `base_sha` + `base_branch` (the branch this PR will merge into — providers can fetch it so the agent can run `git diff base_sha..HEAD` itself instead of yaaos inlining the diff into the prompt), `resource_caps`, `network_policy`, and `org_id` (stamped by `create_workspace` so plugins can request VCS auth for the right org). `ResourceCaps` and `NetworkPolicy` are advisory — the in-process plugin doesn't enforce them; the value objects exist so the interface is stable for future plugins.

`WorkspaceInfo` is the consumer-facing snapshot (id, provider_id, sha, status, timestamps, `age_seconds`). Does NOT expose `working_dir` — internal paths are plugin-private.

### `Workspace` Protocol

Carries `id` plus two methods: `info()` returns `WorkspaceInfo`; `run_coding_agent_cli(argv, *, env=None, stdin=None, timeout_seconds=None, on_stream_line=None)` returns `CodingAgentCliResult`. That's the entire surface. No file or search methods, no `working_dir`. Callers hand `argv` + `env` + `stdin` to the workspace; the workspace forwards to the provider, which decides where and how (cwd, container, sandbox). Subprocess timeout + process-group kill are the provider's responsibility.

`on_stream_line: Callable[[bytes], Awaitable[None]] | None` is optional. When provided, the provider reads stdout line-by-line and invokes the callback per line (consumers parse JSON inline so they can react live, e.g. the Claude Code plugin rendering activity events). When `None`, the provider buffers stdout to completion — the existing behaviour. Timeout + cancel kill paths are unchanged either way.

Each new capability (run tests, install deps, push commits) arrives as a deliberate new method with its own policy. A generic `exec(argv)` would silently broaden as features land.

### `WorkspaceProvider` Protocol (plugin contract)

Each provider carries `meta: PluginMeta` and four methods: `provision(spec)`, `run_coding_agent_cli(plugin_state, argv, ...)`, `destroy(plugin_state)`, `health_check()`.

`provision()` returns an opaque `plugin_state` dict (e.g. `{"working_dir": "..."}` for in-process; `{"container_id": "..."}` for a future Docker plugin). `core/workspace` persists it; consumers never see it. `destroy()` must be idempotent and tolerate partial state.

### DB lifecycle

Every state transition is a row update on `workspaces`.

`create_workspace(provider_id, spec, *, org_id)`:
1. Stamps `spec.org_id = org_id`.
2. Inserts row with `status='creating'`, `expires_at = now() + wallclock_seconds`.
3. Calls `provider.provision(spec)`.
4. Success → row to `status='active'`, `activated_at=now()`, `plugin_state` set.
5. Exception → row to `status='destroy_failed'` with `last_destroy_error`; raises `WorkspaceProvisionError`.

`with_workspace(...)` is the standard context manager — `create_workspace` on entry, `close_workspace` on exit. Returns a `Workspace` handle.

`close_workspace(workspace_id)` flips `active`/`creating` → `expired`. Does NOT call `provider.destroy()` synchronously — that's the reaper's job. Keeps `close` fast and routes all retries through one place.

`force_close_all(*, org_id)` flips every `active`/`creating` workspace for the org to `expired` and returns the count.

### The reaper

Started from `lifespan` via `start_reaper(interval_seconds)`, which calls `core/primitives.spawn("workspace.reaper", _reaper_loop(...))`. Loop: sweep, sleep `YAAOS_REAPER_INTERVAL_SECONDS` (default 30s in prod; short in tests).

Per sweep:
1. **Expire over-budget.** `status='active' AND expires_at < now()` → `expired`.
2. **Destroy expired + creating.** Select up to 50 rows with `status IN ('expired','creating') AND destroy_attempts < 3` and call `_attempt_destroy` on each.

`_attempt_destroy(row)`:
- Provider not registered → `destroy_failed` with error.
- Flip to `destroying`, increment `destroy_attempts`, set `last_destroy_attempt_at`.
- Call `provider.destroy(plugin_state or {})`.
- Success → `status='destroyed'`, `destroyed_at=now()`, clear `last_destroy_error`.
- Exception → attempts ≥ 3 → `destroy_failed`; else → `expired` (next sweep retries). Either way, `last_destroy_error` stored.

After 3 failed retries the row sits in `destroy_failed` for operator attention.

### Startup recovery

`startup_recovery()` (called from lifespan before the first sweep) flips every row in `('creating', 'active', 'destroying')` to `'expired'`. Handles orphaned rows from prior crashes — the reaper picks them up next pass.

### Provider registry

Module-level `_PROVIDERS: dict[str, WorkspaceProvider]`. `register_workspace_provider(provider)` at plugin import (raises on duplicate id). `get_provider(provider_id)` looks up, raising `WorkspaceError` if missing. `_reset_providers_for_tests()` clears the dict.

`health_check_all()` aggregates `provider.health_check()` across the registry — drives the settings page's Plugin Health card. Errors become `HealthStatus(healthy=False, message=str(e))` rather than propagating.

### Admin HTTP endpoints

| Method + path | Purpose |
|---|---|
| `GET /api/workspaces` | List workspaces with filters. |
| `GET /api/workspaces/{id}` | Get one. |
| `POST /api/workspaces/{id}/close` | Force-close one. |
| `POST /api/workspaces/force_close_all` | Force-close every active workspace for the org. |
| `POST /api/workspaces/{id}/retry_destroy` | Reset `destroy_failed` → `expired` so the reaper retries. |

Operational endpoints, unauthenticated (documented POC limitation; tightened when auth lands).

### POC limits

- `in_process_workspace` ignores `ResourceCaps` and `NetworkPolicy` — the CLI runs with the same permissions as the yaaos process.
- Admin endpoints unauthenticated.
- Each review job gets its own workspace (three reviewers on one PR = three workspaces). Wasteful but coordination-free; acceptable at POC scale.

## Data owned

- `workspaces` — `(id, org_id, provider_id, spec jsonb, plugin_state jsonb, status, created_at, activated_at, expires_at, destroyed_at, destroy_attempts, last_destroy_attempt_at, last_destroy_error)`. Indexes: `(status, expires_at)` for the reaper's expiry sweep; `(org_id, created_at)` for org-scoped listings; `org_id` indexed independently.

## How it's tested

`app/core/workspace/test/` is a placeholder; exercised end-to-end by reviewer integration tests and the workspace plugin's tests. Coverage spans: provision → active → close → expired → destroy → destroyed; destroy retries with attempt increment; `destroy_failed` after 3 attempts; `startup_recovery` flipping orphaned rows; admin endpoints via `TestClient`.
