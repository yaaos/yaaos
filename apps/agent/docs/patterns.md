# WorkspaceAgent — coding conventions

> Conventions for writing and extending Go packages inside `apps/agent`.

## Layer boundary rule

Imports flow downward only — `supervisor` → `{workspace, command, activity}` → `protocol` → leaves. The forbidden edges are CI-enforced by `depguard`; see `apps/agent/.golangci.yml` for the rule list and `apps/agent/docs/architecture.md` for the diagram. A compile error is the correct signal — don't add `nolint` comments.

## Module boundary rule

Test helpers must not cross package boundaries. A helper used only by a package's own tests stays private to that package. Cross-package test setup is not used here — the agent has no shared test-helper surface.

## Adding a command kind

See [command.md § Adding a command kind](command.md#adding-a-command-kind) for the step-by-step. Short form:

1. Add a `CommandKind` constant to `internal/protocol/types.go`.
2. Add the wire struct (if new shape) to `internal/protocol/types.go`.
3. Add a result struct + `ToWire()` to `internal/command/results.go`.
4. Add the concrete command type + `Execute` + `SetTraceparent` to `internal/command/workspace_commands.go` or `agent_commands.go`. `SetTraceparent` is a `Command`-interface method (sets the embedded `CommandHeader.Traceparent`); the compiler enforces it, so the supervisor's span-reparenting can never silently drop the new kind's traceparent.
5. Add one `case` to `command.Decode` in `internal/command/command.go`.
6. Add tests: Decode round-trip in `command_test.go`; Execute against a fake ops in `execute_test.go`.

The `exhaustive` linter (see below) will fail CI if the new case is missing from `Decode`. The traceparent rewrite needs no linter — a missing `SetTraceparent` is a compile error.

## Exhaustive enum switches

`exhaustive` (in `apps/agent/.golangci.yml`) guards every switch over a locally-defined enum type. Two switches are currently guarded:

- `command.Decode` in `internal/command/command.go` — switches over `CommandKind`. Its `default` case (returns an error on unknown kinds) satisfies `default-signifies-exhaustive: true` in the linter config.
- `activity.Conductor.HandleInbound` in `internal/activity/conductor.go` — switches over `InboundKind`. Both values must be cased explicitly.

If adding a new enum value causes `exhaustive` to fail, add the matching `case` — don't add a `//nolint` comment. If a `default` is intentional (open-ended error case), the linter config's `default-signifies-exhaustive: true` already covers it.

## Error taxonomy

- **Command error** → return `(nil, err)` from `Execute`; the supervisor maps it to `completed_failure` with `failure_reason`. Log at INFO at the handling boundary — it's an expected outcome.
- **Transport error** → log WARN at the retry site, sleep on the backoff schedule, retry indefinitely. Never crash in steady state.
- **Boot-time / config error** → return from `supervisor.Run`; `main` logs ERROR and exits. The orchestrator restarts.
- **Panic** → recovered at the claim-worker goroutine boundary, converted to `completed_failure`. Never surfaced as an unhandled panic.
- **Identity-integrity violation** — the one steady-state fatal: if bearer renewal returns a different `AgentID` or `OrgID`, the supervisor logs ERROR and calls `os.Exit(1)`. See [architecture.md § Error handling](architecture.md#error-handling--fatal-on-mismatch-carve-out).
- **Leaf packages** (`secret`, `ipc`, `backoff`, `command`) return errors; they never log. The caller logs at the handling boundary.

## Concurrency rules

- One root `context.Context` from the signal handler; `Supervisor.Run` owns every long-lived goroutine via one `WaitGroup`. `ctx.Done()` is the only shutdown signal.
- `Pool` guards all workspace-record mutations with a single `sync.Mutex`. No direct field access outside named mutators.
- `SubscriptionSet` and `WorkspaceMapping` each hold their own independent lock — don't lock one while holding the other.
- Per-claim-worker backoff schedules are independent — a backend blip doesn't lock-step all workers into a simultaneous retry.

## Logging dimensions

- All log records after identity exchange carry `org_id` and `agent_id` via the base logger (`slog.Default().With(...)`).
- Per-command dispatches add `workspace_id` and `command_id` to a child logger.
- Leaf packages take no logger; they return errors. Activity logs via the injected `Logger` interface on `Conductor`.
- Never log auth tokens or `Secret` values. Use `.Value()` only at the OTLP-exporter install site.

## Depguard layer rule

The full rule set lives in `apps/agent/.golangci.yml`. When adding a new internal package, decide which layer it belongs to (leaf, `activity`/`command`/`workspace`, or `supervisor`) and add the corresponding deny rules. Running `golangci-lint run` after the addition confirms placement.
