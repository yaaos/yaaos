# core/agent_gateway

> Wire protocol between the customer-deployed WorkspaceAgent and the yaaos control plane.

## Scope

- **Owns:** inbound WorkspaceAgent surface — identity exchange, long-poll command claim, heartbeat/inventory, AgentEvent + WorkspaceEvent ingestion. `workspace_agents` and `bearer_tokens` tables. `WorkspaceAgentReportSink` Protocol + single-slot registry.
- **Does not own:** workspace lifecycle (owned by [`core/workspace`](core_workspace.md)); workflow routing (owned by [`core/workflow`](core_workflow.md)). Never imports `core/workspace` directly — workspace-state access goes through the registered sink.
- **Emits:** terminal AgentEvents → `workflow.handle_agent_event` enqueued via outbox (owned by [`core/tasks`](core_tasks.md)); progress events → `publish_workspace_activity` in [`core/sse`](core_sse.md).

## Identity exchange — `POST /api/v1/agent/identity`

Vault AWS-auth pattern. The agent submits a sigv4-signed STS `GetCallerIdentity` as `payload`; the backend replays it, derives `instance_id` from the role-session-name, and issues a 1-hour bearer.

- **Audience binding** — `X-Yaaos-Audience` in the signed envelope must match the backend's `Host`. Mismatch → 401 `audience_mismatch`. Binds the signed request to the specific backend deployment.
- **`instance_id` derivation** — extracted from the role-session-name of the assumed-role ARN (`arn:aws:sts::ACCT:assumed-role/ROLE/SESSION` → `SESSION`). The agent never supplies `instance_id`.
- **Find-or-create keyed on `(org_id, instance_id)`.** The same ECS task restarting keeps the same row; each exchange updates `iam_arn`, `version`, and static OS metadata.
- **1-hour TTL.** Response includes `renewal_after` (5 min before `expires_at`) as the suggested re-exchange time.
- **Non-revoking rotation.** A second call issues a new bearer without revoking the old one. The agent atomically swaps the bearer after receiving the rotation response.
- **`issued_iam_arn` on bearer row.** Every `bearer_tokens` row records the canonical IAM ARN verified at issuance.
- **Host allowlist override** — `YAAOS_STS_HOST_OVERRIDE` allows an additional STS host (e.g. `mock-aws:4566`) only when `YAAOS_ENV` is non-prod. The process refuses to boot with both `YAAOS_ENV=prod` and the override set.

## Why / invariants

- **Terminal AgentEvent enqueue is in the same transaction as the workspace mirror update** — prevents a workflow from missing its terminal event on crash between the two writes.
- **Stale-claim guard (410)** — events whose `command_id` is not in any workspace's `current_command_id` are rejected by the sink; the endpoint maps `accepted=False` to 410 Gone.
- **`WorkspaceAgentReportSink` IoC seam** — `core/workspace` implements the Protocol and registers at its own import time (`workspace/__init__.py`). agent_gateway's service functions call the registered sink for all workspace-state reads/writes; the `agent_gateway → workspace` import edge does not exist. Canonical direction: workspace → agent_gateway. Both single-slot registries (`register_report_sink`, `register_org_arn_lookup`) are idempotent for the same value but raise on a conflicting re-registration, so a double-wiring bug surfaces at boot rather than silently swapping the singleton. Tests that need to swap stubs reach `clear_report_sink` directly from `app.core.agent_gateway.report_sink` (intra-module submodule import).
- **`OrgArnLookup` IoC seam** — `/api/v1/agent/identity` needs to resolve a canonical IAM ARN to an org id + aws_region, but `core` cannot import `domain`. `org_arn_lookup.py` declares `OrgArnRef` (a frozen dataclass) + `register_org_arn_lookup` / `lookup_org_by_arn`. `domain/orgs` registers its implementation at import time; the endpoint calls `lookup_org_by_arn` without any `core → domain` edge.
- **`org_context` wrap on every actor-resolving endpoint** — heartbeat, claim, workspace-events, command-events, and the activity WebSocket (entire connection lifetime). Excluded: `/api/v1/agent/identity` (bootstraps the bearer; no agent identity yet).
- **Per-agent identity check on `agent_id`-addressed endpoints** — `heartbeat`, `claim`, and the activity WebSocket bind a path `agent_id`; they additionally require `bearer.agent_id == path agent_id` (`_require_self` in `web.py`; WebSocket closes 4403, HTTP raises 403 `forbidden`). The `org_context` wrap blocks cross-org access; this closes the within-org IDOR where one pod's bearer addresses another pod's row/queue/channel.
- **Per-agent ownership check on workspace/command-event posts** — `post_workspace_event` / `post_command_event` bind `workspace_id` / `command_id`, which resolve to a workspace carrying an owning `agent_id` ([`core/workspace`](core_workspace.md) `WorkspaceRow.agent_id`, set at create-dispatch). The sink resolves the owner (`owning_agent_for_workspace` / `owning_agent_for_command`); when it isn't the bearer's agent, `_require_workspace_owner` raises 403 `forbidden` (same envelope as `_require_self`). A command that resolves to no workspace (e.g. an agent-scoped `ConfigUpdate`, which has no `workspace_id`) or a workspace with a NULL `agent_id` (in-memory/legacy) carries no ownership edge — authorization then falls back to the org scope plus the stale-claim guard, so a legitimate ConfigUpdate terminal event still reaches the existing 410-on-no-claim path rather than being 403'd.
- **`org_id` on the identity-exchange response** — the response carries `org_id` (the `workspace_agents.org_id` for the matched row). The agent pins both `org_id` and `agent_id` on first exchange and verifies they are unchanged on every bearer renewal; a mismatch triggers a fatal exit on the agent side.
- **ARN canonicalization** — `assumed-role/ROLE/SESSION` → `iam::ACCT:role/ROLE`, lowercased. IAM role names are case-insensitive in AWS; lowering both sides avoids mismatches without losing uniqueness.
- **`AgentQueues` is ContextVar-bound.** The active dispatch-queue registry is held in a ContextVar. `bind_agent_queues` is the production DI seam — called at startup in `app/web.py` and `app/worker.py`. The `agent_queues_isolation` autouse fixture in `app/testing/isolation` binds a fresh `AgentQueues()` per test so there is no shared per-agent FIFO state between tests.
- **`SubscriberRegistry` is ContextVar-bound.** Same pattern as `AgentQueues`. `bind_subscriber_registry` is the production DI seam; `subscriber_registry_isolation` autouse fixture resets per test. On WebSocket reconnect it replays `subscribe` for every active route so the agent's rebuilt SubscriptionSet picks up where the old connection left off.
- **No activity flows from agent → SPA when nobody's watching** — the `SubscriberRegistry` only sends `subscribe` on `0 → 1` subscriber-count transitions.
- **`seed_agent` lives in `app/testing/seed`.** The production `ensure_agent_row` API is what callers use; `seed_agent` is a test convenience wrapper that adds a random pod_id and optional heartbeat back-dating. Cross-module tests import it from `app.testing.seed`.

