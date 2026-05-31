# apps/agent — internal architecture

> OS-process scheduler + IPC framing + repo clone + Claude Code subprocess manager; zero business logic.

## Orientation

A single Go binary running as two processes: the **supervisor** (long-lived parent — talks to the control plane, owns coordination) and per-checkout **workspace** children (one per repo, doing git/file/Claude Code work in an isolated temp dir). They speak over pipes (newline-framed JSON).

```
Control plane ──Command──> Supervisor ──routes via──> Pool ──spawns/feeds──> Workspace child
     ▲                          │                       │                        │
     └──events/heartbeat────────┘                  Registry                  temp dir + git
                                              (one record per workspace)
```

- **Command** — a unit of work from the control plane, modeled as a `Command` interface with two families: `WorkspaceCommand` (executed in a child against a `WorkspaceOps` capability) and `AgentCommand` (agent-scoped, executed in the supervisor). Adding a kind = a new type implementing the interface. Zero policy — commands carry fully-specified work.
- **Workspace** — a repo checkout with a lifecycle; the **Pool** owns one registry record per workspace (`{state, path, current command, runner}`) and is the single source of truth heartbeat and disk-sweep read. State has two axes: liveness (`Active`/`Defunct`/`Orphaned`) and busy-ness (current command).
- **Agent lifecycle** — after auth the agent is `unconfigured` and does no workspace work; its claim declares that state, the control plane sends a `ConfigUpdateCommand`, and the agent becomes `configured`, capped at `max_workspaces`.
- **Concurrency** — one root context; the supervisor owns all goroutines; cancel is the only shutdown. **N** (active workspaces) drives process/FD footprint; claim-worker count drives execution width, independent of N.
- **Errors** — command errors become failure events, transport errors retry, only boot-time errors crash, panics are recovered at the worker boundary.
- **Observability** — every log/span/metric carries `org_id` + `agent_id`; local logs are text, OTLP export is structured and late-bound on config.

## Principle

The agent is **zero biz logic**. Every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent owns: process spawning, IPC framing, repo clone, credential redaction, and telemetry. No policy.

## Layer graph

Imports flow downward only. `depguard` in `apps/agent/.golangci.yml` enforces the forbidden edges at lint time.

```
supervisor ──→ workspace, command, activity
workspace  ──→ command, protocol
command    ──→ protocol
activity   ──→ protocol
protocol   (leaf — no internal imports)
```

The key invariant: `protocol` does not import `command`. `ClaimCommand` returns `[]byte`; the supervisor calls `command.Decode` to get a typed `Command`. This keeps the arrow pointing down without a cycle.

## Subcommands

