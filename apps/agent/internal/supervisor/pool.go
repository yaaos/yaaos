// Per-workspace runner pool with unified registry.
//
// The supervisor receives a stream of commands from the backend (interleaved
// across workspaces by the claim-loop fan-in); the pool routes each command
// to the right long-lived `agent workspace` child process based on
// `workspace_id`, spawning a new one on the first CreateWorkspace command,
// serializing commands per workspace, and reaping the runner after
// CleanupWorkspace.
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
//
// Registry:
//
// The Pool owns a single registry (map[workspace_id]*workspaceRecord) guarded
// by its mutex. Each record carries a liveness state (`WorkspaceState`) and
// an orthogonal busy-ness field (`current_command_id`). State transitions
// are the only way to change a record's state — no free-form field writes.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// WorkspaceRunner represents one running workspace process. `Send` writes
// a command on the parent → child pipe and reads events back on the child
// → parent pipe in a loop. Progress events (`kind=progress`) fire the
// `onProgress` callback synchronously and the loop continues; the first
// `kind=completed_*` event ends the loop and is returned.
//
// `onProgress` may be nil; in that case progress events are silently
// dropped. The callback runs on the same goroutine that's reading the
// pipe, so a slow callback backpressures the workspace subprocess —
// callers should keep it cheap (push-to-queue is the canonical shape).
//
// Concurrent `Send` calls to the same runner are NOT safe — the pool
// serializes them. `Close` tears the runner down (signal/grace/kill for
// an OS process, pipe-close for in-process).
type WorkspaceRunner interface {
	Send(ctx context.Context, cmd command.WorkspaceCommand, onProgress func(protocol.AgentEvent)) (protocol.AgentEvent, error)
	Close(ctx context.Context) error
}

// SpawnFunc creates a new runner for the given workspace id. The pool
// calls this exactly once per workspace (on the `CreateWorkspace` arrival).
type SpawnFunc func(ctx context.Context, workspaceID string) (WorkspaceRunner, error)

// WorkspaceState is the liveness axis of a registry record.
// It is orthogonal to the busy-ness axis (current_command_id).
type WorkspaceState int

const (
	// StateActive — the workspace subprocess is running and accepting commands.
	StateActive WorkspaceState = iota
	// StateDefunct — the subprocess exited unexpectedly; the record is kept
	// in the registry until reaped so the disk sweep does not remove its dir.
	StateDefunct
	// StateOrphaned — leftover directory from a prior run, discovered at startup.
	// Runner is nil; the backend decides whether to signal cleanup.
	StateOrphaned
)

// ErrUnknownWorkspace is returned by Dispatch when a non-create command
// arrives for a workspace_id with no registry record.
var ErrUnknownWorkspace = errors.New("no workspace runner")

// ErrAtCap is returned by reserveActiveSlot when the Active-record count
// has reached the configured max_workspaces limit.
var ErrAtCap = errors.New("cap reached")

// errSlotTaken is returned by reserveActiveSlot when another concurrent
// CreateWorkspace dispatch already reserved (or owns) an Active record for
// the same workspace_id. The loser of that race must not spawn a second
// runner — it falls through to using the winner's record.
var errSlotTaken = errors.New("active slot already reserved")

// workspaceRecord is one entry in the pool's registry.
// It is guarded by Pool.mu for state/path/currentCommandID writes
// and by its own slotMu for serializing Send calls to the same runner.
type workspaceRecord struct {
	state            WorkspaceState
	path             string // on-disk workspace directory; set after CreateWorkspace
	currentCommandID string // "" when idle
	runner           WorkspaceRunner
	slotMu           sync.Mutex // serializes Send calls to this runner
}

// heartbeatStatus projects the liveness state onto the protocol status string.
func (r *workspaceRecord) heartbeatStatus() string {
	switch r.state {
	case StateActive:
		return "running"
	case StateDefunct:
		return "exited"
	case StateOrphaned:
		return "unknown"
	default:
		return "unknown"
	}
}

// Pool tracks one registry record per workspace_id, serializes commands per
// workspace, and reaps the runner after `CleanupWorkspace`.
type Pool struct {
	spawn SpawnFunc
	log   Logger

	mu       sync.Mutex
	registry map[string]*workspaceRecord
}

