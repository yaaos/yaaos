# core/agent_gateway

> Wire protocol between the customer-deployed WorkspaceAgent and the yaaos control plane.

## Purpose

Owns the inbound surface that WorkspaceAgents talk to: long-poll command claim, heartbeat with workspace inventory, terminal AgentEvent ingestion, workspace-state events, identity exchange. The only module that touches `core/workflow.HANDLE_AGENT_EVENT` from the wire side — terminal events resolve the event-to-workflow lookup chain (`command_id → workspaces → current_holder_workflow_id`) and enqueue `workflow.handle_agent_event` via the outbox in the same transaction as the workspace mirror update. The Phase 8b WebSocket activity stream lives alongside but is intentionally a separate channel.

## Public interface

Wire types (mirror [`apps/backend/openapi/agent-api.yaml`](../openapi/agent-api.yaml)) + service entry points. See `app/core/agent_gateway/__init__.py`.

- `AgentCommand` (discriminated union of the five command kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`), `AgentEvent`, `WorkspaceEvent`, `HeartbeatRequest/Response`, `IdentityExchangeRequest/Response`, `ClaimRequest`.
- `enqueue_command(agent_id, command)` — push an AgentCommand onto the agent's FIFO; wakes any blocked long-poll.
- `claim_next(agent_id, *, wait_seconds)` — long-poll consume; returns `None` on timeout.
- `record_heartbeat(agent_id, request, *, session)` — bumps liveness + computes `forgotten_workspaces`.
- `record_agent_event(event, *, session)` — applies the stale-claim guard; on terminal events, enqueues `workflow.handle_agent_event` in the outbox. On progress events, republishes to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md) so the SPA's SSE live-tail picks them up.
- `record_workspace_event(event, *, session)` — updates the workspace mirror; same stale-claim guard.
- Errors: `GatewayError`, `StaleClaimError` (→ 410), `UnauthorizedError` (→ 401).

HTTP routes mounted under `/api/v1/` (architecture's `/v1/` namespace nested under the project's `/api/` convention):

| Method + Path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/identity/exchange` | public | SigV4-signed STS → short-lived bearer. **Phase 5 ships a placeholder verifier** that accepts any non-empty `signed_request`; Phase 7 wires the real STS replay. |
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

### Core user flows

1. **Identity exchange.** Agent pod boots, posts a SigV4-signed STS payload + its locally-generated `agent_pod_id`. Control plane verifies (Phase 7) and returns a bearer + the per-pod `agent_id`.
2. **Long-poll command claim.** Free agent slots each post `claim` with `wait_seconds=30`. Backend's per-agent FIFO returns the head, or 204 on timeout. Internally an `asyncio.Condition` per agent wakes the poll the moment `enqueue_command(agent_id, cmd)` runs.
3. **Heartbeat reconciliation.** Every ~30s the agent posts its workspace inventory. Backend bumps liveness and reads back `forgotten_workspaces` — anything the agent reports that's destroyed or unknown control-plane-side.
4. **Event ingestion.** Workspace-state and AgentCommand events flow through their respective endpoints. The single-flight claim columns set by [`core/workspace.try_claim`](core_workspace.md) gate every event: `command_id` not in any workspace's `current_command_id` → 410. Terminal AgentEvents enqueue `workflow.handle_agent_event` via the outbox in the same transaction; progress events republish to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md) — the same channel the Phase 8b WebSocket activity-batch path writes to, so HTTP-posted progress and WS-batched activity converge on one subscriber surface.

### Phase boundaries

- **Phase 5** shipped the wire shape: OpenAPI spec, Pydantic mirror, in-memory per-agent FIFO with long-poll, heartbeat reconciliation, terminal-event routing, stale-claim guard, placeholder bearer verifier.
- **Phase 7 (foundations)** added the `RemoteAgentWorkspaceProvider`, `workspace_agents` table + `ensure_agent_row()`, and the org-side `workspace_provider` + `registered_iam_arn` settings. Real STS-replay verifier in the Phase 7 follow-on.
- **Phase 8b (this commit)** shipped the bidirectional `WSS /api/v1/agents/{id}/activity`:
  - Auth on upgrade — placeholder `Bearer <token>` (Phase 7 verifier swaps in transparently). Missing / empty → close with `4401`.
  - **Agent → backend** `activity_batch` messages publish each event to `activity:{workflow_execution_id}` via [`core/sse_pubsub`](core_sse_pubsub.md).
  - **Backend → agent** `subscribe` / `unsubscribe` messages, dispatched by `SubscriberRegistry` on `0 → 1` / `1 → 0` UI-subscriber-count transitions. Demand-pull: no activity flows when nobody's watching.
  - **WS reconnect**: `SubscriberRegistry.register_sender` replays a `subscribe` for every active route whose `agent_id` matches the reconnecting agent so the agent's rebuilt SubscriptionSet picks up where the old connection left off. Without the replay, in-flight UIs would silently miss progress events until they detached + re-attached.
  - `SubscriberRegistry` is process-local; multi-instance variant rides on the same swap point as the Redis `core/sse_pubsub` backend.
- **Phase 8b follow-on** wires uvicorn `--ws-ping-interval=30 --ws-ping-timeout=10` (ALB idle-timeout survival), the per-workflow SSE endpoint that consumes `activity:{id}` channels, the WS reconnect handler that re-derives subscriptions, the `domain/coding_agent` trust-boundary audit (metadata only, no source content), and the in-memory provider's direct publish path that bypasses the WebSocket wire.

## Data owned

None directly. Reads `workspaces` ([`core/workspace`](core_workspace.md)) to resolve the lookup chain; writes via outbox ([`core/outbox`](core_outbox.md)). `workspace_agents` row writes land in Phase 7.

## How it's tested

`test/test_service.py` covers: per-agent FIFO independence; immediate return when empty; long-poll wakes on enqueue; long-poll times out cleanly; heartbeat reconciliation reports unknown workspaces; terminal AgentEvent enqueues `workflow.handle_agent_event` with the resolved workflow id; progress events do NOT enqueue but DO publish to `activity:{workflow_execution_id}` for SSE live-tail; stale `command_id` raises `StaleClaimError`; workspace-state `ready` event transitions row to `active`; stale workspace event raises.

Endpoint coverage rides on the service tests + the bearer-dep guards; full integration tests against the in-tree Go agent land in Phase 6.
