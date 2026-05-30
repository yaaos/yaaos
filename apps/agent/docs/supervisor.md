# supervisor

> Coordinates identity exchange, claim/heartbeat/sweep loops, workspace command routing, and goroutine lifecycle for the `agent supervisor` subcommand.

## Scope

- **Owns:** identity exchange (STS → bearer), N concurrent claim-loop workers, heartbeat loop, per-command routing to the pool, activity-WebSocket management, bearer-refresh loop, and disk-sweep loop.
- **Does not own:** workspace subprocess execution (that's `internal/workspace`), wire type definitions (`internal/protocol`), or command encoding (`internal/command`).
- **Receives:** raw `[]byte` from `protocol.Client.ClaimCommand`; hands to `command.Decode`.
- **Emits:** typed `protocol.AgentEvent` to `protocol.Client.PostCommandEvent` after each dispatch.
- **Hands to:** `Pool.Dispatch` for workspace commands; `AgentCommand.Execute(s)` for supervisor-resident commands.

## Why / invariants

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
- **Claim request carries lifecycle + active_workspace_ids** — `buildClaimRequest()` reads the config pointer (lifecycle) and `pool.ActiveIDs()` (active workspace set) for every claim poll. The backend filters which commands are eligible based on this.
- **OTLP exporter late-binds on first ConfigUpdate** — `observability.BindExporter` is called inside `ApplyConfig`; it installs the real OTLP/HTTP trace/metric/log providers against the config's endpoint. No-op when `OTLPEndpoint` is empty or the providers are already installed (env-var startup path or a prior ConfigUpdate). See [observability.md](observability.md).
- **Dedup cache guards against re-execution** — `routeCommand` checks an in-memory bounded LRU (1024 entries, `command_id → terminal AgentEvent`) before dispatch. A hit skips the workspace subprocess entirely and replays the cached event through the terminal-event retry loop. The cache entry is written before the first POST so re-delivery during an in-flight POST also hits the cache. The cache is cleared on pod restart (at-least-once; crash-loss accepted).
- **Terminal-event retry loop in `postTerminalEvent`** — retries `PostCommandEvent` with a short backoff ramp (1s/2s/5s/10s/30s). Stops on success or `ErrStaleClaim` (410 Gone). Progress events bypass this and remain best-effort single-shot. The `eventPostBackoff` field is separate from connection-surface backoffs so event-post retries don't interfere with claim or heartbeat timing.

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