// NewPool constructs an empty pool. `spawn` is invoked on the first command
// for a previously unseen workspace_id. Per-command deadlines come from each
// command's own Timeout() (wire-supplied for InvokeClaudeCode, Go-side
// defaults for the rest) — the pool holds no timeout configuration.
func NewPool(spawn SpawnFunc, log Logger) *Pool {
	if log == nil {
		log = nullLogger{}
	}
	return &Pool{
		spawn:    spawn,
		log:      log,
		registry: make(map[string]*workspaceRecord),
	}
}

// ── Named mutators (state transitions) ─────────────────────────────────────
//
// Each mutator is the only way to reach its target state. Pool.mu must be
// held by the caller OR the mutator acquires it internally — see each
// function's comment. Callers that already hold Pool.mu call the *Locked
// variant; external callers (including tests) call the public form.

// createActive inserts a new Active record with the provided runner.
// runner may be nil when the spawn hasn't completed yet (Dispatch sets it
// after spawn returns). Pool.mu is acquired internally.
func (p *Pool) createActive(id string, runner WorkspaceRunner) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.registry[id] = &workspaceRecord{
		state:  StateActive,
		runner: runner,
	}
}

// reserveActiveSlot atomically claims an Active slot for a CreateWorkspace
// dispatch. The existence check, cap check, and placeholder insert all happen
// under a single Pool.mu critical section — concurrent same-id creates cannot
// both pass, and concurrent creates across ids cannot both pass a stale count.
//
// The inserted record's runner is nil (a placeholder); the caller spawns the
// runner outside the lock and calls assignRunner to fill it in. Until then the
// record is NOT yet sendable — readers gate on runner == nil so no command can
// observe a record whose runner is still nil (see Dispatch).
//
// Returns:
//   - nil          → this caller reserved the slot and must spawn + assign.
//   - errSlotTaken → another concurrent create already owns/reserved an Active
//     record for this id; the caller must NOT spawn (avoids orphaning a runner).
//   - ErrAtCap     → the Active-record count is at maxWorkspaces.
//
// maxWorkspaces <= 0 means no cap (unlimited).
func (p *Pool) reserveActiveSlot(id string, maxWorkspaces int) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if existing, ok := p.registry[id]; ok && existing.state == StateActive {
		return errSlotTaken
	}
	if maxWorkspaces > 0 {
		activeCount := 0
		for _, rec := range p.registry {
			if rec.state == StateActive {
				activeCount++
			}
		}
		if activeCount >= maxWorkspaces {
			return ErrAtCap
		}
	}
	p.registry[id] = &workspaceRecord{state: StateActive}
	return nil
}

// assignRunner attaches the spawned runner to a reserved placeholder record.
// Pool.mu is acquired internally. After this the record is sendable.
func (p *Pool) assignRunner(id string, runner WorkspaceRunner) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if rec, ok := p.registry[id]; ok {
		rec.runner = runner
	}
}

// lookupSendable returns the record for id when it is Active AND has a
// non-nil runner — i.e. ready to accept a Send. Returns (nil, false)
// otherwise: missing, non-Active, or a placeholder whose spawn has not yet
// assigned a runner. Pool.mu is acquired internally.
func (p *Pool) lookupSendable(id string) (*workspaceRecord, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.registry[id]
	if !ok || rec.state != StateActive || rec.runner == nil {
		return nil, false
	}
	return rec, true
}

// seedOrphan inserts an Orphaned record for a workspace discovered at startup.
// Runner is nil — no subprocess is associated. Pool.mu is acquired internally.
func (p *Pool) seedOrphan(id string, path string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.registry[id] = &workspaceRecord{
		state: StateOrphaned,
		path:  path,
	}
}

// markDefunct transitions an existing record (any state) to Defunct.
// A Defunct record stays in the registry so KnownIDs still includes it —
// the disk sweep won't remove its directory while it awaits reaping.
// Pool.mu is acquired internally.
func (p *Pool) markDefunct(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.registry[id]
	if !ok {
		return
	}
	rec.state = StateDefunct
}

// remove deletes the record from the registry. Called after cleanup
// completes or forgotten-workspaces reap. Pool.mu is acquired internally.
func (p *Pool) remove(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.registry, id)
}

// setPath records the on-disk workspace directory for an existing record.
// Pool.mu is acquired internally.
func (p *Pool) setPath(id string, path string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.registry[id]
	if !ok {
		return
	}
	rec.path = path
}

// setCommandID marks the workspace as executing commandID.
// Pool.mu is acquired internally.
func (p *Pool) setCommandID(id string, commandID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.registry[id]
	if !ok {
		return
	}
	rec.currentCommandID = commandID
}

