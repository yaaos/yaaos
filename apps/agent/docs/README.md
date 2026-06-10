# apps/agent — yaaos WorkspaceAgent

> Customer-deployed Go binary that holds customer source code, runs coding agents locally, and reports findings + telemetry back to the yaaos control plane.

## Where things are

- `cmd/agent/` — entrypoint; `supervisor` and `workspace` subcommands.
- `internal/` — packages: `ipc`, `protocol`, `command`, `supervisor`, `workspace`, `tracing`, `identity`, `activity`, `secret`, `backoff`, `observability`.
- `bin/ci` — `gofmt`, `go mod tidy` drift, `golangci-lint` (includes `depguard` layer checks + `exhaustive` enum guards), build, `go test -race`, `govulncheck`, `semgrep`.

Internal architecture and package layout → [architecture.md](architecture.md).

Per-package docs: [protocol.md](protocol.md) · [command.md](command.md) · [activity.md](activity.md) · [identity.md](identity.md) · [observability.md](observability.md) · [supervisor.md](supervisor.md) · [workspace.md](workspace.md) · [workspace_lifecycle.md](workspace_lifecycle.md).

Coding conventions → [patterns.md](patterns.md).

Control-plane ↔ WorkspaceAgent protocol → [`docs/workspace-agent-protocol.md`](../../../docs/workspace-agent-protocol.md).

## Configuration

Environment variables consumed by `agent supervisor`:

| Var | Default | Purpose |
|---|---|---|
| `YAAOS_BACKEND_URL` | `https://app.yaaos.cloud` | Control-plane base URL. |
| `YAAOS_AGENT_VERSION` | `0.0.0-dev` | Reported during identity exchange. |
| `AWS_EC2_METADATA_SERVICE_ENDPOINT` | auto (IMDS v2) | Override IMDS endpoint. Set to `http://mock-aws:4566` in dev/test compose to use mock-aws. |
| `YAAOS_STS_ENDPOINT_URL` | `https://sts.amazonaws.com/` | URL the agent signs `GetCallerIdentity` against and embeds in the signed envelope. SigV4 binds the host into the signature, so the backend replays against the same URL. Set to `http://mock-aws:4566/` in dev/test compose. |
| `YAAOS_STS_HOST_OVERRIDE` | (none) | Allow an additional STS host (e.g. `mock-aws:4566`). Non-prod only; the backend refuses to boot if set with `APP_MODE=production`. |

## Wire protocol

See [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml). Hand-written; backend Pydantic mirror + Go types both live in-tree.

## Packaging

### Build

```bash
docker build -f apps/agent/Dockerfile -t yaaos-agent:dev apps/agent
```

Two-stage `golang:1.26-alpine` builder → `node:24-bookworm-slim` runtime (node + npm bundled so the agent can `npm i -g @anthropic-ai/claude-code` and exec `claude` at runtime). UID 65532. Agent is PID 1 — `SIGTERM` reaches it directly. `CGO_ENABLED=0` + `-trimpath` + `-ldflags='-s -w'`.

### Registry + tagging

Published to **`docker.io/yaaos/agent`** (Docker Hub). Tags:

- `MAJOR.MINOR` — immutable release tag (e.g. `0.1`); pin this in production. Minor increments on every `main` merge that changes `apps/agent/**`; major is the human-edited value in `apps/agent/VERSION`.
- `latest` — points to the most recent release; getting-started only.

Build target: `linux/arm64` only (built natively on the arm64 RWX CI runner). yaaos customer hosts are arm64.

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
    "image": "yaaos/agent:MAJOR.MINOR",
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
- `GET /api/workspaces/connection_status` returns `{state, pod_count, latest_heartbeat_at}` per org (`pod_count` = number of agent instances).
- Agent instance silent > 90 s → backend marks `state='unreachable'`; in-flight AgentCommands fail with `agent_lost`.

## Local dev

`docker compose up` brings up the backend + a mock-aws sidecar + a dev-mode agent. The agent reads IMDS credentials from mock-aws and sigv4-signs a `GetCallerIdentity` request; the backend replays against the same mock-aws. Set `YAAOS_DEV_SEED_ARN` in `.env` to configure the registered IAM ARN. See [`docs/setup.md`](../../../docs/setup.md).
