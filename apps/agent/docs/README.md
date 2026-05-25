# apps/agent — yaaos WorkspaceAgent

> Customer-deployed Go binary that holds customer source code, runs coding agents locally, and reports findings + telemetry back to the yaaos control plane.

## Components

- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes (with partial-read tolerance + concurrency-safe encoder).
- `internal/protocol/` — Go mirror of the OpenAPI spec at [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml) + HTTP client for the five backend endpoints.
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop, per-workspace runner `Pool`. Each AgentCommand routes through `pool.Dispatch` which spawns a workspace subprocess on the first command, reuses it for subsequent commands, and reaps it after `CleanupWorkspace`. Production uses `ExecSpawn(os.Args[0])` for OS-process spawning + SIGTERM→SIGKILL cleanup; tests inject `InProcessSpawn(handler)` which runs `workspace.Run` in a goroutine over `io.Pipe` pairs. Pool drops failed/cancelled runners so a subsequent CreateWorkspace can respawn.
- `internal/workspace/` — per-workspace child-process dispatcher. `Run(ctx, in, out, handler, opts)` reads framed AgentCommands from stdin, dispatches by `kind` to the `Handler` interface, writes framed AgentEvents back to stdout. `RealHandler` is the production handler; `StubHandler` is for tests.
- `cmd/agent/main.go` — `agent supervisor` runs the long-poll loop against `YAAOS_BACKEND_URL`. `agent workspace` runs the dispatch loop mounted against `workspace.RealHandler`.

## Architecture

The agent is **zero biz logic** — every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent is OS-process scheduling + IPC framing + repo clone + Claude Code subprocess management. No policy.

### Subcommands

- `agent supervisor` — long-poll [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchange identity, spawn one OS process per active workspace, heartbeat back inventory + liveness, run the disk janitor.
- `agent workspace` — per-workspace child process; reads AgentCommands over stdin, writes AgentEvents over stdout via `ipc` framing. Dispatches by `kind` through the `Handler` interface (`RealHandler` in production).

### Layout

- `cmd/agent/` — main entrypoint, subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes.
- `internal/protocol/` — wire types + HTTP client matching the OpenAPI spec.
- `internal/supervisor/` — supervisor loop, long-poll workers, heartbeat.
- `internal/workspace/` — workspace child-process dispatch loop (`Run` + `Handler` interface) plus `RealHandler` (production) and `StubHandler` (tests). `RealHandler` owns the per-workspace tempdir lifecycle: `CreateWorkspace` allocates an `os.MkdirTemp` under `YAAOS_WORKSPACE_ROOT` (or `os.TempDir()`) and `git clone`s the repo into it (auth via `x-access-token:<token>@…`, token-redacted on error), `WriteFiles` writes path/content entries under the workspace root with path-escape protection, `RefreshWorkspaceAuth` swaps the stored auth token in-memory, `InvokeClaudeCode` runs the wire's `invocation.exec` block through `workspace.RunStreaming`, `CleanupWorkspace` does `os.RemoveAll`. Each dispatch opens a `workspace.handle.<kind>` span around the Handler call.
- `internal/tracing/` — OTel wiring. `Init(withInMemory)` registers the W3C `TraceContext` propagator + an optional in-memory tracer provider for tests. `ExtractContext` parses an incoming traceparent into the active span slot so SDK `Start` derives the new span's parent correctly. `StartSpan` opens a span + returns an `end(err)` closure that records errors. `TraceparentEnv(ctx)` formats the current span as `TRACEPARENT=<value>` for export to child processes.
- `internal/identity/` — SigV4-signed STS `GetCallerIdentity` for control-plane verification.
- `bin/ci` — `gofmt`, `go mod tidy` drift, `golangci-lint`, `go build`, `go test -race`, `govulncheck`, `semgrep` (`p/golang` + `p/owasp-top-ten`). Linter config at `.golangci.yml`.

## Configuration

Environment variables consumed by `agent supervisor`:

| Var | Default | Purpose |
|---|---|---|
| `YAAOS_BACKEND_URL` | `http://localhost:8080` | Control-plane base URL. |
| `YAAOS_AGENT_POD_ID` | random 32-hex | Stable id the agent presents during identity exchange. |
| `YAAOS_AGENT_VERSION` | `0.0.0-dev` | Reported during identity exchange. |
| `YAAOS_SIGNED_STS_REQUEST` | placeholder | The signed STS payload presented during identity exchange. Any non-empty value satisfies the backend's current placeholder verifier. |

## Wire protocol

See [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml). Hand-written; backend Pydantic mirror + Go types both live in-tree. An `internal/protocol/openapi_drift_test.go` test parses the YAML and asserts every property name has a matching `json:` tag on the corresponding Go struct (with `CommandKind` / `EventKind` / `WorkspaceEventKind` enum parity).

### Activity WebSocket

The supervisor maintains a bidirectional WebSocket to `/api/v1/agents/{id}/activity` (when `Config.ActivityWSURL` is set). `internal/activity` owns the protocol: `SubscriptionSet` mirrors the backend's subscriber registry, `WorkspaceMapping` caches `workspace_id → workflow_execution_id`, `Batcher` buffers events per subscribed key and flushes one `activity_batch` frame per key at `Config.ActivityBatchInterval` (250 ms default), and `Conductor` composes them with `HandleInbound` (decodes `subscribe`/`unsubscribe`) + `Publish` (forwards to the Batcher, dropping events the backend hasn't subscribed to). `WSConn.Dial` injects the bearer header; `RunInbound` ties read frames to the Conductor. On dial failure the supervisor falls back to per-event HTTP `PostCommandEvent` posts.

