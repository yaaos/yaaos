# apps/agent — internal architecture

> OS-process scheduler + IPC framing + repo clone + Claude Code subprocess manager; zero business logic.

## Principle

The agent is **zero biz logic**. Every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent owns: process spawning, IPC framing, repo clone, credential redaction, and telemetry. No policy.

## Subcommands

- `agent supervisor` — long-polls [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchanges identity, spawns one OS process per active workspace, heartbeats inventory + liveness, runs the disk janitor.
- `agent workspace` — per-workspace child process; reads AgentCommands over stdin, writes AgentEvents over stdout via `ipc` framing, dispatches by `kind` through the `Handler` interface.

## Package layout

- `cmd/agent/` — main entrypoint; subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes; partial-read tolerance + concurrency-safe encoder.
- `internal/protocol/` — wire types + HTTP client matching [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml); `openapi_drift_test.go` asserts every property name has a matching `json:` tag.
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop, per-workspace runner `Pool`; `pool.Dispatch` spawns/reuses/reaps workspace subprocesses.
- `internal/workspace/` — per-workspace dispatch loop (`Run` + `Handler`); `RealHandler` (production) owns tempdir lifecycle: clone, write-files, auth-refresh, InvokeClaudeCode, cleanup; `StubHandler` for tests.
- `internal/tracing/` — OTel wiring; W3C TraceContext propagation; `TraceparentEnv` exports current span to child processes.
- `internal/identity/` — SigV4-signed STS `GetCallerIdentity` for control-plane verification.
- `internal/activity/` — activity WebSocket protocol: `SubscriptionSet`, `WorkspaceMapping`, `Batcher` (250 ms flush), `Conductor`.
- `internal/secret/` — `Secret` type; `String/GoString/MarshalJSON/Format` all return `"[REDACTED]"`; `.Value()` is the explicit unwrap.
- `internal/backoff/` — `1m → 3m → 5m → 15m → 60m` schedule with ±20 % jitter; per-surface counters (`sts`, `claim`, `heartbeat`, `ws`).
- `internal/observability/` — OTel metrics declarations; `otelslog` bridge for log fan-out.

## Wire-protocol internals

### Activity WebSocket

Supervisor maintains a bidirectional WS to `/api/v1/agents/{id}/activity` when `Config.ActivityWSURL` is set. `Batcher` buffers events per subscribed key and flushes one `activity_batch` frame per key at 250 ms. On dial failure the supervisor falls back to per-event HTTP `PostCommandEvent`.

### Live progress streaming

`RealHandler.InvokeClaudeCode` wires `RunStreaming.OnStdoutLine` to push each Claude Code stream-json line as a `kind=progress` AgentEvent while also accumulating locally for the terminal event. `progress` events record without resuming the workflow engine — only `completed_*` events resume it.

### Workspace lifecycle

`RealHandler.CreateWorkspace` writes a `.workspace-id` manifest into each tempdir after clone. On startup `scanOrphanWorkspaces` reads those manifests and pre-loads orphans as `status="unknown"` so the first heartbeat reports them. The backend issues cleanup via `HeartbeatResponse.forgotten_workspaces`; `cleanupForgottenWorkspaces` honors it with `os.RemoveAll`.

### Per-command timeouts

`Pool.Dispatch` wraps each `runner.Send` in `context.WithTimeout`. Deadlines come from the wire (`InvokeClaudeCodeCommand.Limits.WallclockSeconds`) or from `PoolTimeouts` Go-side defaults (5 m Create, 30 s Write/Refresh/Cleanup, 15 m InvokeClaudeCode fallback). On timeout the pool emits `completed_failure` with reason `timeout: <kind> exceeded <duration> wall-clock` and drops the runner so the next `CreateWorkspace` can respawn.

### Credential redaction

All auth tokens flow through `internal/secret.Secret`. `fmt.Sprintf`, `json.Marshal`, and `log.Printf` variants all emit `"[REDACTED]"`; `.Value()` is the only way to unwrap. No token bytes leak into logs or error messages.
