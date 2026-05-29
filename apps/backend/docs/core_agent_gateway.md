# core/agent_gateway

> Wire protocol between the customer-deployed WorkspaceAgent and the yaaos control plane.

## Scope

- **Owns:** inbound WorkspaceAgent surface — identity exchange, long-poll command claim, heartbeat/inventory, AgentEvent + WorkspaceEvent ingestion. `workspace_agents` and `bearer_tokens` tables. `WorkspaceAgentReportSink` Protocol + single-slot registry.
- **Does not own:** workspace lifecycle (owned by [`core/workspace`](core_workspace.md)); workflow routing (owned by [`core/workflow`](core_workflow.md)). Never imports `core/workspace` directly — workspace-state access goes through the registered sink.
- **Emits:** terminal AgentEvents → `workflow.handle_agent_event` enqueued via outbox (owned by [`core/tasks`](core_tasks.md)); progress events → `publish_workspace_activity` in [`core/sse`](core_sse.md).

## Why / invariants

- **Terminal AgentEvent enqueue is in the same transaction as the workspace mirror update** — prevents a workflow from missing its terminal event on crash between the two writes.
- **Stale-claim guard (410)** — events whose `command_id` is not in any workspace's `current_command_id` are rejected by the sink; the endpoint maps `accepted=False` to 410 Gone.
- **`WorkspaceAgentReportSink` IoC seam** — `core/workspace` implements the Protocol and registers at its own import time (`workspace/__init__.py`). agent_gateway's service functions call the registered sink for all workspace-state reads/writes; the `agent_gateway → workspace` import edge does not exist. Canonical direction: workspace → agent_gateway. Both single-slot registries (`register_report_sink`, `register_org_arn_lookup`) are idempotent for the same value but raise on a conflicting re-registration, so a double-wiring bug surfaces at boot rather than silently swapping the singleton; tests swap stubs via `clear_report_sink` / `_reset_org_arn_lookup_for_tests` first.
- **`OrgArnLookup` IoC seam** — `/identity/exchange` needs to resolve a canonical IAM ARN to an org id + aws_region, but `core` cannot import `domain`. `org_arn_lookup.py` declares `OrgArnRef` (a frozen dataclass) + `register_org_arn_lookup` / `lookup_org_by_arn`. `domain/orgs` registers its implementation at import time; the endpoint calls `lookup_org_by_arn` without any `core → domain` edge.
- **`org_context` wrap on every actor-resolving endpoint** — heartbeat, claim, workspace-events, command-events, and the activity WebSocket (entire connection lifetime). Excluded: `/identity/exchange` (bootstraps the bearer; no agent identity yet).
- **ARN canonicalization** — `assumed-role/ROLE/SESSION` → `iam::ACCT:role/ROLE`, lowercased. IAM role names are case-insensitive in AWS; lowering both sides avoids mismatches without losing uniqueness.
- **`SubscriberRegistry` is process-local.** On WebSocket reconnect it replays `subscribe` for every active route so the agent's rebuilt SubscriptionSet picks up where the old connection left off.
- **No activity flows from agent → SPA when nobody's watching** — the `SubscriberRegistry` only sends `subscribe` on `0 → 1` subscriber-count transitions.

## Gotchas

- **`clear_queues()`** must be called between test runs to reset in-memory dispatch state (per-agent FIFO + `asyncio.Condition`).
- **Replay-LRU window is 10 min** — clock skew > 5 min on the agent side will produce `clock_skew` rejections.
- Bearer plaintext is returned exactly once from `bearers.issue` and never persisted; `verify` returns `None` for every failure (no oracle).

## Vocabulary

- **AgentCommand** — discriminated union: `CreateWorkspace | WriteFiles | RefreshWorkspaceAuth | InvokeClaudeCode | CleanupWorkspace`.
- **AgentEvent** — `progress` (non-terminal) or `completed_{success|failure|skipped}` (terminal).
- **WorkspaceEvent** — `created | ready | exited | destroyed | failed`.
- **AgentRef** — `agent_id` + `agent_pod_id`; returned by `pick_agent_for_org` (least-loaded reachable pod).
- **BearerContext** — resolved identity from a verified bearer: `bearer_id`, `agent_id`, `org_id`.

## Data owned

- `workspace_agents` — per-pod identity rows; one per `(org_id, agent_pod_id)`.
- `bearer_tokens` — `(token_hash, issued_at, expires_at, revoked_at, revoked_reason, last_seen_at, source_ip)`. Revocation cascades from settings actions (`arn_change`, `mode_switch`, `disconnect`, `manual_rotate`) and failsafe-6 (`agent_loss`).

## How it's tested

`test/test_service.py` covers: per-agent FIFO independence; long-poll wakes on enqueue and times out cleanly; heartbeat reports unknown workspaces; terminal event enqueues `workflow.handle_agent_event`; progress events publish to the workspace-activity channel but do NOT enqueue; stale `command_id` raises `StaleClaimError`; `pick_agent_for_org` returns least-loaded `AgentRef` or `None`; `has_any_reachable_agent` respects the 90s cutoff.

`test/test_report_sink_delegation.py` covers sink delegation: heartbeat reconciliation via stub sink; workspace-event dispatch and rejection via stub sink; stale-claim guard raises `StaleClaimError` on `accepted=False` outcome.

`test/test_activity_publish_service.py` covers the WS `activity_batch` path delivering events to `subscribe_workspace_activity`.