### Live progress streaming

`workspace.Run` installs an `encoderEmitter` (via `internal/workspace.Emitter`) that writes `kind=progress` AgentEvents to the same goroutine-safe IPC encoder used for terminal events. `RealHandler.InvokeClaudeCode` pulls the emitter via `EmitterFromContext(ctx)` and wires `workspace.RunStreaming.OnStdoutLine` to push each Claude Code stream-json line as a progress event while accumulating locally for the terminal event. Supervisor-side, `WorkspaceRunner.Send` takes an `onProgress func(AgentEvent)` callback that `Pool.Dispatch` threads through; the backend records progress events without resuming the workflow engine (only `completed_*` resumes).

### Workspace lifecycle

`RealHandler.CreateWorkspace` writes a `.workspace-id` manifest into each tempdir after clone. On startup, the supervisor's `scanOrphanWorkspaces(WorkspaceRoot)` reads those manifests and pre-loads each orphan as `status="unknown"` so the first heartbeat reports them; the backend issues cleanup via `HeartbeatResponse.forgotten_workspaces`, which the supervisor's `cleanupForgottenWorkspaces` honors with `os.RemoveAll` on the named paths.

### Per-command timeouts

`Pool.Dispatch` wraps each `runner.Send` in `context.WithTimeout`. Deadlines come from the wire (`InvokeClaudeCodeCommand.Limits.WallclockSeconds`) for InvokeClaudeCode, or from `PoolTimeouts` Go-side defaults (5 m Create, 30 s Write/Refresh/Cleanup, 15 m InvokeClaudeCode fallback). On timeout the pool emits a `completed_failure` with reason `timeout: <kind> exceeded <duration> wall-clock` and drops the runner so the next `CreateWorkspace` respawns. Supervisor-shutdown cancellation is distinguished from per-command timeout in the failure reason.

### Credential logging

`internal/secret.Secret` wraps every credential in the agent. `String()` / `GoString()` / `MarshalJSON()` / `Format()` all return `"[REDACTED]"`; `.Value()` is the explicit unwrap. Auth tokens flow through `Secret`, so `fmt.Sprintf("%+v", slot)` / `json.Marshal(slot)` / `log.Printf("%v", h.slots)` never leak token bytes.

## Packaging

### Build

```bash
docker build -f apps/agent/Dockerfile -t yaaos-agent:dev apps/agent
```

