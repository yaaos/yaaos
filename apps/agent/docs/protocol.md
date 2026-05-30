# internal/protocol

> Wire-DTO leaf package: JSON types matching the backend OpenAPI spec, `CommandKind` constants, and the HTTP client — no business logic, no decode dispatch.

## Scope

**Owns:**
- All concrete command wire structs: `CreateWorkspaceCommand`, `WriteFilesCommand`, `RefreshWorkspaceAuthCommand`, `InvokeClaudeCodeCommand`, `CleanupWorkspaceCommand`.
- `CommandHeader` — embedded in every concrete command; carries `command_id`, `workspace_id`, `traceparent`, `kind`.
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

## Gotchas

- Do not add union-dispatch or `UnmarshalJSON` override here. Union dispatch was removed; the sole decode path is `command.Decode`.
- `InvokeClaudeCodeCommand.Invocation` is `json.RawMessage` — the agent passes it through without parsing; the backend owns the invocation schema.
- `ClaimCommand` returns `[]byte`, not a typed struct. The caller (`supervisor.claimLoop`) passes the bytes to `command.Decode`. Do not change the return type to a concrete struct — doing so would require `protocol` to import `command`, breaking the layer graph.

## Vocabulary

- **Wire struct** — a Go struct whose JSON tags exactly match the backend OpenAPI spec fields for one command kind or event.
- **CommandHeader** — the three routing fields every command carries: `command_id`, `workspace_id`, `traceparent`, `kind`.
- **Leaf** — a package with no internal imports; safe for any layer to import without cycles.

## Entry points

- `types.go` — all wire structs, `CommandKind` constants, `CommandHeader`, event types.
- `client.go` — `Client`, `ExchangeIdentity`, `Heartbeat`, `ClaimCommand`, `PostCommandEvent`.
- `openapi_drift_test.go` — tag-conformance assertion; fails when a field name drifts from the spec.
