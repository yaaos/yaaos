# apps/agent — yaaos WorkspaceAgent

> Customer-deployed Go binary that holds customer source code, runs coding agents locally, and reports findings + telemetry back to the yaaos control plane.

## Phase

M05 Phase 6 (foundations) ships the wire-protocol layer + supervisor skeleton:

- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes (with partial-read tolerance + concurrency-safe encoder).
- `internal/protocol/` — Go mirror of the OpenAPI spec at [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml) + HTTP client for the five backend endpoints.
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop, per-workspace runner `Pool`. Each AgentCommand routes through `pool.Dispatch` which spawns a workspace subprocess on the first command, reuses it for subsequent commands, and reaps it after `CleanupWorkspace`. Production uses `ExecSpawn(os.Args[0])` for OS-process spawning + SIGTERM→SIGKILL cleanup; tests inject `InProcessSpawn(handler)` which runs `workspace.Run` in a goroutine over `io.Pipe` pairs. Pool drops failed/cancelled runners so a subsequent CreateWorkspace can respawn.
- `internal/workspace/` — per-workspace child-process dispatcher. `Run(ctx, in, out, handler, opts)` reads framed AgentCommands from stdin, dispatches by `kind` to the `Handler` interface, writes framed AgentEvents back to stdout. Slice 62 ships the dispatch loop + `StubHandler`; real bodies (clone, WriteFiles, Claude Code subprocess, cleanup) replace the stub on the same interface in later slices.
- `cmd/agent/main.go` — `agent supervisor` runs the full long-poll loop against `YAAOS_BACKEND_URL`. `agent workspace` runs the dispatch loop mounted against `workspace.StubHandler`.

## Architecture

The agent is **zero biz logic** — every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent is OS-process scheduling + IPC framing + repo clone + Claude Code subprocess management. No policy.

### Subcommands