// clearCommandID clears the busy-ness field on an existing record.
// Pool.mu is acquired internally.
func (p *Pool) clearCommandID(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.registry[id]
	if !ok {
		return
	}
	rec.currentCommandID = ""
}

// ── Read methods ────────────────────────────────────────────────────────────

// Snapshot returns a heartbeat-ready snapshot of every record in the registry.
// Status is derived from the record's WorkspaceState; current_command_id
// is the in-flight command, empty when idle.
func (p *Pool) Snapshot() []protocol.HeartbeatWorkspaceEntry {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]protocol.HeartbeatWorkspaceEntry, 0, len(p.registry))
	for id, rec := range p.registry {
		out = append(out, protocol.HeartbeatWorkspaceEntry{
			WorkspaceID:      id,
			Status:           rec.heartbeatStatus(),
			CurrentCommandID: rec.currentCommandID,
		})
	}
	return out
}

// KnownIDs returns a set of every workspace_id in the registry — Active,
// Defunct, and Orphaned. The disk sweep uses this to determine which
// on-disk directories are protected from removal.
func (p *Pool) KnownIDs() map[string]struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]struct{}, len(p.registry))
	for id := range p.registry {
		out[id] = struct{}{}
	}
	return out
}

// Paths returns a workspace_id → on-disk path map for every record that
// has a path set. Used by the forgotten-workspaces janitor to locate
// directories for removal.
func (p *Pool) Paths() map[string]string {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]string)
	for id, rec := range p.registry {
		if rec.path != "" {
			out[id] = rec.path
		}
	}
	return out
}

// ActiveIDs returns the workspace IDs of all Active records.
func (p *Pool) ActiveIDs() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for id, rec := range p.registry {
		if rec.state == StateActive {
			out = append(out, id)
		}
	}
	return out
}

// IdleIDs returns the workspace IDs of Active records that have no
// in-flight command (currentCommandID == ""). These are workspaces
// ready to accept the next command from the durable queue.
func (p *Pool) IdleIDs() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for id, rec := range p.registry {
		if rec.state == StateActive && rec.currentCommandID == "" {
			out = append(out, id)
		}
	}
	return out
}

// ── Dispatch ────────────────────────────────────────────────────────────────