- `agent supervisor` — long-polls [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchanges identity, spawns one OS process per active workspace, heartbeats workspace registry + liveness, runs the disk janitor.
- `agent workspace` — per-workspace child process; reads raw JSON frames over stdin, decodes via `command.Decode`, calls `Execute` on the typed `WorkspaceCommand`, writes AgentEvents over stdout via `ipc` framing.

## Package layout

- `cmd/agent/` — main entrypoint; subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes; partial-read tolerance + concurrency-safe encoder.
- `internal/protocol/` — wire types + HTTP client matching [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml); `openapi_drift_test.go` asserts every property name has a matching `json:` tag. See [protocol.md](protocol.md).
- `internal/command/` — polymorphic `Command` interface, the 5 workspace command types + `ConfigUpdateCommand`, `WorkspaceOps`/`AgentOps` capability seams, typed result structs, and the `Decode` factory. See [command.md](command.md).
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop, per-workspace runner `Pool`; `pool.Dispatch` spawns/reuses/reaps workspace subprocesses. See [supervisor.md](supervisor.md).
- `internal/workspace/` — per-workspace dispatch loop (`Run`); `RealHandler` (production) implements `command.WorkspaceOps`: tempdir lifecycle, clone, write-files, auth-refresh, RunClaude, cleanup; `StubHandler` for tests. See [workspace.md](workspace.md).
- `internal/tracing/` — OTel wiring; W3C TraceContext propagation; `TraceparentEnv` exports current span to child processes.
- `internal/identity/` — `Provider` interface + `Credentials` struct; `placeholderProvider` carries the signed-STS payload; `Supervisor` depends on the interface for first-exchange and renewal. See [identity.md](identity.md).
- `internal/activity/` — activity WebSocket protocol: `SubscriptionSet`, `WorkspaceMapping`, `Batcher` (250 ms flush), `Conductor`. See [activity.md](activity.md).
- `internal/secret/` — `Secret` type; `String/GoString/MarshalJSON/Format` all return `"[REDACTED]"`; `.Value()` is the explicit unwrap.
- `internal/backoff/` — `1m → 3m → 5m → 15m → 60m` schedule with ±20 % jitter; per-surface counters (`sts`, `claim`, `heartbeat`, `ws`).
- `internal/observability/` — OTel SDK bootstrap, metric instrument declarations, standard dimension helpers (`SetStandardDimensions`, `StandardAttrs`). See [observability.md](observability.md).

## Wire-protocol internals

### Activity WebSocket

Supervisor maintains a bidirectional WS to `/api/v1/agents/{id}/activity` when `Config.ActivityWSURL` is set. `Batcher` buffers events per subscribed key and flushes one `activity_batch` frame per key at 250 ms. On dial failure the supervisor falls back to per-event HTTP `PostCommandEvent`.

### Live progress streaming

`RealHandler.RunClaude` dispatches via the `RunFunc` seam (production default: `RunStreaming`) and wires `OnStdoutLine` to push each Claude Code stream-json line as a `kind=progress` AgentEvent while also accumulating locally for the terminal event. `progress` events record without resuming the workflow engine — only `completed_*` events resume it.

### Workspace registry and lifecycle

The `Pool` is the single owner of workspace state. It holds one record per workspace_id; each record tracks two orthogonal axes:

- **Liveness** (`WorkspaceState`): `Active` → subprocess is running; `Defunct` → subprocess exited unexpectedly; `Orphaned` → leftover from a prior run.
- **Busy-ness** (`current_command_id`): `""` when idle, set to the in-flight command_id during `Dispatch`.

Heartbeat `status` is a pure projection of liveness: `Active → "running"`, `Defunct → "exited"`, `Orphaned → "unknown"`. An idle Active workspace reports `status="running"` — the heartbeat does not under-report between commands.

The disk sweep reads `pool.KnownIDs()` — Active + Defunct + Orphaned — so no registered directory is ever removed. The startup scan calls `pool.seedOrphan(id, path)` per found manifest. The forgotten-workspaces janitor reads `pool.Paths()` and calls `pool.remove(id)` after `os.RemoveAll` succeeds.

Full state machine and record shapes → [workspace_lifecycle.md](workspace_lifecycle.md).

### Per-command timeouts

`Pool.Dispatch` wraps each `runner.Send` in `context.WithTimeout` using `cmd.Timeout()`. Each command type owns its deadline: `InvokeClaudeCodeCommand.Timeout()` reads `Limits.WallclockSeconds` from the wire; all other kinds use Go-side defaults defined in `internal/command`. On timeout the pool emits `completed_failure` with reason `timeout: <kind> exceeded <duration> wall-clock` and drops the runner so the next `CreateWorkspace` can respawn.

### Credential redaction

All auth tokens flow through `internal/secret.Secret`. `fmt.Sprintf`, `json.Marshal`, and `log.Printf` variants all emit `"[REDACTED]"`; `.Value()` is the only way to unwrap. No token bytes leak into logs or error messages.

## Observability

All three OTel signals (traces, metrics, logs) share two standard dimensions on every record produced after identity exchange: `org_id` and `agent_id`. These are set once via `observability.SetStandardDimensions` immediately after the first successful identity exchange and never change for the process lifetime.

- **Resource attributes** (pod-level): `service.name`, `service.version`, `service.instance.id` = `agent_pod_id`.
- **Span / metric attributes** (post-exchange): `org_id`, `agent_id`. Per-command spans also carry `workspace_id`, `command_id`, `kind`.
- **Base slog logger**: the supervisor calls `slog.SetDefault(slog.Default().With("org_id", ..., "agent_id", ...))` after first exchange so every subsequent `slog.*` call emits both dimensions automatically.
- **OTLP disabled**: `observability.Init` is a no-op; instruments resolve to no-op SDK providers. Zero overhead.

Details → [observability.md](observability.md).

## Error handling — fatal-on-mismatch carve-out

The supervisor's normal error model is: retry-with-backoff forever, log warnings, never panic. One exception is carved out: **identity-integrity violations on bearer renewal**.

After first exchange, `supervisor` pins `agentID` and `orgID`. Every subsequent call to `exchangeIdentity` (bearer renewal) must return the same values. If the backend returns different `AgentID` or `OrgID`, `runOneRefreshCycle` returns `fatal=true` and `bearerRefreshLoop` calls `os.Exit(1)`.

Rationale: a pod that silently continues operating under a different identity would corrupt org-scoped audit and workflow records. A hard exit forces the orchestrator to restart with a fresh exchange rather than propagating bad identity silently.

## Concurrency model

The supervisor runs these goroutines concurrently after identity exchange:

- **N claim workers** (`Config.Concurrency`, default 4) — each runs its own `claimLoop`; they share the `Pool` (mutex-guarded) and the `protocol.Client` (inherently safe).
- **Heartbeat loop** — fires on `Config.HeartbeatInterval` (default 30 s); reads `Pool.Snapshot()` under the pool's lock.
- **Bearer refresh loop** — wakes ~1 h before bearer expiry; calls `exchangeIdentity` and `client.SetBearer` (atomic store).
- **Disk sweep loop** — fires every 5 min; reads `Pool.KnownIDs()` under the pool's lock; calls `os.RemoveAll` for orphan dirs.
- **Activity WS read loop** (optional) — runs while the WS is connected; exits on transport error, triggering the reconnect loop.
- **WS reconnect loop** (optional) — waits on `wsReadLoopDone`, sleeps on the WS backoff schedule, re-dials.

No goroutine shares mutable state without a lock or atomic. The `Pool` guards all workspace-record mutations with a `sync.Mutex`. `Conductor.SubscriptionSet` and `WorkspaceMapping` each have their own independent locks. `observability.SetStandardDimensions` is guarded by `stdDimsMu`.

## Testing model

Tests are pure-stdlib and fake-driven at the capability seams (`WorkspaceOps`, `AgentOps`, `identity.Provider`, `CloneFunc`, `RunFunc`); timing tests run in a `testing/synctest` bubble; every concurrency invariant ships a `-race` test (reviewer-gated convention); `protocol/openapi_drift_test.go` is the cross-plane Go↔Python schema-parity guard. Full per-layer map → [patterns.md § Testing](patterns.md#testing).
