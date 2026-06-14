# WorkspaceAgent — coding conventions

> Conventions for writing and extending Go packages inside `apps/agent`.

## Layer boundary rule

Imports flow downward only — `supervisor` → `{workspace, command, activity}` → `protocol` → leaves. Permitted edges are CI-enforced by `depguard`; see `apps/agent/.golangci.yml` for the rule list and `apps/agent/docs/architecture.md` for the diagram. A compile error is the correct signal — don't add `nolint` comments.

## Module boundaries

- Each directory directly under `internal/` is one module. Each has its own depguard rule in `.golangci.yml`.
- A module's public interface = the capitalized identifiers in its non-test `.go` files. No separate manifest.
- Adding a new internal import requires extending the importer's `allow:` list in `.golangci.yml`. CI fails otherwise.
- Cross-module test fixtures live in `<module>/<module>test/` sub-packages (e.g. `workspace/workspacetest/`). depguard forbids production `.go` files from importing them.
- `_test.go` files are unreachable cross-package by Go itself; no extra rules needed.

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

## Observability

### Span instrumentation

`tracing.StartSpan` is the sole span primitive for agent code. Use it at every meaningful IO boundary (clone, subprocess launch, identity exchange, claim HTTP call, WS dial). Never wrap a loop iteration — one span per call, not per iteration.

**Rule:** always call `end(err)` with the function's returned error. If the function doesn't return `error` (e.g. `runOneRefreshCycle`), capture failures into a local `spanErr` variable and pass it to `end` via a `defer`. Never call `end(nil)` when an error occurred.

**Sanctioned no-span locations** (per-loop and per-message are anti-patterns):

- The `claimLoop` iteration itself — only the `ClaimCommand` HTTP call inside it gets a span.
- Individual WS message reads in the activity read-loop — only the `dialAndStartWS` dial gets a span.

Full span inventory → [architecture.md § Observability](architecture.md#observability).

Grep recipe: `rg -n "tracing.StartSpan" apps/agent/internal/`

## Depguard layer rule

The full rule set lives in `apps/agent/.golangci.yml`. When adding a new internal package, decide which layer it belongs to (leaf, `activity`/`command`/`workspace`, or `supervisor`) and add a `list-mode: strict` rule with the appropriate `allow:` list. See the **Module boundaries** section above. Running `golangci-lint run` after the addition confirms placement.

## Testing

> The agent's tests are pure-stdlib, fake-driven at capability seams, with a per-layer map answering "what test, what double, where."

1. **Pure standard library.** `testing` package + hand-written `if got != want { t.Errorf }`. No testify, gomock, or moq.
2. **Fakes at capability seams, never mocks.** Hand-written fakes at `WorkspaceOps`, `AgentOps`, `identity.Provider`, `CloneFunc`, `RunFunc` are the idiom. A fake is a working stand-in asserted on *state*; a mock records call-expectations on *behavior*. Mocks are not used.
3. **Doubles by composition.** Embed `workspacetest.StubHandler`, override the one method under test.
4. **Table-driven + `t.Run` subtests** when ≥ 3 cases exercise one behavior.
5. **`httptest.Server` for transport.** A real in-process HTTP/WS server, not a stub, at HTTP and WebSocket boundaries.
6. **`testing/synctest` for time.** Virtual-time bubble; never `time.Sleep` polling; never a hand-rolled clock interface.
7. **`-race` per concurrency invariant.** A dedicated contention test for each invariant (cap, same-id, backoff, dedup, child-watcher). Reviewer-gated convention, not CI-enforced; the race detector already runs suite-wide in `bin/ci`.
8. **Hygiene.** `t.Helper()` in helpers; `t.Cleanup` over `defer`.
9. **White-box by default; black-box for public contracts.** `package x_test` form for the public/wire contract (as `command_test` already does); white-box (`package x`) everywhere else.
10. **`openapi_drift_test.go` is the cross-plane contract guard.** The named mechanism keeping Go↔Python wire-coherent; see `internal/protocol/openapi_drift_test.go` and [architecture.md § Testing model](architecture.md#testing-model).
11. **No cross-package test helpers.** Follows the module-boundary rule above; `testing/synctest` needs no shared fake-clock helper, so the rule holds.

### Per-layer map

| Layer | Test | Double | Lives in |
|---|---|---|---|
| Command logic | decode round-trip + `Execute` | fake `WorkspaceOps` / `AgentOps` | `command/*_test.go` |
| Registry / lifecycle / pool | concurrency invariants | in-process `Pool` + `workspacetest.StubHandler` via `supervisortest.InProcessSpawn`, `-race` | `supervisor/*_test.go` |
| Transport (client / identity / activity WS) | wire round-trip, auth, reconnect | `httptest.Server` | `protocol/`, `supervisor/`, `activity/` |
| Child subprocess | orchestration + signal/pipe mechanics | fake `RunFunc` + `TestHelperProcess` | `workspace/` |
| Timing loops | interval / expiry / backoff | `testing/synctest` bubble | `backoff/`, `activity/`, `supervisor/` |
| Wire contract | Go↔Python schema parity | reflection drift test | `protocol/openapi_drift_test.go` |
