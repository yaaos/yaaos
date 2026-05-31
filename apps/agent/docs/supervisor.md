# supervisor

> Coordinates identity exchange, claim/heartbeat/sweep loops, workspace command routing, and goroutine lifecycle for the `agent supervisor` subcommand.

## Scope

- **Owns:** identity exchange (STS → bearer), N concurrent claim-loop workers, heartbeat loop, per-command routing to the pool, activity-WebSocket management, bearer-refresh loop, and disk-sweep loop.
- **Does not own:** workspace subprocess execution (that's `internal/workspace`), wire type definitions (`internal/protocol`), or command encoding (`internal/command`).
- **Receives:** raw `[]byte` from `protocol.Client.ClaimCommand`; hands to `command.Decode`.
- **Emits:** typed `protocol.AgentEvent` to `protocol.Client.PostCommandEvent` after each dispatch.
- **Hands to:** `Pool.Dispatch` for workspace commands; `AgentCommand.Execute(s)` for supervisor-resident commands.

## Why / invariants

- **Supervisor owns the identity exchange transport.** `identity.Provider.SignClaim` returns the signed claim; the supervisor builds the `IdentityExchangeRequest` (including `AgentMetadata`), POSTs it, and stamps `Credentials` from the response. Provider does not contact the backend.
- **`refreshLead` is 5 minutes.** Bearers have a 1-hour TTL; the 5-minute lead gives the supervisor several retry attempts before the bearer expires under transient STS failures.
- **Single pool mutex guards the registry.** All state reads/writes to workspace records go through `Pool`'s named mutators. No free-form field access.
- **Lifecycle is derived from `config.Load() == nil`** — no separate enum. Nil means unconfigured; non-nil means configured. A restart clears the pointer and re-enters unconfigured (safe by default).
- **Unconfigured gate in `routeWorkspaceCmd`** — all `WorkspaceCommand` dispatch paths return `completed_failure "agent unconfigured"` until the first `ConfigUpdateCommand` is applied. The claim loop runs regardless; claim requests carry `lifecycle` so the backend gates which commands to deliver.
- **`max_workspaces` cap + at-most-one-runner are atomic in `Pool.reserveActiveSlot`** — a single `Pool.mu` critical section does the existence check, the cap check, and the placeholder insert. Two concurrent same-id `CreateWorkspace` dispatches cannot both reserve (the loser gets `errSlotTaken` and never spawns, so exactly one runner exists); concurrent creates across ids cannot both pass a stale count. The supervisor reads `config.MaxWorkspaces` and passes it to `Pool.Dispatch`.
- **No command ever observes a nil-runner record** — a reserved slot's runner is nil until the spawn completes and `assignRunner` fills it. Dispatch gates every Send through `lookupSendable`, which requires Active + non-nil runner, so a placeholder is never sent to.
- **Heartbeat reads `pool.Snapshot()`** — a pure projection of the registry state. It reports every registered workspace (Active/Defunct/Orphaned), not just in-flight ones.
- **Disk sweep reads `pool.KnownIDs()`** — covers Active, Defunct, and Orphaned. A Defunct record keeps its id in KnownIDs so the sweep never removes a directory the registry knows about.
- **Orphan startup scan calls `pool.seedOrphan(id, path)`** per found directory, so the first heartbeat after a pod restart correctly reports leftover workspaces as `status="unknown"`.
- **Forgotten-workspace janitor reads `pool.Paths()`** — includes every record that has a path set. After `os.RemoveAll` succeeds, calls `pool.remove(id)` to drop the record.
- **Busy-ness is tracked inside `Pool.Dispatch`** — `setCommandID`/`clearCommandID` toggle `current_command_id` around Send. A completed command's workspace stays `status="running"` until the backend explicitly reaps it.
- **Claim request carries capacity-pull fields** — `buildClaimRequest()` derives `lifecycle` from the config pointer; `new_workspaces = max_workspaces − active count`; `workspace_ids = pool.IdleIDs()` (Active workspaces with no in-flight command). The backend selects up to `new_workspaces` unassigned `CreateWorkspace` rows and one pending row per named `workspace_id`.
- **`received` event cancels the lease** — after decoding a claimed command, the claim loop posts `kind=received` before dispatch. This flips the backend's `agent_commands` row from `claimed → delivered`, cancelling the 30-second lease requeue. Best-effort.
- **Bootstrap-retry asymmetry** — `stsBackoff` (identity exchange) has a 1-hour max-elapsed deadline via `backoff.NewWithDeadline`. An unbootstrapped pod that cannot reach the control plane for 1 hour calls `os.Exit(1)` so the container orchestrator can restart it. Once bootstrapped, the bearer-renewal loop uses `bearerRefreshLoop` (indefinite retries) — a transient STS blip must not kill a running pod that holds active workspaces. See [`apps/agent/internal/backoff`](../internal/backoff/schedule.go) for `NewWithDeadline`.
- **OTLP exporter late-binds on first ConfigUpdate** — `observability.BindExporter` is called inside `ApplyConfig`; it installs the real OTLP/HTTP trace/metric/log providers against the config's endpoint. No-op when `OTLPEndpoint` is empty or the providers are already installed (env-var startup path or a prior ConfigUpdate). See [observability.md](observability.md).
- **Dedup cache guards against re-execution** — `routeCommand` checks an in-memory bounded LRU (1024 entries, `command_id → terminal AgentEvent`) before dispatch. A hit skips the workspace subprocess entirely and replays the cached event through the terminal-event retry loop. The cache entry is written before the first POST so re-delivery during an in-flight POST also hits the cache. The cache is cleared on pod restart (at-least-once; crash-loss accepted).
- **Terminal-event retry loop in `postTerminalEvent`** — retries `PostCommandEvent` with a short backoff ramp (1s/2s/5s/10s/30s). Stops on success or `ErrStaleClaim` (410 Gone). Progress events bypass this and remain best-effort single-shot. The `eventPostBackoff` field is separate from connection-surface backoffs so event-post retries don't interfere with claim or heartbeat timing.
- **Graceful-shutdown "going away" signal.** After all goroutines exit and the pool is reaped, `Run` calls `sendGoingAway()` which POSTs `DELETE /api/v1/agent/identity` with a 5-second deadline. The control plane eagerly marks the agent offline, revokes the bearer, and expires held workspaces. Best-effort: errors are logged but never cause a non-zero exit. The backend's liveness sweeper handles ungraceful exits via the decay path (heartbeat timeout → offline).
- **Shutdown-vs-in-flight contract** — when the root `ctx` is cancelled (supervisor shutdown) while `Pool.Dispatch` has a `Send` in-flight, the per-command `sendCtx` also cancels; `Send` returns `ctx.Err()`; the pool emits `completed_failure` with `failure_reason` prefixed `"runner:"`. No in-flight command is silently dropped — the caller always receives a terminal event. See `pool.go:failureEvent`.
- **Concurrent invariants each have a `-race` test** — see [patterns.md § Testing](patterns.md) principle 7. Covered: registry cap, same-id atomicity, per-surface backoff independence, dedup LRU consistency, and `execRunner.Close` idempotency.

## Testing

- Timing-sensitive supervisor tests run in `testing/synctest` bubbles where feasible. The activity-WS integration test (`supervisor_activity_ws_test.go`) uses a real `httptest.Server` WS connection; its subscribe-propagation poll cannot be bubbled because the WS read goroutine blocks on OS network I/O. See [patterns.md § Testing](patterns.md) principle 6.

## Gotchas

- `CloseAll` on shutdown: pool reaps all runners; already-nil runners (Orphaned records) are skipped.
- The activity-WS conductor is torn down before `CloseAll` to avoid a slow-flush race on ctx cancel.
- Bearer refresh loop runs independently on its own backoff — a failed STS exchange does not affect the heartbeat or claim schedules.

## Vocabulary

- **Orphan** — a workspace directory found on disk at startup from a prior run. Seeded into the registry as Orphaned; the backend signals cleanup via `forgotten_workspaces`.
- **Forgotten** — a workspace the backend no longer tracks; named in `HeartbeatResponse.forgotten_workspaces`. The janitor removes its directory and drops the registry record.
- **Defunct** — a workspace whose runner exited unexpectedly (child-exit). Stays in the registry (and thus in KnownIDs) until the backend reaps it. See [workspace_lifecycle.md](workspace_lifecycle.md).

## Entry points

- `apps/agent/internal/supervisor/supervisor.go` — `Supervisor` struct, `New`, `Run`, goroutine wiring.
- `apps/agent/internal/supervisor/pool.go` — registry, state machine, `Dispatch`.
- `apps/agent/internal/supervisor/reconciliation.go` — startup scan, disk sweep, forgotten-workspace janitor.
