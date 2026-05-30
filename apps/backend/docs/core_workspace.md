# core/workspace

> Provisioned environments for code work — DB-backed lifecycle, plugin actuators, and a reaper.

## Scope

- **Owns:** `Workspace` + `WorkspaceProvider` Protocols, provider registry, `workspaces` table lifecycle, reaper background loop, single-flight claim registry, three `WorkflowCommand` impls (`ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`). Implements and registers `WorkspaceAgentReportSink` (the IoC seam to [`core/agent_gateway`](core_agent_gateway.md)).
- **Does not own:** lifecycle *policy* (that's callers); workspace filesystem internals (plugin-private); `domain/tickets` data (bridged via [Workflow-context callback](#workflow-context-callback)).
- **Receives:** `WorkspaceSpec` from callers; AgentEvent ingestion goes through the registered sink. **Emits:** `workspace.transitioned` audit rows via [`core/audit_log`](core_audit_log.md); `WorkflowCommand` events to [`core/workflow`](core_workflow.md).

## Why / invariants

- **`close_workspace` does not call `provider.destroy()` synchronously** — keeps close fast; all destroy retries flow through the reaper. Only the reaper destroys.
- **`on_stream_line` callback** — when provided, the provider reads stdout line-by-line (live JSON parsing); when absent, buffers to completion. Timeout/cancel paths are unchanged either way.
- **Each new workspace capability is a deliberate named method** (`run_coding_agent_cli`, `read_text`). A generic `exec(argv)` would silently broaden.
- **`release_claim` preserves `current_holder_workflow_id`** even after clearing `current_command_id` — audit/reconciliation lookups still find which workflow last touched the workspace.
- **Workspace-context callback bridges `core → domain`** without violating layer order: `domain/reviewer` registers a concrete `WorkflowContextProvider` at boot; `ProvisionWorkspace` reads it to fetch ticket context. See [`domain/reviewer/__init__.py`](domain_reviewer.md).
- **`get_workflow_context_provider()` is non-Optional** — raises `RuntimeError` when no provider is installed. A missing provider is a boot-time wiring bug, not a runtime option. `assert_workflow_context_provider()` is called from `web.py` / `worker.py` after `domain/reviewer` import so the crash happens at startup, not mid-flow. Test isolation via the `workflow_context_provider_isolation` fixture in `app/testing/isolation`.
- **Recovery-policy registration is explicit** — `register_workspace_recovery_policies()` (in `dispatch.py`) is called from `web.py` / `worker.py` startup, not at import time. This ensures every process that dispatches workflows registers the policy explicitly. Tests that need the policy call the function directly or use the `recovery_policies_isolation` fixture.
- **`WorkspaceAgentReportSink` is registered at import time** (`workspace/__init__.py`). agent_gateway's service functions call the sink for workspace-state reads/writes; the sink implementation lives in `app/core/workspace/agent_report.py`. The stale-claim guard (kind mismatch or unknown workspace) returns `accepted=False`; agent_gateway maps that to `StaleClaimError` / 410 Gone.

## Gotchas

- **Admin HTTP endpoints are unauthenticated.** Gate behind an authenticated proxy before exposing to untrusted networks.
- **`ResourceCaps` / `NetworkPolicy` are advisory** — the `in_memory` plugin ignores them; values exist to stabilise the interface for future plugins.
- **Failsafe-5 (disk sweep) is in the Go agent** (`apps/agent/internal/supervisor`), not here.
- **Never hand-edit `tach.toml`** — run `apps/backend/bin/sync_modules` after interface changes.

## Vocabulary

- **WorkspaceProvider** — dumb actuator (provision + run + destroy + health-check); no policy.
- **plugin_state** — opaque dict returned by `provision()`, persisted by this module, never exposed to consumers (e.g. `{"working_dir": "..."}` for in-process).
- **Reaper** — background loop enforcing TTL expiry, idle-timeout, agent-loss detection, and destroy retries.
- **Recovery-policy registration** — `register_workspace_recovery_policies()` registers `auth_expired → RefreshWorkspaceAuth` into [`core/workflow`](core_workflow.md)'s recovery-policy registry. Called explicitly from `web.py` / `worker.py` startup after workspace import. The registry itself lives in `core/workflow/recovery.py`.

## Data owned

- `workspaces` — `(id, org_id, provider_id, spec jsonb, plugin_state jsonb, status, provider, current_command_id, current_holder_workflow_id, max_idle_seconds, created_at, activated_at, expires_at, destroyed_at, destroy_attempts, last_destroy_attempt_at, last_destroy_error)`. Indexes: `(status, expires_at)`, `(org_id, created_at)`, `current_holder_workflow_id`, `org_id`.

## Routes

- `GET /api/workspaces/connection_status` — `ORG_SETTINGS_READ` (Admin+). Aggregated heartbeat state for the current org. Returns `{state, pod_count, latest_heartbeat_at}`. State values: `connected`, `lost`, `not_configured`. Implemented in `app/core/workspace/web.py`; delegates to [`core/agent_gateway`](core_agent_gateway.md) `connection_status_for_org`.

## How it's tested

`app/core/workspace/test/test_dispatch.py` covers `try_claim` / `release_claim` contention. `app/core/workspace/test/test_provider_fail_fast.py` covers the fail-fast contract: `get_workflow_context_provider()` raises when unbound; `assert_workflow_context_provider()` raises / passes correctly. Lifecycle coverage (provision → active → close → expired → destroy → destroyed; retry increment; `destroy_failed` after 3 attempts; `startup_recovery`) lives in reviewer integration tests and the workspace plugin's own tests. `app/core/workspace/test/test_connection_status_endpoint.py` covers the HTTP route: auth enforcement (401, 403) and the `not_configured` happy path. Recovery-policy tests live in `app/core/workflow/test/test_recovery_registry.py` (registry lives in [`core/workflow`](core_workflow.md)). `app/core/workspace/test/test_agent_report.py` covers the sink implementation: kind→status map, stale-claim guard, heartbeat reconciliation, and claim resolution.

Cross-module tests that need a workspace row without the full provision flow use `seed_workspace` from `app.testing.seed`.
