# apps/agent — yaaos WorkspaceAgent

> Customer-deployed Go binary that holds customer source code, runs coding agents locally, and reports findings + telemetry back to the yaaos control plane.

## Phase

M05 Phase 6 (foundations) ships the wire-protocol layer + supervisor skeleton:

- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes (with partial-read tolerance + concurrency-safe encoder).
- `internal/protocol/` — Go mirror of the OpenAPI spec at [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml) + HTTP client for the five backend endpoints.
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop. Command-routing stub emits `completed_success` for every command kind so the backend's workflow engine can advance end-to-end; real workspace OS-process spawning + Claude Code invocation lands in the follow-on iteration.
- `cmd/agent/main.go` — `agent supervisor` runs the full loop against `YAAOS_BACKEND_URL`. `agent workspace` prints a not-implemented marker.

## Architecture

The agent is **zero biz logic** — every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent is OS-process scheduling + IPC framing + repo clone + Claude Code subprocess management. No policy.

### Subcommands

- `agent supervisor` — long-poll [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchange identity, spawn one OS process per active workspace, heartbeat back inventory + liveness, run the disk janitor. **Phase 6 foundations**: identity + claim loop + heartbeat loop ship; per-workspace OS-process spawning + disk janitor + wall-clock timeout enforcement land in the follow-on.
- `agent workspace` — per-workspace child process; reads AgentCommands over stdin, writes AgentEvents over stdout. Wraps git clone + Claude Code CLI. **Phase 6 follow-on.**

### Layout

- `cmd/agent/` — main entrypoint, subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes.
- `internal/protocol/` — wire types + HTTP client matching the OpenAPI spec.
- `internal/supervisor/` — supervisor loop, long-poll workers, heartbeat.
- `internal/workspace/` — workspace process body. **Stub** in foundations.
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
- **Phase 6 foundations (this)** — IPC framing library, wire-protocol Go types + HTTP client, supervisor identity-exchange + claim + heartbeat loops, command-routing stub. Tests for IPC + protocol decoding + client + an httptest-driven end-to-end against a fake backend.
- **Phase 6 follow-on** — workspace OS-process spawning, IPC reader/writer in the workspace process, repo clone + Claude Code invocation, wall-clock timeout, disk janitor, OTel SDK wiring (`go.opentelemetry.io/otel` with `propagation.TraceContext` + in-memory exporter for tests + traceparent extraction into child spans).
- **Phase 7** — real SigV4-signed STS verifier on the backend side; `RemoteAgentWorkspaceProvider` integration.
- **Phase 9** — Dockerfile, image registry, deployment guide.
