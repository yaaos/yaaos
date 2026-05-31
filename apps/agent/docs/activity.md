# internal/activity

> Activity WebSocket protocol handler: subscription tracking, workspace-to-workflow mapping, event batching, and flush delivery.

## Scope

- **Owns:** `SubscriptionSet` (which workspace_ids are subscribed), `WorkspaceMapping` (workspace_id → workflow_execution_id cache), `Batcher` (250 ms flush timer per subscribed key), `Conductor` (composes the three into the single consumer-facing API), `DecodeInbound`/`EncodeBatch` (WS frame codec).
- **Does not own:** the WebSocket dial/auth/reconnect strategy (owned by `internal/supervisor`), command dispatch (`internal/command`), or HTTP event posting (`internal/protocol.Client`).
- **Receives:** raw WS bytes from the supervisor's read loop (subscribe/unsubscribe frames) and `AgentEvent` values from dispatched workspace commands.
- **Emits:** encoded `activity_batch` JSON frames via the injected `SendFunc`; send errors are logged and dropped — the supervisor owns retry and reconnect.
- **Hands to:** `SendFunc` (caller-supplied callback); `internal/protocol.AgentEvent` (imported from the wire-DTO leaf).

## Why / invariants

- **`Conductor` is the single consumer API.** The supervisor calls `HandleInbound` on WS reads and `Publish` for each event from a workspace command. All other types (`SubscriptionSet`, `WorkspaceMapping`, `Batcher`) are internal building blocks.
- **Mapping precedes subscription.** `HandleInbound` sets the `workflow_execution_id` mapping before adding the workspace_id to the `SubscriptionSet`. A concurrent `Publish` that races between the two would find the workspace subscribed but unmapped — ordering prevents that.
- **Unsubscribed workspaces drop at the Batcher's gate.** `Publish` never blocks; events for unsubscribed workspaces are discarded immediately.
- **No mapping → drop with a `Warn` log.** If a subscribed workspace has no `workflow_execution_id` mapping at flush time, the batch is dropped and logged. This is defensive; it should not happen given correct subscribe payloads from the backend.
- **Logger is injected, not global.** `NewConductorWithLogger` accepts a `Logger` interface; the supervisor passes its own logger so activity logs carry the same `org_id`/`agent_id` dimensions as the rest of the supervisor's output. `NewConductor` installs a silent no-op logger — used in tests that don't need log assertions.
- **`SubscriptionSet` and `WorkspaceMapping` each have independent locks.** They are safe for concurrent reads from the Batcher flush goroutine while `HandleInbound` mutates them.

## Gotchas

- `InboundKind` has exactly two values (`subscribe`/`unsubscribe`). The `exhaustive` linter (see `apps/agent/.golangci.yml`) guards `conductor.HandleInbound`'s switch — both cases must remain covered.
- The 250 ms flush interval is a Batcher constant. Changing it affects activity latency for all workspace subscriptions simultaneously.
- `SendFunc` is invoked synchronously inside the Batcher's flush goroutine. A slow or blocking send stalls the next flush tick.

## Vocabulary

- **SubscriptionSet** — the set of workspace_ids the backend has subscribed; gates `Publish`.
- **WorkspaceMapping** — cache of workspace_id → workflow_execution_id; populated on subscribe, used at flush time to write the outbound `activity_batch` frame.
- **Batcher** — buffers events per workspace_id and flushes every 250 ms via `FlushFunc`.
- **Conductor** — the assembled facade the supervisor calls; owns `Start`/`Stop` lifecycle and the `HandleInbound`/`Publish` API.
- **SendFunc** — caller-supplied callback that writes one encoded frame onto the WebSocket.

## Testing

- Timing tests (`Batcher` flush interval, `Conductor` flush-to-wire) run inside `testing/synctest` bubbles — fake time advances deterministically; no wall-clock polling. See [patterns.md § Testing](patterns.md) principle 6.
- The `wsclient_test.go` transport test (`TestRunInbound_FeedsConductor`) uses a real `httptest.Server` WS connection; its subscribe-propagation poll loop cannot be bubbled because the WS read goroutine blocks on OS network I/O.

## Entry points

- `apps/agent/internal/activity/conductor.go` — `Conductor`, `NewConductor`, `NewConductorWithLogger`, `HandleInbound`, `Publish`.
- `apps/agent/internal/activity/protocol.go` — `InboundKind`, `InboundMessage`, `DecodeInbound`, `EncodeBatch`.
- `apps/agent/internal/activity/batcher.go` — `Batcher`, flush loop.
- `apps/agent/internal/activity/subscription.go` — `SubscriptionSet`.
- `apps/agent/internal/activity/mapping.go` — `WorkspaceMapping`.