The build is a two-stage `golang:1.22-alpine` → `gcr.io/distroless/static-debian12:nonroot`. Final image is ~25 MB, runs as UID 65532, has no shell. The agent process is PID 1 — `SIGTERM` from ECS reaches it directly without an init wrapper. `CGO_ENABLED=0` + `-trimpath` + `-ldflags='-s -w'` produce a fully-static, stripped binary with no host-path leakage.

### Registry + tagging

Published to **`ghcr.io/yaaos/yaaos-agent`**. Tags:

- `vX.Y.Z` — immutable release tag. Customer ECS task definitions pin this.
- `latest` — most recent stable release. Getting-started flows only; production pins to a `vX.Y.Z`.
- `sha-<short>` — every CI build. For incident bisection / rollback to a non-released build.

Multi-arch: `linux/amd64` + `linux/arm64` (built with `docker buildx`).

## Deployment (ECS Fargate)

The agent is designed for ECS Fargate at customer scale ~1–10 tasks. Each task is one supervisor pod that handles `Concurrency` workspaces in parallel.

### IAM role trust policy

The agent authenticates to the yaaos control plane via SigV4-signed STS `GetCallerIdentity`. The IAM role attached to the ECS task must have:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:GetCallerIdentity",
    "Resource": "*"
  }]
}
```

…and a trust policy that allows the ECS task role to assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Register the role's ARN in yaaos at `PATCH /api/orgs` with `{workspace_provider: "remote_agent", registered_iam_arn: "arn:aws:iam::ACCOUNT:role/..."}`.

### Task definition template

```json
{
  "family": "yaaos-agent",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::ACCOUNT:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::ACCOUNT:role/yaaos-agent",
  "containerDefinitions": [{
    "name": "agent",
    "image": "ghcr.io/yaaos/yaaos-agent:vX.Y.Z",
    "essential": true,
    "command": ["supervisor"],
    "environment": [
      {"name": "YAAOS_BACKEND_URL", "value": "https://yaaos.example.com"},
      {"name": "YAAOS_AGENT_VERSION", "value": "X.Y.Z"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/yaaos/agent",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "agent"
      }
    },
    "mountPoints": [{
      "sourceVolume": "workspaces",
      "containerPath": "/var/agent/workspaces"
    }]
  }],
  "volumes": [{
    "name": "workspaces",
    "host": {}
  }]
}
```

Scale `cpu` / `memory` with `Concurrency` (default 4 workspaces per pod). A standard 1 vCPU / 2 GB task handles ~4 concurrent reviews comfortably.

### CloudWatch log group

Create the log group once before the first deploy:

```bash
aws logs create-log-group --log-group-name /yaaos/agent
aws logs put-retention-policy --log-group-name /yaaos/agent --retention-in-days 30
```

### Logging

The agent uses Go's `log/slog` and writes every record to a **multi-sink fan-out**:

- **stdout** — picked up by the ECS awslogs driver → CloudWatch (`/yaaos/agent`, 30-day retention).
- **rotated local file** at `${YAAOS_LOG_DIR:-/var/log/yaaos-agent}/agent.log` — the operator's out-of-band channel when the control plane is unreachable and CloudWatch is no help. Pull with `aws ecs execute-command --command 'cat /var/log/yaaos-agent/agent.log'` or `kubectl cp`.
  - Rotation: 50 MB per file, 10 backups, 3-day age (gzipped). Lumberjack runs the rotation; pruning fires on the next write so the agent's ~30s heartbeat cadence keeps it tidy.
  - If the directory is unwritable (mount missing, permissions wrong), the agent emits one stderr warning and continues with stdout-only — never fatal.
- **OTel collector** (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set) — the `internal/observability` package plugs an `otelslog` bridge into the logging fan-out as the third sink. Disabled by default; zero overhead when the env var is unset.

### Observability

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the agent exports all three OTel signals to whichever collector the customer points it at. The OTel Collector is vendor-neutral — pick Datadog / Honeycomb / New Relic / AWS CloudWatch / Splunk / Grafana / etc. at the collector, not in the agent.

- **Logs** — every `slog` record fans out to the collector via the `otelslog` bridge (bound to scope `github.com/yaaos/agent`).
- **Traces** — the supervisor's `tracing.StartSpan` ([`internal/tracing`](../internal/tracing)) writes spans (`supervisor.dispatch.<kind>`, `workspace.handle.<kind>`) plus auto-instrumented client spans on every outbound HTTP call (via `otelhttp.NewTransport` on the protocol client's transport). W3C TraceContext propagation chains backend → supervisor → workspace → Claude Code.
- **Metrics** — minimum set declared in [`internal/observability/metrics.go`](../internal/observability/metrics.go):
  - `yaaos.agent.commands.claimed` (counter)
  - `yaaos.agent.commands.completed{result, kind}` (counter)
  - `yaaos.agent.command.duration` (histogram, seconds)
  - `yaaos.agent.workspaces.active` (up/down counter)
  - `yaaos.agent.connection.failures{surface, class}` (counter)
  - `yaaos.agent.connection.backoff_seconds{surface}` (gauge)

Resource attributes: `service.name=yaaos-workspace-agent`, `service.version`, `service.instance.id` (the `agent.pod_id` the backend stores in `workspace_agents.agent_pod_id`).

Standard env vars: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_PROTOCOL` (`http/protobuf` default), `OTEL_METRIC_EXPORT_INTERVAL` (ms, 30 s default), `OTEL_SDK_DISABLED`. No yaaos-prefixed variants — customers reuse whatever OTel config their other services already use.

