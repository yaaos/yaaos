# core/agent_gateway

> Wire protocol between the customer-deployed WorkspaceAgent and the yaaos control plane.

## Purpose

Owns the inbound surface that WorkspaceAgents talk to: long-poll command claim, heartbeat with workspace inventory, terminal AgentEvent ingestion, workspace-state events, identity exchange. The only module that touches `core/workflow.HANDLE_AGENT_EVENT` from the wire side — terminal events resolve the event-to-workflow lookup chain (`command_id → workspaces → current_holder_workflow_id`) and enqueue `workflow.handle_agent_event` via the outbox in the same transaction as the workspace mirror update. The bidirectional activity WebSocket lives alongside but is intentionally a separate channel.

## Public interface

Wire types (mirror [`apps/backend/openapi/agent-api.yaml`](../openapi/agent-api.yaml)) + service entry points. See `app/core/agent_gateway/__init__.py`.

- `AgentCommand` (discriminated union of the five command kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`), `AgentEvent`, `WorkspaceEvent`, `HeartbeatRequest/Response`, `IdentityExchangeRequest/Response`, `ClaimRequest`.
- `enqueue_command(agent_id, command)` — push an AgentCommand onto the agent's FIFO; wakes any blocked long-poll.
- `claim_next(agent_id, *, wait_seconds)` — long-poll consume; returns `None` on timeout.
- `record_heartbeat(agent_id, request, *, session)` — bumps liveness + computes `forgotten_workspaces`.
- `record_agent_event(event, *, session)` — applies the stale-claim guard; on terminal events, enqueues `workflow.handle_agent_event` in the outbox. On progress events, republishes to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md) so the SPA's SSE live-tail picks them up.
- `record_workspace_event(event, *, session)` — updates the workspace mirror; same stale-claim guard.
- `pick_agent_for_org(org_id, *, session)` — returns an `AgentRef` (least-loaded reachable pod for the org) or `None` when no pod is reachable. Load is in-process queue depth; ties break on most-recent heartbeat.
- `has_any_reachable_agent(*, session)` — returns `True` when any pod heartbeated within the last 90 s; used for global health checks without exposing the Row.
- `clear_queues()` — drop every in-memory queue and condition. Called by tests between runs.
- `AgentRef` — value object returned by `pick_agent_for_org`: `agent_id` (row PK) + `agent_pod_id` (pod identity used for command dispatch).
- Errors: `GatewayError`, `StaleClaimError` (→ 410), `UnauthorizedError` (→ 401).

HTTP routes mounted under `/api/v1/` (architecture's `/v1/` namespace nested under the project's `/api/` convention):

| Method + Path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/identity/exchange` | public | SigV4-signed STS → 24h bearer. Replays the signed request against AWS STS, canonicalizes assumed-role ARNs to role ARNs, looks up the org by `registered_iam_arn`, cross-checks the signed URL's region against `aws_region`. Rate-limited per source IP (10/min) and per `agent_pod_id` (100/hr). Issues a real bearer via `core/agent_gateway/bearers`. |
| `POST /api/v1/agents/{id}/heartbeat` | bearer | Liveness + workspace inventory → reconciliation response. |
| `POST /api/v1/agents/{id}/commands/claim` | bearer | Long-poll (up to `wait_seconds`, capped at 55s). 200 with one command or 204. |
| `POST /api/v1/workspaces/{id}/events` | bearer | Workspace state transitions. Stale-claim → 410. |
| `POST /api/v1/commands/{id}/events` | bearer | AgentCommand events. Terminal events advance the workflow. Stale-claim → 410. |

## Module architecture

### Entities

- **AgentCommand** — wire-layer command. Discriminated by `kind`. Carries `command_id`, `workspace_id`, `traceparent`, plus kind-specific payload.
- **AgentEvent** — non-terminal `progress` or terminal `completed_{success|failure|skipped}`. Carries `command_id`, `outcome_label`, `outputs`, `attempt`, `traceparent`.
- **WorkspaceEvent** — `created | ready | exited | destroyed | failed`. Scoped to the `command_id` that drove the transition.

### Key value objects

- `IdentityExchangeRequest/Response` — bearer issuance handshake.
- `HeartbeatRequest/Response` — liveness + inventory; response carries `forgotten_workspaces` so the agent cleans up control-plane orphans.
- `ClaimRequest` — `wait_seconds` long-poll horizon.
- `BearerContext` — resolved identity from a verified bearer. `bearer_id`, `agent_id`, `org_id`.
- `VerifiedIdentity` — STS verifier result. `canonical_arn` (IAM role), `raw_arn` (as STS returned it), `region`.
- `FailureCategory` — typed STS verifier failure: `parse_error`, `endpoint_disallowed`, `body_mismatch`, `replay_detected`, `aws_rejected`, `clock_skew`.

### Core user flows

1. **Identity exchange.** Agent pod boots, sigv4-signs an STS `GetCallerIdentity` request with its IAM credentials (IRSA / EC2 instance profile / ECS task role), posts the signed envelope + `agent_pod_id` to `/identity/exchange`. Control plane:
   - Parses the envelope; rejects non-STS URLs (`endpoint_disallowed`), wrong body (`body_mismatch`).
   - Replay-LRU dedupe on `Authorization || X-Amz-Date` (10 min window, rejects `replay_detected`).
   - HTTPS POST to AWS STS via a TLS-1.3-pinned shared httpx client; on `RequestExpired` returns `clock_skew`, other non-2xx returns `aws_rejected`.
   - Canonicalizes the returned ARN (`arn:aws:sts::ACCT:assumed-role/ROLE/SESSION` → `arn:aws:iam::ACCT:role/ROLE`) and lowercases it so the customer's registered IAM role ARN matches. IAM names are unique-case-insensitive in AWS, so lowering both sides is safe and removes a foot-gun.
   - Looks up the org by `orgs.registered_iam_arn` (UNIQUE) and checks the signed URL's region against `orgs.aws_region`. Registration enforces a strict no-path role-ARN shape in [`domain/orgs/org_settings_web`](domain_orgs.md) so two orgs can't collide on the same canonical (paths are stripped by AWS's `assumed-role` form).
   - Issues a 24h bearer via `bearers.issue` (sha256 hash stored, plaintext returned once), upserts the `workspace_agents` row, captures source IP.
   - Returns `{bearer, expires_at, agent_id}`.
2. **Long-poll command claim.** Free agent slots each post `claim` with `wait_seconds=30`. Backend's per-agent FIFO returns the head, or 204 on timeout. Internally an `asyncio.Condition` per agent wakes the poll the moment `enqueue_command(agent_id, cmd)` runs.
3. **Heartbeat reconciliation.** Every ~30s the agent posts its workspace inventory. Backend bumps liveness and reads back `forgotten_workspaces` — anything the agent reports that's destroyed or unknown control-plane-side.
4. **Event ingestion.** Workspace-state and AgentCommand events flow through their respective endpoints. The single-flight claim columns set by [`core/workspace.try_claim`](core_workspace.md) gate every event: `command_id` not in any workspace's `current_command_id` → 410. Terminal AgentEvents enqueue `workflow.handle_agent_event` via the outbox in the same transaction; progress events republish to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md) — the same channel the WebSocket activity-batch path writes to, so HTTP-posted progress and WS-batched activity converge on one subscriber surface.

### Activity WebSocket

The bidirectional `WSS /api/v1/agents/{id}/activity` carries demand-pull activity:

- Auth on upgrade — `Bearer <token>` (placeholder verifier). Missing / empty → close with `4401`.
- **Agent → backend** `activity_batch` messages publish each event to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md).
- **Backend → agent** `subscribe` / `unsubscribe` messages, dispatched by `SubscriberRegistry` on `0 → 1` / `1 → 0` UI-subscriber-count transitions. No activity flows when nobody's watching.
- **WS reconnect**: `SubscriberRegistry.register_sender` replays a `subscribe` for every active route whose `agent_id` matches the reconnecting agent so the agent's rebuilt SubscriptionSet picks up where the old connection left off.
- `SubscriberRegistry` is process-local.

## Data owned

- `workspace_agents` — per-pod identity rows (one per `(org_id, agent_pod_id)`). Bumped on every successful identity exchange + heartbeat.
- `bearer_tokens` — issued-bearer ledger. `token_hash` (sha256), `issued_at`, `expires_at`, `revoked_at`, `revoked_reason`, `last_seen_at`, `source_ip`. Plaintext is returned exactly once from `bearers.issue` and never persisted. `verify` returns `None` for every failure (no oracle).

Reads `workspaces` ([`core/workspace`](core_workspace.md)) to resolve the lookup chain; writes to the outbox owned by [`core/tasks`](core_tasks.md). Bearer revocation cascades from Workspace settings actions (`arn_change` / `mode_switch` / `disconnect` / `manual_rotate`) and from failsafe-6 (`agent_loss`).

## How it's tested

`test/test_service.py` covers: per-agent FIFO independence; immediate return when empty; long-poll wakes on enqueue; long-poll times out cleanly; heartbeat reconciliation reports unknown workspaces; terminal AgentEvent enqueues `workflow.handle_agent_event` with the resolved workflow id; progress events do NOT enqueue but DO publish to `activity:{workflow_execution_id}` for SSE live-tail; stale `command_id` raises `StaleClaimError`; workspace-state `ready` event transitions row to `active`; stale workspace event raises; `pick_agent_for_org` returns `AgentRef` for a fresh pod, `None` for no agents / stale heartbeat / non-reachable state, and prefers the least-loaded pod; `has_any_reachable_agent` returns `True`/`False` based on 90-s cutoff. Tests call `clear_queues()` between runs to reset the in-memory dispatch state.

Endpoint coverage rides on the service tests + the bearer-dep guards.
