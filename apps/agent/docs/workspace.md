# workspace

> Per-workspace child process: dispatch loop, `WorkspaceOps` implementations, and progress-event emission.

## Scope

- **Owns:** the `Run` dispatch loop (reads commands from stdin pipe, writes events to stdout pipe), `RealHandler` (production `WorkspaceOps`), `StubHandler` (test no-op), progress-event emitter.
- **Does not own:** subprocess spawning (`internal/supervisor`), wire-type definitions (`internal/protocol`), or command decoding policy (`internal/command`).
- **Receives:** framed JSON `WorkspaceCommand` bytes over stdin (`ipc.Decoder`).
- **Emits:** framed JSON `AgentEvent` bytes over stdout (`ipc.Encoder`); terminal events are `completed_success` or `completed_failure`; intermediate events are `progress`.
- **Hands to:** `command.WorkspaceOps` methods (one per command kind); `ipc.Encoder` for all outbound events.

## Why / invariants

- **Single-threaded by design.** `Run` processes one command at a time, in-order. Concurrency lives at the supervisor level (one process per workspace, N workers in the pool).
- **Clean EOF = normal exit.** The supervisor closes the write end of the command pipe when it reaps the runner; `Run` exits nil on `ipc.ErrClosed`.
- **Progress events do not resume the backend's workflow engine** — only `completed_*` events do. `RealHandler.RunClaude` streams stdout lines as `kind=progress` while accumulating the final result.
- **`RealHandler` writes a `.workspace-id` manifest** after clone so the startup reconciliation can reattribute orphan directories. See [workspace_lifecycle.md](workspace_lifecycle.md).
- **`StubHandler` is the test default** for supervisor-level tests via `InProcessSpawn(workspace.StubHandler{})`. It returns deterministic no-op success for every command kind.

## Gotchas

- `ctx` cancellation from the supervisor reaches `Run` via `runCancel()` in `inProcessRunner.Close`; blocking `Execute` calls must honour ctx or they keep the goroutine alive.
- `EmitterFromContext` is the only way to emit progress events from inside a handler; building a raw `AgentEvent` and writing it directly bypasses command-ID and traceparent stamping.

## Vocabulary

- **`WorkspaceOps`** — the capability seam `workspace.Run` calls for each command kind; production: `RealHandler`; tests: `StubHandler` or a custom implementation.
- **`Run`** — the dispatcher loop entrypoint; one goroutine per workspace subprocess (or in-process equivalent in tests).
- **`inProcessRunner`** — test double wiring `workspace.Run` in a goroutine over `io.Pipe` pairs; used by `InProcessSpawn` in supervisor tests.

## Entry points

- `apps/agent/internal/workspace/workspace.go` — `Run`, `executeCommand`, `StubHandler`.
- `apps/agent/internal/workspace/realhandler.go` — `RealHandler`.
- `apps/agent/internal/workspace/emitter.go` — `Emitter`, `EmitterFromContext`.
