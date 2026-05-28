# apps/agent — yaaos WorkspaceAgent

> Customer-deployed Go binary that holds customer source code, runs coding agents locally, and reports findings + telemetry back to the yaaos control plane.

## Where things are

- `cmd/agent/` — entrypoint; `supervisor` and `workspace` subcommands.
- `internal/` — packages: `ipc`, `protocol`, `supervisor`, `workspace`, `tracing`, `identity`, `activity`, `secret`, `backoff`, `observability`.
- `bin/ci` — `gofmt`, `go mod tidy` drift, `golangci-lint`, build, `go test -race`, `govulncheck`, `semgrep`.

Internal architecture, package responsibilities, and wire-protocol details → [architecture.md](architecture.md).

## Configuration

Environment variables consumed by `agent supervisor`:

| Var | Default | Purpose |
|---|---|---|
| `YAAOS_BACKEND_URL` | `http://localhost:8080` | Control-plane base URL. |
| `YAAOS_AGENT_POD_ID` | random 32-hex | Stable id presented during identity exchange. |
| `YAAOS_AGENT_VERSION` | `0.0.0-dev` | Reported during identity exchange. |
| `YAAOS_SIGNED_STS_REQUEST` | placeholder | Signed STS payload for identity exchange. Any non-empty value satisfies the current placeholder verifier. |

## Wire protocol

See [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml). Hand-written; backend Pydantic mirror + Go types both live in-tree.

## Packaging

### Build

```bash
docker build -f apps/agent/Dockerfile -t yaaos-agent:dev apps/agent
```

Two-stage `golang:1.22-alpine` → `gcr.io/distroless/static-debian12:nonroot`. ~25 MB, UID 65532, no shell. Agent is PID 1 — `SIGTERM` reaches it directly. `CGO_ENABLED=0` + `-trimpath` + `-ldflags='-s -w'`.

### Registry + tagging

Published to **`ghcr.io/yaaos/yaaos-agent`**. Tags:

- `vX.Y.Z` — immutable release tag; pin this in production.
- `latest` — most recent stable; getting-started only.
- `sha-<short>` — every CI build; for bisection/rollback.

Multi-arch: `linux/amd64` + `linux/arm64` (built with `docker buildx`).

## Deployment (ECS Fargate)

Designed for customer scale ~1–10 tasks. Each task is one supervisor pod handling `Concurrency` workspaces in parallel (default 4).

### IAM role

The ECS task role needs:

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

Trust policy:

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

Register the role ARN in yaaos: `PATCH /api/orgs` with `{workspace_provider: "remote_agent", registered_iam_arn: "arn:aws:iam::ACCOUNT:role/..."}`.

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
  "volumes": [{"name": "workspaces", "host": {}}]
}
```

Scale `cpu`/`memory` with `Concurrency`. 1 vCPU / 2 GB handles ~4 concurrent reviews comfortably.

### CloudWatch log group

```bash
aws logs create-log-group --log-group-name /yaaos/agent
aws logs put-retention-policy --log-group-name /yaaos/agent --retention-in-days 30
```

## Observability

Logging fan-out (via `internal/observability`):

- **stdout** — ECS awslogs driver → CloudWatch (`/yaaos/agent`, 30-day retention).
- **rotated file** at `${YAAOS_LOG_DIR:-/var/log/yaaos-agent}/agent.log` — 50 MB / 10 backups / 3-day age (gzipped). Pull via `aws ecs execute-command`. If the directory is unwritable the agent warns and continues stdout-only.
- **OTel collector** — when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; zero overhead otherwise.

OTel signals (when endpoint is set):

- **Traces** — `supervisor.dispatch.<kind>` + `workspace.handle.<kind>` spans; W3C TraceContext chains backend → supervisor → workspace → Claude Code. Outbound HTTP auto-instrumented via `otelhttp`.
- **Metrics** (from [`internal/observability/metrics.go`](../internal/observability/metrics.go)):
  - `yaaos.agent.commands.claimed`
  - `yaaos.agent.commands.completed{result, kind}`
  - `yaaos.agent.command.duration` (histogram, seconds)
  - `yaaos.agent.workspaces.active`
  - `yaaos.agent.connection.failures{surface, class}`
  - `yaaos.agent.connection.backoff_seconds{surface}`
- **Logs** — every `slog` record fans to the collector via `otelslog` bridge.

Resource attributes: `service.name=yaaos-workspace-agent`, `service.version`, `service.instance.id`. Standard OTel env vars apply (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_SDK_DISABLED`).

## Connection resilience

Backoff schedule: `1m → 3m → 5m → 15m → 60m forever`, ±20 % jitter. Four independent per-surface schedules:

| Surface | Site | Notes |
|---|---|---|
| `sts` | bootstrap identity exchange | Retries forever; process stays up so OTel keeps flowing. |
| `claim` | long-poll loop | `ErrNoCommand` (204) resets the counter; any other error sleeps on schedule. |
| `heartbeat` | 30 s ticker | Sleeps on schedule before next tick; resets on first success. |
| `ws` | activity-WS dial + read | `wsReconnectLoop` re-dials on schedule; commands keep flowing via HTTP fallback. |

Each failure logs `WARN` with `surface`, `class` (`auth`/`network`), and `next_sleep_seconds`. `workspace` subcommand routes its console sink to stderr (stdout is the IPC pipe).

### Health + scaling

- Backend tracks liveness via `workspace_agents.last_heartbeat_at`.
- `GET /api/workspaces/connection_status` returns `{state, pod_count, latest_heartbeat_at}` per org.
- Pod silent > 90 s → backend marks `state='unreachable'`; in-flight AgentCommands fail with `agent_lost`.

## Local dev

`docker compose up` brings up the backend + a dev-mode agent. Any non-empty `YAAOS_SIGNED_STS_REQUEST` satisfies the placeholder verifier. See [`docs/setup.md`](../../../docs/setup.md).