### Connection resilience

Every control-plane interaction is wrapped in a backoff schedule from [`internal/backoff`](../internal/backoff):

**Schedule:** `1m → 3m → 5m → 15m → 60m forever`, with ±20 % jitter on every step. The jitter defeats thundering-herd reconnects when N agents recover simultaneously after a backend outage. Same ramp applies to **auth** (401/403) and **network** (5xx, connection refused, timeout) failures — operators distinguish via the local log file, not via different cadences.

**Per-surface counters** (4 independent schedules so a misconfigured ARN doesn't slow heartbeat retries on an unrelated transient blip):

| Surface | Site | Behavior |
|---|---|---|
| `sts` | bootstrap identity exchange in `Run()` | Retries forever on the schedule instead of crashing the agent. Operator sees the failure class in the local log; the process stays up so OTel metrics keep flowing. |
| `claim` | long-poll loop | On `ErrNoCommand` the counter resets (a 204 isn't a failure). On any other error, sleep on the schedule and retry. |
| `heartbeat` | 30 s ticker | On failure, sleep on the schedule before letting the next tick fire. Reset on first successful heartbeat. |
| `ws` | activity-WS dial + read loop | On dial fail OR read-loop exit, `wsReconnectLoop` re-dials on the schedule. WS is best-effort; commands keep flowing via HTTP fallback while it's down. |

Each failure logs a `WARN` line with `surface`, `class` (`auth` / `network`), and `next_sleep_seconds`. The OTel `yaaos.agent.connection.failures{surface,class}` counter increments on each failure; `yaaos.agent.connection.backoff_seconds{surface}` gauge tracks the upcoming sleep so dashboards can chart "which surface is in backoff and how deep."

The `workspace` subcommand routes its console sink to **stderr** instead of stdout, because stdout there is the supervisor↔workspace IPC pipe. The file sink is identical in both modes.

### Health + scaling

- ECS service auto-scales tasks above sustained load.
- Backend tracks per-pod liveness via the `workspace_agents.last_heartbeat_at` column; `GET /api/workspaces/connection_status` returns `{state, pod_count, latest_heartbeat_at}` aggregated for an org.
- Pod silently > 90s = backend marks `state='unreachable'`; in-flight AgentCommands fail with `agent_lost` recovery label.

## Local dev

Local `docker compose up` brings up the backend + a dev-mode agent against the placeholder identity-exchange verifier (any non-empty `YAAOS_SIGNED_STS_REQUEST` works). See [`docs/setup.md`](../../../docs/setup.md).
