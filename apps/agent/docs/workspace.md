# workspace

> Per-workspace child process: dispatch loop, `WorkspaceOps` implementations, and progress-event emission.

## Scope

- **Owns:** the `Run` dispatch loop (reads commands from stdin pipe, writes events to stdout pipe), `RealHandler` (production `WorkspaceOps`), progress-event emitter.
- **Does not own:** subprocess spawning (`internal/supervisor`), wire-type definitions (`internal/protocol`), or command decoding policy (`internal/command`).
- **Receives:** framed JSON `WorkspaceCommand` bytes over stdin (`ipc.Decoder`).
- **Emits:** framed JSON `AgentEvent` bytes over stdout (`ipc.Encoder`); terminal events are `completed_success` or `completed_failure`; intermediate events are `progress`.
- **Hands to:** `command.WorkspaceOps` methods (one per command kind); `ipc.Encoder` for all outbound events.
- **Capability seams in `RealHandlerConfig`:** `CloneFunc` (git clone) and `RunFunc` (Claude Code subprocess). Both default to their production implementations; tests inject fakes. See [patterns.md § Testing](patterns.md#testing) for the fake-at-seam convention.

## Why / invariants

- **Single-threaded by design.** `Run` processes one command at a time, in-order. Concurrency lives at the supervisor level (one process per workspace, N workers in the pool).
- **Clean EOF = normal exit.** The supervisor closes the write end of the command pipe when it reaps the runner; `Run` exits nil on `ipc.ErrClosed`.
- **Progress events do not resume the backend's workflow engine** — only `completed_*` events do. `RealHandler.RunClaude` streams stdout lines as `kind=progress` while accumulating the final result.
- **`RealHandler` writes a `.workspace-id` manifest** after clone so the startup reconciliation can reattribute orphan directories. See [workspace_lifecycle.md](workspace_lifecycle.md).
- **`workspacetest.StubHandler` is the test default** for supervisor-level tests via `supervisortest.InProcessSpawn(workspacetest.StubHandler{})`. It returns deterministic no-op success for every command kind.

## Gotchas

- `ctx` cancellation from the supervisor reaches `Run` via `runCancel()` in `inProcessRunner.Close`; blocking `Execute` calls must honour ctx or they keep the goroutine alive.
- `EmitterFromContext` is the only way to emit progress events from inside a handler; building a raw `AgentEvent` and writing it directly bypasses command-ID and traceparent stamping.

## Vocabulary

- **`WorkspaceOps`** — the capability seam `workspace.Run` calls for each command kind; production: `RealHandler`; tests: `workspacetest.StubHandler` or a custom implementation.
- **`CloneFunc`** — function type for git clone; `RealHandlerConfig` field; production default is `gitClone`.
- **`RunFunc`** — function type for Claude Code subprocess dispatch (`func(context.Context, RunStreamingOptions) (*RunStreamingResult, error)`); `RealHandlerConfig` field; production default is `RunStreaming`. Tests inject a fake to avoid spawning a real Claude binary.
- **`Run`** — the dispatcher loop entrypoint; one goroutine per workspace subprocess (or in-process equivalent in tests).
- **`inProcessRunner`** — test double wiring `workspace.Run` in a goroutine over `io.Pipe` pairs; used by `supervisortest.InProcessSpawn` in supervisor tests.

## Entry points

- `apps/agent/internal/workspace/workspace.go` — `Run`, `executeCommand`.
- `apps/agent/internal/workspace/workspacetest/stub.go` — `StubHandler` (test-only, quarantined sub-package).
- `apps/agent/internal/workspace/realhandler.go` — `RealHandler`, `RealHandlerConfig`, `CloneFunc`, `RunFunc` type definitions.
- `apps/agent/internal/workspace/subprocess.go` — `RunStreaming`, `RunStreamingOptions`, `RunStreamingResult`.
- `apps/agent/internal/workspace/emitter.go` — `Emitter`, `EmitterFromContext`.