## Gotchas

- **Replay-LRU window is 10 min** — clock skew > 5 min on the agent side will produce `clock_skew` rejections.
- Bearer plaintext is returned exactly once from `bearers.issue` and never persisted; `verify` returns `None` for every failure (no oracle).

## Vocabulary

- **AgentCommand** — discriminated union: `CreateWorkspace | WriteFiles | RefreshWorkspaceAuth | InvokeClaudeCode | CleanupWorkspace`.
- **AgentEvent** — `progress` (non-terminal) or `completed_{success|failure|skipped}` (terminal).
- **WorkspaceEvent** — `created | ready | exited | destroyed | failed`.
- **AgentRef** — `agent_id` + `instance_id`; returned by `pick_agent_for_org` (least-loaded reachable pod). Queue operations (enqueue/claim/queue_depth) all key on `agent_id` (= `WorkspaceAgentRow.id`), the same key the `/agents/{agent_id}/...` routes bind.
- **BearerContext** — resolved identity from a verified bearer: `bearer_id`, `agent_id`, `org_id`.

## Data owned

- `workspace_agents` — per-pod identity rows; one per `(org_id, instance_id)`. Columns: `instance_id` (role-session-name from STS ARN), `iam_arn`, `version`, `os`, `cpu_count`, `memory_bytes`, `claimed_workspace_count`, `last_heartbeat_at`, `last_shutdown_at`, `state`.
- `bearer_tokens` — `(token_hash, issued_at, expires_at, revoked_at, revoked_reason, last_seen_at, source_ip, issued_iam_arn)`. Revocation cascades from settings actions (`arn_change`, `mode_switch`, `disconnect`, `manual_rotate`) org-wide via `revoke_all_for_org`, and from failsafe-6 (`agent_loss`) per-pod via `revoke_all_for_agent`.

## How it's tested

`test/test_service.py` covers: per-agent FIFO independence; long-poll wakes on enqueue and times out cleanly; heartbeat reports unknown workspaces; terminal event enqueues `workflow.handle_agent_event`; progress events publish to the workspace-activity channel but do NOT enqueue; stale `command_id` raises `StaleClaimError`; `pick_agent_for_org` returns least-loaded `AgentRef` or `None`; `has_any_reachable_agent` respects the 90s cutoff.

`test/test_identity_exchange.py` covers: happy-path bearer issuance (row persisted by `instance_id`, OS metadata stored, bearer returned with `instance_id` in response); bearer TTL is 1 hour; non-revoking rotation (second call issues new bearer, old stays valid); ARN mismatch → 403; region mismatch → 401; invalid signature → 401; empty payload → 401; unsupported kind → 401; audience mismatch → 401; response includes `org_id` and `instance_id`.

`test/test_queue_binding.py` covers ContextVar isolation for `AgentQueues` and `SubscriberRegistry`: fresh bind hides prior state; fail-fast `RuntimeError` fires before bind; `claim_next` drains from the bound registry.

`test/test_report_sink_delegation.py` covers sink delegation: heartbeat reconciliation via stub sink; workspace-event dispatch and rejection via stub sink; stale-claim guard raises `StaleClaimError` on `accepted=False` outcome.

`test/test_endpoint_authz_service.py` covers per-endpoint authz: `heartbeat` / `claim` reject a foreign path `agent_id` (403); `post_workspace_event` / `post_command_event` reject a foreign owning `agent_id` (403) and allow the owner (200); an agent-scoped command (no owning workspace, e.g. ConfigUpdate) is NOT 403'd — it falls through to the stale-claim 410.

`test/test_activity_publish_service.py` covers the WS `activity_batch` path delivering events to `subscribe_workspace_activity`.

Queue + registry isolation between tests is provided by the `agent_queues_isolation` and `subscriber_registry_isolation` autouse fixtures in `app/testing/isolation` — no explicit reset needed in tests. Seed an agent row via `app.testing.seed.seed_agent`.
