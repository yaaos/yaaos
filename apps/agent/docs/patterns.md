# WorkspaceAgent — coding conventions

> Conventions for writing and extending Go packages inside `apps/agent`.

## Module boundary rule

Test helpers must not cross package boundaries. A helper used only by a package's own tests stays private to that package. Cross-package test setup is not used here — the agent has no shared test-helper surface.

## Adding a command kind

See [command.md § Adding a command kind](command.md#adding-a-command-kind) for the step-by-step. Short form:

1. Add a `CommandKind` constant to `internal/protocol/types.go`.
2. Add the wire struct (if new shape) to `internal/protocol/types.go`.
3. Add a result struct + `ToWire()` to `internal/command/results.go`.
4. Add the concrete command type + `Execute` to `internal/command/workspace_commands.go` or `agent_commands.go`.
5. Add one `case` to `command.Decode` in `internal/command/command.go`.
6. Add tests: Decode round-trip in `command_test.go`; Execute against a fake ops in `execute_test.go`.
