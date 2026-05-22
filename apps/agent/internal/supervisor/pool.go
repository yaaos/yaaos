// Per-workspace runner pool. The supervisor receives a stream of
// AgentCommands from the backend (interleaved across workspaces by the
// claim-loop fan-in); the pool routes each command to the right
// long-lived `agent workspace` child process based on `workspace_id`,
// spawning a new one on the first command, serializing commands per
// workspace, and reaping the runner after `CleanupWorkspace`.
//
// Two runner implementations ship:
//
//   - `execSpawn` (production) — fork+exec of `os.Args[0] workspace`
//     with parent-side pipes for stdin (commands) and stdout (events);
//     close = SIGTERM then SIGKILL after a grace.
//   - `inProcessSpawn` (tests) — runs `workspace.Run` in a goroutine
//     against `io.Pipe` pairs; close = close the pipes so Run exits.
//
// Both produce values that satisfy `WorkspaceRunner`. The pool itself
// doesn't care which one it's holding.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace"
)

// WorkspaceRunner represents one running workspace process. `Send` writes a
// command on the parent → child pipe and blocks until the child writes one
// event back on the child → parent pipe. Concurrent `Send` calls to the
// same runner are NOT safe — the pool serializes them. `Close` tears the
// runner down (signal/grace/kill for an OS process, pipe-close for
// in-process).
type WorkspaceRunner interface {
	Send(ctx context.Context, cmd *protocol.AgentCommand) (protocol.AgentEvent, error)
	Close(ctx context.Context) error
}

// SpawnFunc creates a new runner for the given workspace id. The pool
// calls this exactly once per workspace (on the `CreateWorkspace` arrival).
type SpawnFunc func(ctx context.Context, workspaceID string) (WorkspaceRunner, error)

// Pool tracks one runner per workspace_id, serializes commands per
// workspace, and reaps the runner after `CleanupWorkspace`.
type Pool struct {
	spawn    SpawnFunc
	log      Logger
	timeouts PoolTimeouts

	mu      sync.Mutex
	runners map[string]*workspaceSlot
}

// PoolTimeouts caps how long each command kind can sit inside the
// workspace process before the pool gives up + emits completed_failure.
// InvokeClaudeCode reads `Limits.WallclockSeconds` off the wire when
// present (the control plane owns that knob per CLAUDE.md "every
// threshold/timeout comes from the control plane via payload"); the
// other kinds use these Go-side defaults until they grow per-command
// wire fields of their own. Zero values pick conservative defaults.
type PoolTimeouts struct {
	CreateWorkspace      time.Duration // default 5m (git clone may be slow)
	WriteFiles           time.Duration // default 30s
	RefreshWorkspaceAuth time.Duration // default 30s
	CleanupWorkspace     time.Duration // default 30s
	// InvokeClaudeCodeFallback applies when the command omits
	// Limits.WallclockSeconds (shouldn't happen in production — the
	// backend always sets it — but defensive defaults beat hangs).
	// Default: 15m.
	InvokeClaudeCodeFallback time.Duration
}

func (t PoolTimeouts) withDefaults() PoolTimeouts {
	if t.CreateWorkspace == 0 {
		t.CreateWorkspace = 5 * time.Minute
	}
	if t.WriteFiles == 0 {
		t.WriteFiles = 30 * time.Second
	}
	if t.RefreshWorkspaceAuth == 0 {
		t.RefreshWorkspaceAuth = 30 * time.Second
	}
	if t.CleanupWorkspace == 0 {
		t.CleanupWorkspace = 30 * time.Second
	}
	if t.InvokeClaudeCodeFallback == 0 {
		t.InvokeClaudeCodeFallback = 15 * time.Minute
	}
	return t
}

// timeoutForCommand returns the per-command deadline duration. For
// InvokeClaudeCode, prefers the wire-supplied Limits.WallclockSeconds;
// otherwise falls back to the per-kind default.
func (t PoolTimeouts) timeoutForCommand(cmd *protocol.AgentCommand) time.Duration {
	switch cmd.Kind {
	case protocol.KindCreateWorkspace:
		return t.CreateWorkspace
	case protocol.KindWriteFiles:
		return t.WriteFiles
	case protocol.KindRefreshWorkspaceAuth:
		return t.RefreshWorkspaceAuth
	case protocol.KindCleanupWorkspace:
		return t.CleanupWorkspace
	case protocol.KindInvokeClaudeCode:
		if cmd.InvokeClaudeCode != nil && cmd.InvokeClaudeCode.Limits.WallclockSeconds > 0 {
			return time.Duration(cmd.InvokeClaudeCode.Limits.WallclockSeconds) * time.Second
		}
		return t.InvokeClaudeCodeFallback
	default:
		return t.InvokeClaudeCodeFallback
	}
}

// workspaceSlot pairs a runner with a per-workspace mutex. The pool grabs
// the outer mu briefly to find/insert the slot, then releases it so other
// workspaces can dispatch in parallel; the per-slot mu serializes Sends
// to the same workspace.
type workspaceSlot struct {
	runner WorkspaceRunner
	mu     sync.Mutex
}

