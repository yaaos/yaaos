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
- **Progress events do not resume the backend's run engine** — only `completed_*` events do. `RealHandler.RunClaude` streams stdout lines as `kind=progress` while accumulating the final result.
- **`RealHandler` writes a `.workspace-id` manifest** after clone so the startup reconciliation can reattribute orphan directories. See [workspace_lifecycle.md](workspace_lifecycle.md).
- **Checkout mode is decided by which of `ProvisionWorkspaceCommand.Repo.HeadSHA` / `.BranchName` is set.** `HeadSHA` set → detached pin (`git checkout --detach`) — fork-safe, the only mode a legacy caller with no `branch_name` exercises. `HeadSHA` empty, `BranchName` set → named work branch (`git checkout -B`), tracking the remote branch when it already exists, otherwise creating a fresh local branch off the clone's default-branch HEAD. See `gitClone` in `realhandler.go`.
- **`ProvisionWorkspace` sets the commit identity** (`git config user.name`/`user.email`) from `GitUserName`/`GitUserEmail` on the wire — backend-supplied constants, not agent policy. Best-effort no-op when both are empty; detached-checkout review flows never commit, so an unset identity there is harmless.
- **`PushBranch` re-points `origin` at a URL carrying the workspace's *current* auth token before pushing** (`pushURLWithCurrentToken`) — `RefreshWorkspaceAuth` only updates the in-memory slot, never the git remote's stored URL, so a push run right after a credential rotation must not fall back to a stale clone-time token. Requires HEAD to already be a named branch.
- **`workspacetest.StubHandler` is the test default** for supervisor-level tests via `supervisortest.InProcessSpawn(workspacetest.StubHandler{})`. It returns deterministic no-op success for every command kind.
- **`RunClaude` rejects an empty `cmd.SkillPath` explicitly, then stats a non-empty one before spawning anything.** Empty → deterministic `completed_failure` (`"skill not found: (empty skill_path)"`) without ever touching the filesystem — `filepath.Join(path, "")` would otherwise resolve to the checkout root, which always exists, silently defeating the check. Non-empty but absent → deterministic `completed_failure` (`"skill not found: <path>"`). Either way no subprocess launched — zero agent policy, the convention is backend-computed.
- **Artifact + exit-push ride every `InvokeClaudeCode` exit, success or failure.** `TMPDIR` is workspace-local so `$TMPDIR/<command_id>.md` dies with the workspace at `Cleanup`, not per-file. `readArtifact` enforces `artifactMaxBytes` atomically via `io.LimitReader` (not a separate stat-then-read, which would race a growing file); `maybePushOriginHead` pushes iff HEAD is a named branch. Both artifact fields ride the `AgentEvent`'s top-level `artifact`/`artifact_error` via `command.ArtifactResult`, populated on both `executeCommand` return paths — a push failure never masks a real artifact. See [architecture.md § Skill-path check + artifact collection + exit-push](architecture.md#skill-path-check--artifact-collection--exit-push).

## Gotchas

- `ctx` cancellation from the supervisor reaches `Run` via `runCancel()` in `inProcessRunner.Close`; blocking `Execute` calls must honour ctx or they keep the goroutine alive.
- `EmitterFromContext` is the only way to emit progress events from inside a handler; building a raw `AgentEvent` and writing it directly bypasses command-ID and traceparent stamping.

## Vocabulary

- **`WorkspaceOps`** — the capability seam `workspace.Run` calls for each command kind; production: `RealHandler`; tests: `workspacetest.StubHandler` or a custom implementation.
- **`CloneFunc`** — function type for git clone; `RealHandlerConfig` field; production default is `gitClone`.
- **`RunFunc`** — function type for Claude Code subprocess dispatch (`func(context.Context, RunStreamingOptions) (*RunStreamingResult, error)`); `RealHandlerConfig` field; production default is `RunStreaming`. Tests inject a fake to avoid spawning a real Claude binary.
- **`Run`** — the dispatcher loop entrypoint; one goroutine per workspace subprocess (or in-process equivalent in tests).
- **`inProcessRunner`** — test double wiring `workspace.Run` in a goroutine over `io.Pipe` pairs; used by `supervisortest.InProcessSpawn` in supervisor tests.
- **`command.ArtifactResult`** — optional interface (`ArtifactPayload() (*string, string)`) a command `Result` implements to carry a collected artifact; today only `command.InvokeResult`. `workspace.executeCommand` type-asserts against it to populate the `AgentEvent`'s top-level `artifact`/`artifact_error` fields, distinct from the `ToWire()` `Outputs` map.

## Entry points

- `apps/agent/internal/workspace/workspace.go` — `Run`, `executeCommand`.
- `apps/agent/internal/workspace/workspacetest/stub.go` — `StubHandler` (test-only, quarantined sub-package).
- `apps/agent/internal/workspace/realhandler.go` — `RealHandler`, `RealHandlerConfig`, `CloneFunc`, `RunFunc` type definitions.
- `apps/agent/internal/workspace/subprocess.go` — `RunStreaming`, `RunStreamingOptions`, `RunStreamingResult`.
- `apps/agent/internal/workspace/emitter.go` — `Emitter`, `EmitterFromContext`.
