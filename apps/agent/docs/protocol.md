# internal/protocol

> Wire-DTO leaf package: JSON types matching the backend OpenAPI spec, `CommandKind` constants, and the HTTP client — no business logic, no decode dispatch.

## Scope

**Owns:**
- All concrete command wire structs: `ProvisionWorkspaceCommand`, `WriteFilesCommand`, `RefreshWorkspaceAuthCommand`, `InvokeClaudeCodeCommand`, `CleanupWorkspaceCommand`, `ConfigUpdateCommand`.
- `CommandHeader` — embedded in every concrete command; carries `command_id`, `workspace_id`, `traceparent`, `kind`, `completion_token`.
- `CommandKind` constants.
- Event types: `AgentEvent`, `EventKind` constants.
- Identity, heartbeat, and claim HTTP types.
- `Client` — the HTTP client for the 4 backend endpoints the agent calls.

**Does not own:**
- Union dispatch or `kind`-switch decoding — that is `command.Decode` (`internal/command`).
- Any business logic, timeouts, or ops implementations.

**Boundary:**
- Receives: raw HTTP responses (JSON bodies).
- Emits: typed Go structs + raw bytes from `ClaimCommand`.
- `ClaimCommand` returns `([]byte, error)` — the caller (`supervisor.claimLoop`) passes the bytes to `command.Decode`. This keeps `protocol` below `command` in the layer graph without a cycle.

## Why / invariants

- **Leaf package.** `protocol` imports no other internal packages. All consumers import it; it imports nothing from them. `depguard` enforces this — see `apps/agent/.golangci.yml`.
- **`ClaimCommand` returns raw bytes.** Decoding the union into a typed `Command` requires `command.Decode`, which lives above `protocol`. Returning `[]byte` keeps the dependency arrow pointing down.
- **Field tags are load-bearing.** `json:` tags on every field must match the keys the backend emits and the openapi spec declares. `openapi_drift_test.go` enforces this mechanically.
- **Flat wire shape (workspace commands).** The backend sends each workspace command's fields as a flat JSON object with `kind` embedded. Each concrete struct embeds `CommandHeader` so `kind`, `command_id`, `workspace_id`, and `traceparent` are always present. `ConfigUpdateCommand` is the one exception: it embeds `CommandHeader` too but nests its payload under a `config` object (`AgentConfigWire`). `command.Decode` unmarshals into `protocol.ConfigUpdateCommand` directly, so the decoded shape, the OpenAPI spec, and the drift test cannot diverge.
- **No agent ID in URLs for operational channels.** `Heartbeat` and `ClaimCommand` use bearer-derived identity; no `agentID` parameter is passed to these methods. The caller no longer needs to thread `agentID` into every protocol call after the initial identity exchange.

## Gotchas

- Do not add union-dispatch or `UnmarshalJSON` override here. Union dispatch was removed; the sole decode path is `command.Decode`.
- `InvokeClaudeCodeCommand.Invocation` is `json.RawMessage` — the agent passes it through without parsing; the backend owns the invocation schema.
- `ClaimCommand` returns `[]byte`, not a typed struct. The caller (`supervisor.claimLoop`) passes the bytes to `command.Decode`. Do not change the return type to a concrete struct — doing so would require `protocol` to import `command`, breaking the layer graph.

## Vocabulary

- **Wire struct** — a Go struct whose JSON tags exactly match the backend OpenAPI spec fields for one command kind or event.
- **CommandHeader** — the routing fields every command carries: `command_id`, `workspace_id`, `traceparent`, `kind`, plus the `completion_token` capability the agent echoes on its events, and `workflow_execution_id` (the workflow execution that dispatched the command; empty for agent-scoped commands like ConfigUpdate).
- **Leaf** — a package with no internal imports; safe for any layer to import without cycles.

## Endpoint URLs

| Method | URL | Notes |
|---|---|---|
| `POST` | `/api/v1/agent/identity` | Unauthenticated bootstrap |
| `POST` | `/api/v1/agent/heartbeat` | Bearer-gated; no agent ID in URL |
| `POST` | `/api/v1/agent/commands/claim` | Bearer-gated; capacity-pull body |
| `POST` | `/api/v1/commands/{id}/events` | Per-command ID retained |
| `POST` | `/api/v1/workspaces/{id}/events` | Per-workspace ID retained |
| `WSS` | `/api/v1/agent/activity` | Bearer-gated; no agent ID in URL |

## Claim body (capacity-pull)

`ClaimRequest` carries:
- `lifecycle` — `"unconfigured"` (delivers only `ConfigUpdate`) or `"configured"`.
- `new_workspaces` — `max_workspaces − active count`; the backend returns up to this many unassigned `ProvisionWorkspace` rows.
- `workspace_ids` — idle Active workspaces; the backend returns one pending command per named workspace.

## Completion token

Each command header carries a one-time backend-minted `completion_token` (omitted when empty). The agent echoes it on **every** AgentEvent it posts for that command — `received`, `progress`, and the terminal `completed_*` — so the backend authorizes the event by hashing the token. It round-trips through `command.Decode` automatically (embedded in `CommandHeader`) and is threaded onto every constructed `AgentEvent`: the workspace child's terminal + progress events, and supervisor-synthesized events (timeout / cap / unknown-kind failures, AgentCommand success). The agent never inspects or stores it.

## `received` EventKind

After claiming a command the supervisor posts `kind=received` as the first event. This cancels the backend's 30-second lease requeue (`claimed → delivered`). Best-effort: a POST failure is logged but does not prevent dispatch.

## Identity wire format

`POST /api/v1/agent/identity` body:

| Field | Type | Notes |
|---|---|---|
| `kind` | string | `"aws-sts"` (only value today) |
| `agent_version` | string | semver, optional |
| `agent_metadata` | `AgentMetadata` | `os`, `cpu_count`, `memory_bytes` — static; reported once at identity exchange |
| `payload` | string | JSON-encoded sigv4-signed STS envelope: `{url, headers, body}` |

Response:

| Field | Notes |
|---|---|
| `bearer` | 1-hour bearer token |
| `expires_at` | RFC3339 — agent must re-exchange before this |
| `renewal_after` | RFC3339 — suggested renewal time (5 min before `expires_at`) |
| `agent_id` | per-pod `workspace_agents.id` |
| `instance_id` | role-session-name from the STS ARN; backend-derived, never supplied by agent |
| `org_id` | org UUID |

## Entry points

- `types.go` — all wire structs, `CommandKind` constants, `CommandHeader`, event types, `AgentMetadata`, `IdentityExchangeRequest`, `IdentityExchangeResponse`.
- `client.go` — `Client`, `ExchangeIdentity`, `Heartbeat`, `ClaimCommand`, `PostCommandEvent`.
- `openapi_drift_test.go` — tag-conformance assertion; fails when a field name drifts from the spec.
