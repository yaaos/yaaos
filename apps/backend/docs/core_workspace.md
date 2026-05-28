# core/workspace

> Provisioned environments for code work — DB-backed lifecycle, plugin actuators, and a reaper.

## Scope

- **Owns:** `Workspace` + `WorkspaceProvider` Protocols, provider registry, `workspaces` table lifecycle, reaper background loop, single-flight claim/recovery registry, three `WorkflowCommand` impls (`ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`).
- **Does not own:** lifecycle *policy* (that's callers); workspace filesystem internals (plugin-private); `domain/tickets` data (bridged via [Workflow-context callback](#workflow-context-callback)).
- **Receives:** `WorkspaceSpec` from callers; terminal AgentEvents from [`core/agent_gateway`](core_agent_gateway.md) trigger reaper. **Emits:** `workspace.transitioned` audit rows via [`core/audit_log`](core_audit_log.md); `WorkflowCommand` events to [`core/workflow`](core_workflow.md).

## Why / invariants

- **`close_workspace` does not call `provider.destroy()` synchronously** — keeps close fast; all destroy retries flow through the reaper. Only the reaper destroys.
- **`on_stream_line` callback** — when provided, the provider reads stdout line-by-line (live JSON parsing); when absent, buffers to completion. Timeout/cancel paths are unchanged either way.
- **Each new workspace capability is a deliberate named method** (`run_coding_agent_cli`, `read_text`). A generic `exec(argv)` would silently broaden.
- **`release_claim` preserves `current_holder_workflow_id`** even after clearing `current_command_id` — audit/reconciliation lookups still find which workflow last touched the workspace.
- **Workspace-context callback bridges `core → domain`** without violating layer order: `domain/reviewer` registers a concrete `WorkflowContextProvider` at boot; `ProvisionWorkspace` reads it to fetch ticket context. See [`domain/reviewer/__init__.py`](domain_reviewer.md).

## Gotchas

- **Admin HTTP endpoints are unauthenticated.** Gate behind an authenticated proxy before exposing to untrusted networks.
- **`ResourceCaps` / `NetworkPolicy` are advisory** — the `in_memory` plugin ignores them; values exist to stabilise the interface for future plugins.
- **Failsafe-5 (disk sweep) is in the Go agent** (`apps/agent/internal/supervisor`), not here.
- **Never hand-edit `tach.toml`** — run `apps/backend/bin/sync_modules` after interface changes.

## Vocabulary

- **WorkspaceProvider** — dumb actuator (provision + run + destroy + health-check); no policy.
- **plugin_state** — opaque dict returned by `provision()`, persisted by this module, never exposed to consumers (e.g. `{"working_dir": "..."}` for in-process).
- **Reaper** — background loop enforcing TTL expiry, idle-timeout, agent-loss detection, and destroy retries.
- **Recovery-policy registry** — maps AgentCommand failure labels to `WorkflowCommand` kinds. Boot ships `auth_expired → RefreshWorkspaceAuth`.

## Data owned

- `workspaces` — `(id, org_id, provider_id, spec jsonb, plugin_state jsonb, status, provider, current_command_id, current_holder_workflow_id, max_idle_seconds, created_at, activated_at, expires_at, destroyed_at, destroy_attempts, last_destroy_attempt_at, last_destroy_error)`. Indexes: `(status, expires_at)`, `(org_id, created_at)`, `current_holder_workflow_id`, `org_id`.

## How it's tested

`app/core/workspace/test/test_dispatch.py` covers `try_claim` / `release_claim` contention and the recovery-policy registry. Lifecycle coverage (provision → active → close → expired → destroy → destroyed; retry increment; `destroy_failed` after 3 attempts; `startup_recovery`) lives in reviewer integration tests and the workspace plugin's own tests.
