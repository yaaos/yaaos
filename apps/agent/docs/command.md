# internal/command

> Polymorphic command substrate: interfaces, concrete types, capability seams, typed results, and the single Decode factory.

## Scope

**Owns:**
- `Command` interface — the polymorphic root every command kind implements.
- Two command families: `WorkspaceCommand` (5 kinds, executed in workspace children) and `AgentCommand` (1 kind today, executed in the supervisor).
- `WorkspaceOps` and `AgentOps` capability seams — caller-owned interfaces the command types call at execution time.
- Typed result structs (`CreateResult`, `WriteFilesResult`, `RefreshResult`, `InvokeResult`, `CleanupResult`, `ConfigUpdateResult`) + `ExecResult` for subprocess outcomes.
- `AgentConfig` — the typed config struct `ConfigUpdateCommand` carries.
- `Decode(raw []byte) (Command, error)` — the one surviving kind-switch (factory).

**Does not own:**
- Wire DTOs, JSON tags, `CommandKind` constants — those stay in `internal/protocol` (the wire-DTO leaf).
- Real I/O: no OS syscalls, no HTTP, no subprocess spawning. Those are in the ops implementations.
- Logging — `command` returns errors; callers log at the handling boundary.

**Boundary:**
- Receives: raw JSON bytes (from the supervisor, via `Decode`).
- Emits: a `Command` value; callers call `.Execute(ctx, ops)` to run it.
- Imports: `internal/protocol` (wire structs + kind constants), `internal/secret` (OTLPToken redaction). No upward imports — this package is below `supervisor` and `workspace`.

## Why / invariants

- **One kind-switch, in Decode.** You must peek `kind` to know which concrete type to unmarshal into — that is unavoidable. Every other switch that existed (`workspace.dispatch`, `pool.timeoutForCommand`, `supervisor.routeCommand`) is replaced by method dispatch on the interface.
- **`WorkspaceOps` / `AgentOps` are caller-owned seams** — the command types call the seam; the workspace or supervisor provides the implementation. This keeps the command package free of real I/O and makes unit-testing trivial (supply a fake ops).
- **Timeouts live on the command type.** `InvokeClaudeCode.Timeout()` prefers `Limits.WallclockSeconds` from the wire (the control plane sets it per invocation); all other kinds use Go-side defaults.
- **Typed results, map wire.** `Execute` returns a typed `Result`; `ToWire()` produces the `map[string]any` that `AgentEvent.Outputs` carries. The backend wire contract stays `map[string]any`; the Go `command`↔`supervisor` boundary is fully typed.
- **`ConfigUpdateCommand` is an `AgentCommand`, not a `WorkspaceCommand`.** It runs in the supervisor via `AgentOps.ApplyConfig` — never dispatched to a workspace child.

## Gotchas

- `AgentCommand` (the interface here) is unrelated to the `protocol.AgentCommand` union struct. Same term, different concept.
- `CreateResult.Path` carries the workspace path the supervisor registry keys on; don't rename it.
- `InvokeResult.ToWire()` includes both `stdout` (full, for the backend's CodeReview parser) and `stdout_excerpt` (display-friendly, truncated at 16 KiB). Both keys are load-bearing — the backend reads `stdout`, operators read `stdout_excerpt`.
- `secret.Secret` fields (`AgentConfig.OTLPToken`) print as `[REDACTED]` under all fmt/json paths. Use `.Value()` only at the OTLP-exporter install site.

## Vocabulary

- **Command** — a unit of work from the control plane, modeled as a typed Go value rather than a raw JSON union.
- **WorkspaceCommand** — a command that executes in a workspace child process against `WorkspaceOps`.
- **AgentCommand** — a command that executes in the supervisor against `AgentOps`.
- **Ops seam** — a caller-owned capability interface (`WorkspaceOps` / `AgentOps`) the command calls at execution time.
- **Decode** — the factory: peek `kind`, unmarshal into concrete type, return as `Command`.
- **Result** — a typed struct whose `ToWire()` produces `AgentEvent.Outputs`.

## Entry points

- `command.go` — `Command`/`WorkspaceCommand`/`AgentCommand` interfaces + `Decode` factory.
- `workspace_commands.go` — the 5 `WorkspaceCommand` types + `Execute` bodies.
- `agent_commands.go` — `ConfigUpdateCommand` + `AgentConfig`.
- `results.go` — result structs + `ToWire()` + `ExecResult`.
- `ops.go` — `WorkspaceOps` + `AgentOps` interfaces.

## Adding a command kind

1. Decide family: `WorkspaceCommand` (workspace child) or `AgentCommand` (supervisor).
2. Add a `CommandKind` constant to `internal/protocol/types.go`.
3. If the wire shape is new, add the typed struct to `internal/protocol/types.go`.
4. Add a result struct + `ToWire()` to `results.go`.
5. Add the concrete command type + `Execute` to `workspace_commands.go` or `agent_commands.go`.
6. Add one `case` to `Decode` in `command.go`.
7. The compiler lists any unimplemented interface methods.
8. Add tests in `command_test.go` (Decode round-trip) and `execute_test.go` (Execute against a fake ops).