// Dispatch routes one WorkspaceCommand to the right runner and returns the
// event the runner emits. If no registry record exists and the command is
// CreateWorkspace, the pool spawns a runner and inserts an Active record.
// Any other kind for an unknown workspace is a protocol violation — Dispatch
// returns a synthetic `completed_failure` event so the supervisor can still
// ack the command.
//
// Registry effects per kind:
//
//   - CreateWorkspace  → createActiveCapped (cap enforced atomically), setPath from CreateResult.Path
//   - other kinds      → require existing Active record; failure → completed_failure
//   - CleanupWorkspace → remove on success
//   - all kinds        → setCommandID around Execute, clearCommandID on completion
//
// Per-command timeout comes from cmd.Timeout() — the command type owns its
// deadline (wire-supplied for InvokeClaudeCode, Go-side defaults for others).
//
// maxWorkspaces is the cap on Active records. 0 means unlimited.
//
// Progress events (kind=progress) emitted by the runner are forwarded
// to `onProgress` synchronously and the dispatch continues waiting for
// the terminal event. Pass nil to drop progress events.
//
// Child-exit: when the runner's Send returns an error, the pool calls
// markDefunct so the record stays in the registry (protecting the directory
// from the disk sweep) until the backend explicitly reaps it via
// forgotten_workspaces.
func (p *Pool) Dispatch(ctx context.Context, cmd command.WorkspaceCommand, onProgress func(protocol.AgentEvent), maxWorkspaces int) protocol.AgentEvent {
	header := cmd.Header()
	workspaceID := header.WorkspaceID
	if workspaceID == "" {
		return failureEvent(header, "missing workspace_id")
	}

	// Find or create a registry record.
	//
	// CreateWorkspace reserves an Active slot atomically (existence + cap +
	// placeholder insert under one Pool.mu critical section), then spawns the
	// runner outside the lock and assigns it. Two concurrent same-id creates
	// cannot both reserve — the loser gets errSlotTaken and falls through to
	// the winner's record without spawning, so exactly one runner exists.
	//
	// Non-create commands require an already-sendable record (Active + a
	// non-nil runner). A reserved-but-not-yet-spawned placeholder is NOT
	// sendable, so no command can ever observe a record whose runner is nil.
	isCreate := header.Kind == protocol.KindCreateWorkspace
	if isCreate {
		switch err := p.reserveActiveSlot(workspaceID, maxWorkspaces); {
		case err == nil:
			// We won the reservation — spawn and assign the runner.
			runner, spawnErr := p.spawn(ctx, workspaceID)
			if spawnErr != nil {
				// Spawn failed — remove the placeholder so the workspace is
				// not spuriously reported in Snapshot.
				p.remove(workspaceID)
				p.log.Error("pool.spawn_failed", "workspace_id", workspaceID, "err", spawnErr.Error())
				return failureEvent(header, "spawn: "+spawnErr.Error())
			}
			p.assignRunner(workspaceID, runner)
			p.log.Info("pool.workspace_spawned", "workspace_id", workspaceID)
		case errors.Is(err, ErrAtCap):
			p.log.Warn("pool.create_at_cap", "workspace_id", workspaceID, "max", maxWorkspaces)
			return failureEvent(header, "cap reached")
		case errors.Is(err, errSlotTaken):
			// A concurrent create already owns the slot; do not spawn a
			// second runner. Fall through to the sendable lookup below.
			p.log.Info("pool.create_slot_taken", "workspace_id", workspaceID)
		}
	}

	// Require a sendable record (Active + spawned runner). Covers both
	// non-create commands and the create loser that fell through.
	rec, ok := p.lookupSendable(workspaceID)
	if !ok {
		return failureEvent(header, fmt.Sprintf("%s: %s (kind=%s)", ErrUnknownWorkspace, workspaceID, header.Kind))
	}

	// Serialize Sends to the same workspace via the per-record mutex.
	rec.slotMu.Lock()
	defer rec.slotMu.Unlock()

	// Mark busy before Send returns.
	p.setCommandID(workspaceID, header.CommandID)
	defer p.clearCommandID(workspaceID)

	// Per-command wall-clock cap comes from the command itself.
	timeout := cmd.Timeout()
	sendCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	ev, err := rec.runner.Send(sendCtx, cmd, onProgress)
	if err != nil {
		// IO failure / runner died / outer context cancel / per-command
		// timeout. Transition to Defunct so the record stays in KnownIDs
		// protecting the directory, then emit completed_failure.
		reason := "runner: " + err.Error()
		if errors.Is(sendCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil {
			reason = fmt.Sprintf("timeout: %s exceeded %s wall-clock", header.Kind, timeout)
		}
		p.markDefunct(workspaceID)
		_ = rec.runner.Close(context.Background())
		p.log.Warn("pool.send_failed", "workspace_id", workspaceID, "kind", string(header.Kind), "err", err.Error())
		return failureEvent(header, reason)
	}

	if header.Kind == protocol.KindCleanupWorkspace {
		// CleanupWorkspace is always terminal — remove the record from the
		// registry after a successful send, regardless of the event kind.
		p.remove(workspaceID)
		if cerr := rec.runner.Close(context.Background()); cerr != nil {
			p.log.Warn("pool.close_failed", "workspace_id", workspaceID, "err", cerr.Error())
		} else {
			p.log.Info("pool.workspace_closed", "workspace_id", workspaceID)
		}
		return ev
	}

	// For CreateWorkspace, set the path from the event outputs so the
	// janitor knows where to find the directory.
	if header.Kind == protocol.KindCreateWorkspace {
		if ev.Kind == protocol.EventCompletedSuccess {
			if pathVal, ok := ev.Outputs["path"]; ok {
				if pathStr, ok := pathVal.(string); ok && pathStr != "" {
					p.setPath(workspaceID, pathStr)
				}
			}
		}
	}

	return ev
}

// CloseAll terminates every runner. Called on supervisor shutdown.
func (p *Pool) CloseAll(ctx context.Context) {
	p.mu.Lock()
	recs := make([]*workspaceRecord, 0, len(p.registry))
	ids := make([]string, 0, len(p.registry))
	for id, rec := range p.registry {
		recs = append(recs, rec)
		ids = append(ids, id)
	}
	p.registry = make(map[string]*workspaceRecord)
	p.mu.Unlock()

	for i, rec := range recs {
		if rec.runner == nil {
			continue
		}
		if err := rec.runner.Close(ctx); err != nil {
			p.log.Warn("pool.close_all_failed", "workspace_id", ids[i], "err", err.Error())
		}
	}
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

// readResult carries one event (or error) from a background read goroutine
// back to execRunner.Send's select loop.
type readResult struct {
	ev  protocol.AgentEvent
	err error
}
