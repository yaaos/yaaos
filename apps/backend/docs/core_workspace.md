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

Carries `id` plus three methods: `info()` returns `WorkspaceInfo`; `run_coding_agent_cli(argv, *, env=None, stdin=None, timeout_seconds=None, on_stream_line=None)` returns `CodingAgentCliResult`; `read_text(path)` returns the workspace-relative file content or `None`. No `working_dir` exposed. Callers hand `argv` + `env` + `stdin` to the workspace; the workspace forwards to the provider, which decides where and how (cwd, container, sandbox). `read_text` is the narrow file-read hook the incremental-review anchor re-resolver uses (plan §6.2 step 4b) — providers implement it with path-traversal protection. Subprocess timeout + process-group kill are the provider's responsibility.

`on_stream_line: Callable[[bytes], Awaitable[None]] | None` is optional. When provided, the provider reads stdout line-by-line and invokes the callback per line (consumers parse JSON inline so they can react live, e.g. the Claude Code plugin rendering activity events). When `None`, the provider buffers stdout to completion — the existing behaviour. Timeout + cancel kill paths are unchanged either way.

Each new capability (run tests, install deps, push commits) arrives as a deliberate new method with its own policy. A generic `exec(argv)` would silently broaden as features land.

### `WorkspaceProvider` Protocol (plugin contract)

Each provider carries `meta: PluginMeta` and five methods: `provision(spec)`, `run_coding_agent_cli(plugin_state, argv, ...)`, `read_text(plugin_state, path)`, `destroy(plugin_state)`, `health_check()`.

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

Started from `lifespan` via `start_reaper(interval_seconds)`, which calls `core/observability.spawn("workspace.reaper", _reaper_loop(...))`. Loop: sweep, sleep `YAAOS_REAPER_INTERVAL_SECONDS` (default 30s in prod; short in tests).

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

Operational endpoints, unauthenticated. POC limitation — not safe for production.

### Single-flight claim + recovery

The workspace state machine runs one in-flight AgentCommand at a time. `try_claim(workspace_id, *, command_id, workflow_execution_id, session)` performs an atomic conditional `UPDATE` that succeeds iff the row is `status='active'` AND `current_command_id IS NULL`; concurrent callers racing the same workspace see `rowcount=0` and back off. `release_claim(workspace_id, *, command_id, session)` clears the claim only when the supplied command id still owns it — making it idempotent and safe against stale event redelivery. The terminal event must arrive before disposal: `release_claim` clears `current_command_id` but **preserves** `current_holder_workflow_id` so reconciliation / audit lookups can still find which workflow last touched the workspace.

The recovery-policy registry (`register_recovery_policy(failure_label=, command_kind=)`, `get_recovery_policy(label)`, `registered_recovery_labels()`) maps AgentCommand failure labels to lifecycle WorkflowCommand kinds. One policy ships at boot: `auth_expired → RefreshWorkspaceAuth`.

### Lifecycle commands

`commands.py` ships three `WorkflowCommand`s — `ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`. All Workspace-category, all `restart_safe=True`. Registered against the engine via `domain/reviewer` bootstrap so the reviewer workflows can reference them.