// NewPool constructs an empty pool. `spawn` is invoked on the first
// command for a previously unseen workspace_id. Uses default
// PoolTimeouts; call `NewPoolWithTimeouts` to override.
func NewPool(spawn SpawnFunc, log Logger) *Pool {
	return NewPoolWithTimeouts(spawn, log, PoolTimeouts{})
}

// NewPoolWithTimeouts constructs a pool with custom per-kind timeouts.
// Zero-value fields in `timeouts` get conservative defaults; explicit
// values win.
func NewPoolWithTimeouts(spawn SpawnFunc, log Logger, timeouts PoolTimeouts) *Pool {
	if log == nil {
		log = nullLogger{}
	}
	return &Pool{
		spawn:    spawn,
		log:      log,
		timeouts: timeouts.withDefaults(),
		runners:  make(map[string]*workspaceSlot),
	}
}

// Dispatch routes one command to the right runner and returns the event
// the runner emits. If the runner doesn't exist yet and the command is
// `CreateWorkspace`, the pool spawns it. Any other kind for an unknown
// workspace is a protocol violation — Dispatch returns a synthetic
// `completed_failure` event so the supervisor can still ack the command.
//
// After a `CleanupWorkspace` command the runner is closed + removed from
// the pool regardless of whether the runner reported success or failure.
func (p *Pool) Dispatch(ctx context.Context, cmd *protocol.AgentCommand) protocol.AgentEvent {
	header := cmd.Header()
	workspaceID := header.WorkspaceID
	if workspaceID == "" {
		return failureEvent(header, "missing workspace_id")
	}

	p.mu.Lock()
	slot, ok := p.runners[workspaceID]
	if !ok {
		if cmd.Kind != protocol.KindCreateWorkspace {
			p.mu.Unlock()
			return failureEvent(header, fmt.Sprintf("no workspace runner for %s (kind=%s)", workspaceID, cmd.Kind))
		}
		runner, err := p.spawn(ctx, workspaceID)
		if err != nil {
			p.mu.Unlock()
			p.log.Error("pool.spawn_failed", "workspace_id", workspaceID, "err", err.Error())
			return failureEvent(header, "spawn: "+err.Error())
		}
		slot = &workspaceSlot{runner: runner}
		p.runners[workspaceID] = slot
		p.log.Info("pool.workspace_spawned", "workspace_id", workspaceID)
	}
	p.mu.Unlock()

	slot.mu.Lock()
	defer slot.mu.Unlock()

	// Per-command wall-clock cap. InvokeClaudeCode honours the wire's
	// Limits.WallclockSeconds; other kinds use the pool's Go-side
	// defaults (overridable via NewPoolWithTimeouts).
	timeout := p.timeouts.timeoutForCommand(cmd)
	sendCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	ev, err := slot.runner.Send(sendCtx, cmd)
	if err != nil {
		// IO failure / runner died / outer context cancel / per-command
		// timeout. Drop the slot so a subsequent CreateWorkspace can
		// respawn; emit completed_failure with a kind-specific reason so
		// the backend's audit log can tell timeouts apart from outer
		// cancellation or runner crashes. The distinguishing signal: a
		// per-command timeout fires when sendCtx hits DeadlineExceeded
		// while the outer ctx is still alive; outer cancel cancels both.
		reason := "runner: " + err.Error()
		if errors.Is(sendCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil {
			reason = fmt.Sprintf("timeout: %s exceeded %s wall-clock", cmd.Kind, timeout)
		}
		p.dropSlot(workspaceID)
		_ = slot.runner.Close(context.Background())
		p.log.Warn("pool.send_failed", "workspace_id", workspaceID, "kind", string(cmd.Kind), "err", err.Error())
		return failureEvent(header, reason)
	}
	if cmd.Kind == protocol.KindCleanupWorkspace {
		// Contract: CleanupWorkspace is always terminal — reap the runner.
		p.dropSlot(workspaceID)
		if cerr := slot.runner.Close(context.Background()); cerr != nil {
			p.log.Warn("pool.close_failed", "workspace_id", workspaceID, "err", cerr.Error())
		} else {
			p.log.Info("pool.workspace_closed", "workspace_id", workspaceID)
		}
	}
	return ev
}

// CloseAll terminates every runner. Called on supervisor shutdown.
func (p *Pool) CloseAll(ctx context.Context) {
	p.mu.Lock()
	slots := make([]*workspaceSlot, 0, len(p.runners))
	ids := make([]string, 0, len(p.runners))
	for id, slot := range p.runners {
		slots = append(slots, slot)
		ids = append(ids, id)
	}
	p.runners = make(map[string]*workspaceSlot)
	p.mu.Unlock()

	for i, slot := range slots {
		if err := slot.runner.Close(ctx); err != nil {
			p.log.Warn("pool.close_all_failed", "workspace_id", ids[i], "err", err.Error())
		}
	}
}

func (p *Pool) dropSlot(workspaceID string) {
	p.mu.Lock()
	delete(p.runners, workspaceID)
	p.mu.Unlock()
}

func failureEvent(header protocol.CommandHeader, reason string) protocol.AgentEvent {
	return protocol.AgentEvent{
		CommandID:     header.CommandID,
		Kind:          protocol.EventCompletedFailure,
		FailureReason: reason,
		Traceparent:   header.Traceparent,
		ReportedAt:    time.Now().UTC(),
	}
}

// ── In-process runner (test default) ────────────────────────────────────

// inProcessRunner wraps `workspace.Run` in a goroutine fed by io.Pipe pairs.
// `Send` writes one framed AgentCommand on the command pipe and reads one
// framed AgentEvent off the event pipe. Use this in tests so the dispatch
// frame is exercised end-to-end without OS-process spawning.
type inProcessRunner struct {
	cmdW *io.PipeWriter
	evR  *io.PipeReader

	cmdR *io.PipeReader // kept so the goroutine sees EOF when we close
	evW  *io.PipeWriter

	enc *ipc.Encoder
	dec *ipc.Decoder

	runCancel context.CancelFunc // cancels the workspace.Run + its handler ctx
	done      chan struct{}
}

// InProcessSpawn returns a SpawnFunc that runs `workspace.Run(handler)` in
// a goroutine connected by io.Pipe pairs. The default handler is the
// workspace package's `StubHandler` — tests can pass a custom one.
func InProcessSpawn(handler workspace.Handler) SpawnFunc {
	if handler == nil {
		handler = workspace.StubHandler{}
	}
	return func(ctx context.Context, _ string) (WorkspaceRunner, error) {
		cmdR, cmdW := io.Pipe()
		evR, evW := io.Pipe()
		runCtx, runCancel := context.WithCancel(ctx)
		runner := &inProcessRunner{
			cmdW:      cmdW,
			evR:       evR,
			cmdR:      cmdR,
			evW:       evW,
			enc:       ipc.NewEncoder(cmdW),
			dec:       ipc.NewDecoder(evR),
			runCancel: runCancel,
			done:      make(chan struct{}),
		}
		go func() {
			defer close(runner.done)
			_ = workspace.Run(runCtx, cmdR, evW, handler, workspace.Options{})
			_ = evW.Close() // signal EOF to the parent decoder
		}()
		return runner, nil
	}
}

func (r *inProcessRunner) Send(ctx context.Context, cmd *protocol.AgentCommand) (protocol.AgentEvent, error) {
	wireCmd, err := encodeCommand(cmd)
	if err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("encode command: %w", err)
	}
	if err := r.enc.Write(wireCmd); err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("write command: %w", err)
	}
	// Honour ctx by racing the read against ctx.Done. Closing the event
	// pipe wakes a blocked Scan; we close on context cancel below.
	resultCh := make(chan readResult, 1)
	go func() {
		var ev protocol.AgentEvent
		err := r.dec.Read(&ev)
		resultCh <- readResult{ev: ev, err: err}
	}()
	select {
	case <-ctx.Done():
		// Close the read end so the goroutine unblocks; surface ctx err.
		_ = r.evR.CloseWithError(ctx.Err())
		<-resultCh
		return protocol.AgentEvent{}, ctx.Err()
	case res := <-resultCh:
		if res.err != nil {
			return protocol.AgentEvent{}, fmt.Errorf("read event: %w", res.err)
		}
		return res.ev, nil
	}
}