- `agent supervisor` — long-poll [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchange identity, spawn one OS process per active workspace, heartbeat back inventory + liveness, run the disk janitor. **Phase 6 foundations**: identity + claim loop + heartbeat loop ship; per-workspace OS-process spawning + disk janitor + wall-clock timeout enforcement land in the follow-on.
- `agent workspace` — per-workspace child process; reads AgentCommands over stdin, writes AgentEvents over stdout via `ipc` framing. Dispatch frame ships (slice 62); real bodies (git clone, WriteFiles, Claude Code subprocess, cleanup) land in later slices on the same `Handler` interface.

### Layout

- `cmd/agent/` — main entrypoint, subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes.
- `internal/protocol/` — wire types + HTTP client matching the OpenAPI spec.
- `internal/supervisor/` — supervisor loop, long-poll workers, heartbeat.
- `internal/workspace/` — workspace child-process dispatch loop (`Run` + `Handler` interface) plus `RealHandler` (production) and `StubHandler` (tests). `RealHandler` owns the per-workspace tempdir lifecycle: `CreateWorkspace` allocates an `os.MkdirTemp` under `YAAOS_WORKSPACE_ROOT` (or `os.TempDir()`), `WriteFiles` writes path/content entries under the workspace root with path-escape protection, `RefreshWorkspaceAuth` swaps the stored auth token in-memory, `CleanupWorkspace` does `os.RemoveAll`. Each dispatch opens a `workspace.handle.<kind>` span around the Handler call. Git clone inside CreateWorkspace and the InvokeClaudeCode subprocess wiring are still follow-on slices.
- `internal/tracing/` — OTel wiring. `Init(withInMemory)` registers the W3C `TraceContext` propagator + an optional in-memory tracer provider for tests. `ExtractContext` parses an incoming traceparent into the active span slot so SDK `Start` derives the new span's parent correctly. `StartSpan` opens a span + returns an `end(err)` closure that records errors. `TraceparentEnv(ctx)` formats the current span as `TRACEPARENT=<value>` for export to child processes.
- `internal/identity/` — SigV4-signed STS `GetCallerIdentity` for control-plane verification. **Stub** in foundations.
- `bin/ci` — `go vet ./... && go build ./... && go test ./...`.

## Configuration

Environment variables consumed by `agent supervisor`:

| Var | Default | Purpose |
|---|---|---|
| `YAAOS_BACKEND_URL` | `http://localhost:8080` | Control-plane base URL. |
| `YAAOS_AGENT_POD_ID` | random 32-hex | Stable id the agent presents during identity exchange. |
| `YAAOS_AGENT_VERSION` | `0.0.0-dev` | Reported during identity exchange. |
| `YAAOS_SIGNED_STS_REQUEST` | placeholder | The signed STS payload. Phase 6 foundations: any non-empty value satisfies the backend's placeholder verifier; Phase 7 wires the real STS replay on both sides. |

## Wire protocol

See [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml). Hand-written; backend Pydantic mirror + Go types both live in-tree until codegen automation lands.

## Phase boundaries

- **Phase 0b** — directory + go.mod + skeleton package files + `bin/ci`.
- **Phase 5** — backend's `core/agent_gateway` implements the five HTTPS endpoints + placeholder bearer issuer.
- **Phase 6 foundations** — IPC framing library, wire-protocol Go types + HTTP client, supervisor identity-exchange + claim + heartbeat loops, command-routing stub. Tests for IPC + protocol decoding + client + an httptest-driven end-to-end against a fake backend.
- **Phase 6 follow-on slice 62** — workspace dispatch loop (`workspace.Run` + `Handler`) wired into `cmd/agent`. `StubHandler` returns success outputs for every command kind.
- **Phase 6 follow-on slice 63** — supervisor `Pool` + `WorkspaceRunner` interface, production `ExecSpawn` (`os/exec` of `os.Args[0] workspace` with stdin/stdout pipes + process-group SIGTERM→SIGKILL on close), test `InProcessSpawn` (in-goroutine `workspace.Run` over `io.Pipe`). `routeCommand` rewritten to dispatch through the pool.
- **Phase 6 follow-on slice 64** — OTel SDK + `internal/tracing` package. Supervisor extracts the backend's traceparent from each AgentCommand header, opens a `supervisor.dispatch.<kind>` span, rewrites the wire's traceparent before forwarding so the workspace sees the supervisor's span as parent. Workspace extracts per-command traceparent and opens a `workspace.handle.<kind>` span. `ExecSpawn` exports `TRACEPARENT=<value>` into the workspace subprocess's env so any future Claude Code shim inherits trace context.
- **Phase 6 follow-on slice 65** — `RealHandler` wired into `agent workspace`. Real tempdir lifecycle, real WriteFiles (with `../` escape protection), real auth-token refresh, real `os.RemoveAll` on cleanup. Configurable via `YAAOS_WORKSPACE_ROOT` env.
- **Phase 5 follow-on slice 67** — Go-side openapi drift test (`internal/protocol/openapi_drift_test.go`). Parses `apps/backend/openapi/agent-api.yaml`, walks `allOf` + `$ref` composition, asserts every YAML property name has a matching `json:` tag on the corresponding Go struct (embedded structs flattened so `CreateWorkspaceCommand`'s `CommandHeader` tags count). Enum parity for `CommandKind` / `EventKind` / `WorkspaceEventKind`. Mirror of the Python slice-66 test.
- **Phase 6 follow-on slice 68** — per-command wall-clock timeout in `supervisor.Pool`. `Dispatch` wraps each `runner.Send` in a `context.WithTimeout` whose deadline comes from the wire (`InvokeClaudeCodeCommand.Limits.WallclockSeconds`) for InvokeClaudeCode, or `PoolTimeouts` Go-side defaults (5m Create, 30s Write/Refresh/Cleanup, 15m InvokeClaudeCode fallback) for the others. On timeout the pool emits a `completed_failure` with reason `timeout: <kind> exceeded <duration> wall-clock` and drops the broken runner so the next `CreateWorkspace` respawns. Outer-context cancellation (supervisor shutdown) is distinguished from per-command timeout in the failure reason.
- **Phase 6 follow-on slice 69 (this)** — real `git clone` in `CreateWorkspace`. Runtime image swaps from `distroless/static-debian12` to `debian:bookworm-slim` with `git` + `ca-certificates` installed (the agent shells out to `git`; Claude Code will too). Image grew from ~25MB to ~80MB. `CreateWorkspace` now: tempdir → `git clone --depth=<history> [--branch=<name>] <auth-injected-url> <dest>` → `git fetch --depth=<n+1> origin <head_sha>` (with a full-fetch fallback) → `git checkout --detach <head_sha>`. Auth: github_installation tokens injected via `https://x-access-token:<token>@github.com/...`. Token-redaction in error messages (any string echoing the URL gets `x-access-token:<…>@` rewritten to `x-access-token:REDACTED@`). Clone failure tears down the tempdir + leaves no slot. `CloneFunc` injectable so unit tests use a no-op; integration tests spin up a local bare repo via `git init --bare` and clone via `file://`. Claude Code subprocess for InvokeClaudeCode + disk janitor + production OTel exporter still pending.
- **Phase 7** — real SigV4-signed STS verifier on the backend side; `RemoteAgentWorkspaceProvider` integration.
- **Phase 9 (this commit)** — multi-stage Dockerfile producing a distroless static image at `ghcr.io/yaaos/yaaos-agent`. Deployment guide below.

## Packaging

### Build

```bash
docker build -f apps/agent/Dockerfile -t yaaos-agent:dev apps/agent
```

The build is a two-stage `golang:1.22-alpine` → `gcr.io/distroless/static-debian12:nonroot`. Final image is ~25 MB, runs as UID 65532, has no shell. The agent process is PID 1 — `SIGTERM` from ECS reaches it directly without an init wrapper. `CGO_ENABLED=0` + `-trimpath` + `-ldflags='-s -w'` produce a fully-static, stripped binary with no host-path leakage.

### Registry + tagging

Published to **`ghcr.io/yaaos/yaaos-agent`** (decision logged in [plan/milestones/M05-workspace-agent/DECISIONS.md](../../../plan/milestones/M05-workspace-agent/DECISIONS.md)). Tags:

- `vX.Y.Z` — immutable release tag. Customer ECS task definitions pin this.
- `latest` — most recent stable release. Getting-started flows only; production pins to a `vX.Y.Z`.
- `sha-<short>` — every CI build. For incident bisection / rollback to a non-released build.

Multi-arch: `linux/amd64` + `linux/arm64` (built with `docker buildx`; CI wiring lands alongside the GHCR push workflow).

## Deployment (ECS Fargate)

The agent is designed for ECS Fargate at customer scale ~1–10 tasks. Each task is one supervisor pod that handles `Concurrency` workspaces in parallel.

### IAM role trust policy

The agent authenticates to the yaaos control plane via SigV4-signed STS `GetCallerIdentity` (Phase 7 follow-on). The IAM role attached to the ECS task must have:

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

Register the role's ARN in yaaos at `PATCH /api/orgs` with `{workspace_provider: "remote_agent", registered_iam_arn: "arn:aws:iam::ACCOUNT:role/..."}`. Phase 7 follow-on adds the matching org-settings UI.

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

The agent logs to stdout in structured plain text today (line-per-event). JSON structured logs land alongside the Phase 6 follow-on OTel wiring.

### Health + scaling

- ECS service auto-scales tasks above sustained load.
- Backend tracks per-pod liveness via the `workspace_agents.last_heartbeat_at` column (Phase 7); `GET /api/workspaces/connection_status` returns `{state, pod_count, latest_heartbeat_at}` aggregated for an org.
- Pod silently > 90s = backend marks `state='unreachable'`; in-flight AgentCommands fail with `agent_lost` recovery label.

## Local dev

Local `docker compose up` brings up the backend + a dev-mode agent against the placeholder identity-exchange verifier (any non-empty `YAAOS_SIGNED_STS_REQUEST` works). See [`docs/setup.md`](../../../docs/setup.md) § M05 dev story.