- `CleanupWorkspace` has a real body: reads `workspace_id` from inputs and calls `close_workspace()`. Idempotent — missing/invalid/unknown ids return success so partial-failure workflows still drain.
- `ProvisionWorkspace` has a real body: fetches the ticket context via the registered `WorkflowContextProvider` (see [Workflow-context callback](#workflow-context-callback)), builds a `WorkspaceSpec`, calls `create_workspace()`, returns `workspace_id` in outputs. Fails cleanly when no provider is registered, the ticket isn't found, or the underlying create fails.
- `RefreshWorkspaceAuth` has a real body: a no-op-success for the in_memory provider (in-process provider re-fetches a fresh installation token on each git fetch/clone, so there's no stored credential to refresh). The remote_agent provider currently inherits the same no-op body.

### Workflow-context callback

`workflow_context.py` exposes a singleton `WorkflowContextProvider` Protocol that bridges `core/workspace` to `domain/tickets` without crossing the `core → domain` layer boundary. The domain layer registers a concrete reader at boot via `register_workflow_context_provider(provider)`; `ProvisionWorkspace.execute()` reads it via `get_workflow_context_provider()` and calls `await provider.get_workspace_ticket_context(ticket_id)` to fetch the ticket's `org_id`, `plugin_id`, `repo_external_id`, and `payload`. Registration is idempotent-replace so test reloads don't conflict; `_reset_workflow_context_provider_for_tests()` clears the singleton.

The bridge is registered from [`domain/reviewer/__init__.py`](domain_reviewer.md) at module-import time, alongside the workflow + command registrations.

### Idle-timeout sweep

The reaper's second sweep marks any workspace that is `status='active'`, holds no claim, and has been activated longer than `max_idle_seconds` (default 600s) as `expired` so the destroy pass picks it up. Workspaces with a live claim are skipped — those are the engine's; cancellation goes through `workflow.request_cancel`. This is the cleanup-failsafe layer above the TTL sweep, catching workspaces that completed their work but whose cleanup workflow never ran.

### Failsafe 6 — agent-loss recovery

Third reaper sweep (`_failsafe_agent_loss`): every org in `workspace_provider='remote_agent'` mode whose `workspace_agents` rows all have stale `last_heartbeat_at` (>90s) — or none at all — gets every in-flight workspace transitioned to `expired` with reason `agent_loss`. The sweep also calls `bearers.revoke_all_for_org(org_id, 'agent_loss')` so the agent must re-exchange identity on reconnect. POC scope: per-org match (not per-pod) since `workspaces` has no `agent_id` column. policy: no retry-on-different-agent — workflows referencing expired workspaces fail loud.

### Failsafe 7 — audit row per transition

Every state mutation in `service.py` routes through `_audit_transition`, which writes a `workspace.transitioned` row via `audit_for_workspace` ([`core/audit_log`](core_audit_log.md)). Payload: `from_state`, `to_state`, `reason`, optional `error`. Reasons include `provisioned`, `provision_failed`, `closed`, `force_close_all`, `ttl_expired`, `idle_timeout`, `agent_loss`, `destroy_attempt`, `destroyed`, `destroy_failed`, `provider_not_registered`. Powers the Workspace settings security feed.

### Failsafe 5 — proactive disk sweep (Go agent side)

Owned by `apps/agent/internal/supervisor` — not this module. Every 5 min the supervisor walks `YAAOS_WORKSPACE_ROOT`, reads `.workspace-id` manifests, and `os.RemoveAll`s any directory whose id isn't in its in-memory pool (or any dir with no manifest). Defence against orphans the backend's `forgotten_workspaces` response never names (agent crashed mid-create before reporting).

### POC limits

- `in_memory_workspace` ignores `ResourceCaps` and `NetworkPolicy` — the CLI runs with the same permissions as the yaaos process.
- Admin endpoints unauthenticated.
- Each review job gets its own workspace (three reviewers on one PR = three workspaces). Wasteful but coordination-free; acceptable at POC scale.

## Data owned

- `workspaces` — `(id, org_id, provider_id, spec jsonb, plugin_state jsonb, status, provider, current_command_id, current_holder_workflow_id, max_idle_seconds, created_at, activated_at, expires_at, destroyed_at, destroy_attempts, last_destroy_attempt_at, last_destroy_error)`. added `provider`, `current_command_id`, `current_holder_workflow_id`, `max_idle_seconds` via migration `017_workspaces_m05_columns`. Indexes: `(status, expires_at)` for the reaper's expiry sweep; `(org_id, created_at)` for org-scoped listings; `current_holder_workflow_id` for the event-to-workflow lookup chain; `org_id` indexed independently.

## How it's tested

`app/core/workspace/test/test_dispatch.py` () covers the single-flight claim + recovery registry directly: `try_claim` succeeds when unclaimed/active, loses on contention, refuses non-active rows; `release_claim` is idempotent and ignores wrong-command-id calls; the recovery registry idempotently re-registers the same target, raises on conflict, and the `auth_expired → RefreshWorkspaceAuth` boot policy is present. Lifecycle coverage (provision → active → close → expired → destroy → destroyed; destroy retries with attempt increment; `destroy_failed` after 3 attempts; `startup_recovery` flipping orphaned rows; admin endpoints via `TestClient`) is exercised end-to-end by reviewer integration tests and the workspace plugin's tests.