type readResult struct {
	ev  protocol.AgentEvent
	err error
}

func (r *inProcessRunner) Close(_ context.Context) error {
	// Two-step shutdown. Closing cmdW makes the workspace's Decoder.Read
	// return ipc.ErrClosed if it's currently blocked there. Cancelling the
	// run-ctx unblocks any handler that's parked on ctx.Done — without
	// this, a hanging handler (e.g. a long-running Claude Code invocation)
	// keeps the goroutine alive forever.
	_ = r.cmdW.Close()
	r.runCancel()
	select {
	case <-r.done:
	case <-time.After(2 * time.Second):
		// Belt + braces: forcibly tear down both pipe ends. Sufficient to
		// unblock any remaining read/write inside workspace.Run.
		_ = r.cmdR.Close()
		_ = r.evW.Close()
		<-r.done
	}
	return nil
}

// encodeCommand serializes the AgentCommand union into the flat wire shape
// the workspace expects. The wrapper's UnmarshalJSON peeks at `kind` and
// dispatches; for the encode direction we just write the concrete payload
// (the pointer field that's set).
func encodeCommand(cmd *protocol.AgentCommand) (any, error) {
	switch cmd.Kind {
	case protocol.KindCreateWorkspace:
		return cmd.CreateWorkspace, nil
	case protocol.KindWriteFiles:
		return cmd.WriteFiles, nil
	case protocol.KindRefreshWorkspaceAuth:
		return cmd.RefreshWorkspaceAuth, nil
	case protocol.KindInvokeClaudeCode:
		return cmd.InvokeClaudeCode, nil
	case protocol.KindCleanupWorkspace:
		return cmd.CleanupWorkspace, nil
	default:
		return nil, errors.New("unknown command kind")
	}
}
