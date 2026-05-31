package supervisor

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
	"github.com/yaaos/agent/internal/workspace"
)

func newCreateCmd(workspaceID, commandID string) command.WorkspaceCommand {
	return &command.CreateWorkspaceCommand{
		Proto: protocol.CreateWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   commandID,
				WorkspaceID: workspaceID,
				Traceparent: "tp-" + commandID,
				Kind:        protocol.KindCreateWorkspace,
			},
		},
	}
}

func newWriteCmd(workspaceID, commandID string) command.WorkspaceCommand {
	return &command.WriteFilesCommand{
		Proto: protocol.WriteFilesCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   commandID,
				WorkspaceID: workspaceID,
				Traceparent: "tp-" + commandID,
				Kind:        protocol.KindWriteFiles,
			},
		},
	}
}

func newCleanupCmd(workspaceID, commandID string) command.WorkspaceCommand {
	return &command.CleanupWorkspaceCommand{
		Proto: protocol.CleanupWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   commandID,
				WorkspaceID: workspaceID,
				Traceparent: "tp-" + commandID,
				Kind:        protocol.KindCleanupWorkspace,
			},
		},
	}
}

func TestPool_FirstCommandSpawnsRunner_SuccessEvent(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("kind: want completed_success got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev.CommandID != "c-1" {
		t.Errorf("command_id: want c-1 got %q", ev.CommandID)
	}
	if ev.Traceparent != "tp-c-1" {
		t.Errorf("traceparent: want tp-c-1 got %q", ev.Traceparent)
	}
}

func TestPool_NonCreateForUnknownWorkspace_Failure(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	ev := pool.Dispatch(context.Background(), newWriteCmd("ws-unknown", "c-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("kind: want completed_failure got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "no workspace runner") {
		t.Errorf("failure_reason: want substring 'no workspace runner' got %q", ev.FailureReason)
	}
}

func TestPool_MultipleCommandsReuseSameRunner(t *testing.T) {
	// Count spawns by wrapping the underlying SpawnFunc.
	var spawnCount int
	var mu sync.Mutex
	inner := InProcessSpawn(workspace.StubHandler{})
	counter := func(ctx context.Context, id string) (WorkspaceRunner, error) {
		mu.Lock()
		spawnCount++
		mu.Unlock()
		return inner(ctx, id)
	}
	pool := NewPool(counter, nil)
	defer pool.CloseAll(context.Background())

	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-2"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("write: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("write2: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if spawnCount != 1 {
		t.Errorf("spawn count: want 1, got %d", spawnCount)
	}
}

// TestPool_ConcurrentSameIDCreate_SpawnsExactlyOneRunner proves the
// check-and-set in reserveActiveSlot: N concurrent CreateWorkspace dispatches
// for the SAME workspace_id spawn exactly one runner (no orphaned runner from
// a lost reservation race) and leave exactly one Active registry record. Run
// with -race to exercise the atomic reservation guard.
func TestPool_ConcurrentSameIDCreate_SpawnsExactlyOneRunner(t *testing.T) {
	var spawnCount atomic.Int64
	inner := InProcessSpawn(workspace.StubHandler{})
	counter := func(ctx context.Context, id string) (WorkspaceRunner, error) {
		spawnCount.Add(1)
		return inner(ctx, id)
	}
	pool := NewPool(counter, nil)
	defer pool.CloseAll(context.Background())

	const total = 16
	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			pool.Dispatch(context.Background(), newCreateCmd("ws-shared", fmtWS(i)), nil, 0)
		}(i)
	}
	wg.Wait()

	if got := spawnCount.Load(); got != 1 {
		t.Errorf("spawn count for concurrent same-id create: want 1, got %d", got)
	}
	// Exactly one Active record survives — the at-most-one-runner invariant.
	ids := pool.ActiveIDs()
	if len(ids) != 1 || ids[0] != "ws-shared" {
		t.Errorf("ActiveIDs: want [ws-shared], got %v", ids)
	}
}

func TestPool_CleanupReapsRunner_RespawnOnNextCreate(t *testing.T) {
	var spawnCount int
	var mu sync.Mutex
	inner := InProcessSpawn(workspace.StubHandler{})
	counter := func(ctx context.Context, id string) (WorkspaceRunner, error) {
		mu.Lock()
		spawnCount++
		mu.Unlock()
		return inner(ctx, id)
	}
	pool := NewPool(counter, nil)
	defer pool.CloseAll(context.Background())

	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0)
	pool.Dispatch(context.Background(), newCleanupCmd("ws-1", "c-2"), nil, 0)
	// After cleanup, another Write for ws-1 finds no runner.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3"), nil, 0); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-cleanup write should fail-no-runner, got %q", ev.Kind)
	}
	// But a new CreateWorkspace respawns.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-4"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("respawn create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if spawnCount != 2 {
		t.Errorf("spawn count: want 2, got %d", spawnCount)
	}
}

func TestPool_ParallelDispatchAcrossWorkspaces(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	var wg sync.WaitGroup
	var failures int
	var mu sync.Mutex
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			wsID := fmtWS(i)
			if ev := pool.Dispatch(context.Background(), newCreateCmd(wsID, "c-create-"+wsID), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
				mu.Lock()
				failures++
				mu.Unlock()
				return
			}
			if ev := pool.Dispatch(context.Background(), newWriteCmd(wsID, "c-write-"+wsID), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
				mu.Lock()
				failures++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if failures != 0 {
		t.Errorf("want 0 failures, got %d", failures)
	}
}

func fmtWS(i int) string { return "ws-" + string(rune('a'+i)) }

func TestPool_SpawnFailure_EmitsFailure(t *testing.T) {
	failingSpawn := func(context.Context, string) (WorkspaceRunner, error) {
		return nil, errors.New("disk full")
	}
	pool := NewPool(failingSpawn, nil)

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "disk full") {
		t.Errorf("failure_reason: want substring 'disk full' got %q", ev.FailureReason)
	}
}

// hangingOps blocks InvokeClaudeCode forever — used to test ctx cancellation
// while a Send is in-flight.
type hangingOps struct{ workspace.StubHandler }

func (hangingOps) RunClaude(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	<-ctx.Done()
	return command.InvokeResult{}, ctx.Err()
}

func TestPool_SendContextCancel_RunnerDroppedAndFailureEmitted(t *testing.T) {
	pool := NewPool(InProcessSpawn(hangingOps{}), nil)
	defer pool.CloseAll(context.Background())

	// First spawn the workspace via a successful CreateWorkspace.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	invokeCmd := &command.InvokeClaudeCodeCommand{
		Proto: protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "c-invoke",
				WorkspaceID: "ws-1",
				Traceparent: "tp-invoke",
				Kind:        protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 1},
		},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	ev := pool.Dispatch(ctx, invokeCmd, nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure on ctx cancel, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "runner:") {
		t.Errorf("failure_reason: want 'runner:' prefix, got %q", ev.FailureReason)
	}
	// The runner should be marked defunct — verify via KnownIDs (record
	// stays) and Snapshot (status=exited).
	known := pool.KnownIDs()
	if _, ok := known["ws-1"]; !ok {
		t.Errorf("defunct workspace should still be in KnownIDs after cancel")
	}
	snap := pool.Snapshot()
	found := false
	for _, e := range snap {
		if e.WorkspaceID == "ws-1" {
			found = true
			if e.Status != "exited" {
				t.Errorf("status after cancel: want exited got %q", e.Status)
			}
		}
	}
	if !found {
		t.Errorf("ws-1 should still be in Snapshot after runner failure")
	}
	// A new CreateWorkspace respawns into a fresh Active record.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-respawn"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Errorf("respawn after cancel: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

func TestPool_MissingWorkspaceID_Failure(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)

	// A CreateWorkspaceCommand with no workspace_id.
	cmd := &command.CreateWorkspaceCommand{
		Proto: protocol.CreateWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				Kind: protocol.KindCreateWorkspace,
			},
		},
	}
	ev := pool.Dispatch(context.Background(), cmd, nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "missing workspace_id") {
		t.Errorf("failure_reason: %q", ev.FailureReason)
	}
}

// emittingInvokeOps emits 3 progress events from RunClaude then succeeds.
// Used to test the supervisor.Pool → workspace.Run progress-forwarding
// path end-to-end.
type emittingInvokeOps struct{ workspace.StubHandler }

func (emittingInvokeOps) RunClaude(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	e := workspace.EmitterFromContext(ctx)
	for i := 0; i < 3; i++ {
		e.Progress(map[string]any{"i": i, "workspace_id": cmd.WorkspaceID})
	}
	return command.InvokeResult{WorkspaceID: cmd.WorkspaceID}, nil
}

func TestPool_ProgressEventsForwardedToOnProgress(t *testing.T) {
	pool := NewPool(InProcessSpawn(emittingInvokeOps{}), nil)
	defer pool.CloseAll(context.Background())

	// First spawn via CreateWorkspace.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	invokeCmd := &command.InvokeClaudeCodeCommand{
		Proto: protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "c-invoke",
				WorkspaceID: "ws-1",
				Kind:        protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 60},
		},
	}
	var progress []protocol.AgentEvent
	var pmu sync.Mutex
	terminal := pool.Dispatch(context.Background(), invokeCmd, func(p protocol.AgentEvent) {
		pmu.Lock()
		progress = append(progress, p)
		pmu.Unlock()
	}, 0)
	if terminal.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("terminal: want completed_success got %q (reason=%q)", terminal.Kind, terminal.FailureReason)
	}
	if len(progress) != 3 {
		t.Fatalf("want 3 progress events, got %d", len(progress))
	}
	for i, p := range progress {
		if p.Kind != protocol.EventProgress {
			t.Errorf("event %d: want progress got %q", i, p.Kind)
		}
		if p.CommandID != "c-invoke" {
			t.Errorf("event %d: command_id mismatch got %q", i, p.CommandID)
		}
		if got := p.Outputs["i"]; got != float64(i) && got != i {
			t.Errorf("event %d: outputs.i want %d got %v", i, i, got)
		}
	}
}

func TestPool_ProgressForwarderNilDoesntPanic(t *testing.T) {
	pool := NewPool(InProcessSpawn(emittingInvokeOps{}), nil)
	defer pool.CloseAll(context.Background())

	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil, 0)
	invokeCmd := &command.InvokeClaudeCodeCommand{
		Proto: protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "c-invoke",
				WorkspaceID: "ws-1",
				Kind:        protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 60},
		},
	}
	// nil onProgress: progress events are silently dropped — no panic,
	// terminal event still arrives.
	ev := pool.Dispatch(context.Background(), invokeCmd, nil, 0)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Errorf("terminal with nil onProgress: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

// TestPool_TraceContinuity_BackendParentToWorkspaceChild proves the
// supervisor → workspace dispatch path links spans through one trace_id.
// The backend's traceparent enters via the command header; the
// workspace's handle.<kind> span on the way out should share that
// trace_id and have a parent_span_id linking back through the chain.
func TestPool_TraceContinuity_BackendParentToWorkspaceChild(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	defer tracing.Init(false)

	// Backend-emitted traceparent: trace_id aabb..ff99, span_id 1122..eeff.
	const backendParent = "00-aabbccddeeff00112233445566778899-1122334455667788-01"

	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	// Simulate the supervisor's wrapping span around dispatch (what
	// supervisor.routeCommand does in production). The new ctx has the
	// supervisor's child of the backend parent as the active span.
	ctx := tracing.ExtractContext(context.Background(), backendParent)
	ctx, end := tracing.StartSpan(ctx, "supervisor.dispatch.CreateWorkspace")

	// Now rewrite the cmd's traceparent to the supervisor's span (the
	// same rewrite routeCommand does via cmd.SetTraceparent before
	// pool.Dispatch).
	cmd := newCreateCmd("ws-1", "c-1").(*command.CreateWorkspaceCommand)
	cmd.Proto.Traceparent = tracing.InjectTraceparent(ctx)
	if cmd.Proto.Traceparent == "" {
		t.Fatal("supervisor span should produce non-empty traceparent")
	}

	ev := pool.Dispatch(ctx, cmd, nil, 0)
	end(nil)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("dispatch: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	spans := exp.GetSpans()
	if len(spans) < 2 {
		t.Fatalf("want at least 2 spans (supervisor + workspace), got %d", len(spans))
	}
	const wantTraceID = "aabbccddeeff00112233445566778899"
	for _, s := range spans {
		if s.SpanContext.TraceID().String() != wantTraceID {
			t.Errorf("span %q: trace_id=%s want %s", s.Name, s.SpanContext.TraceID(), wantTraceID)
		}
	}

	// Check parent linkage: supervisor span's parent is the backend
	// parent span_id; workspace span's parent is the supervisor span's
	// span_id.
	var supervisorSpan, wsSpan *struct {
		spanID, parentSpanID string
	}
	for i := range spans {
		s := spans[i]
		entry := &struct{ spanID, parentSpanID string }{
			spanID:       s.SpanContext.SpanID().String(),
			parentSpanID: s.Parent.SpanID().String(),
		}
		switch {
		case strings.HasPrefix(s.Name, "supervisor.dispatch"):
			supervisorSpan = entry
		case strings.HasPrefix(s.Name, "workspace.handle"):
			wsSpan = entry
		}
	}
	if supervisorSpan == nil {
		t.Fatal("no supervisor.dispatch.* span")
	}
	if wsSpan == nil {
		t.Fatal("no workspace.handle.* span")
	}
	if supervisorSpan.parentSpanID != "1122334455667788" {
		t.Errorf("supervisor span parent: want 1122334455667788 got %s", supervisorSpan.parentSpanID)
	}
	if wsSpan.parentSpanID != supervisorSpan.spanID {
		t.Errorf("workspace span parent: want %s (supervisor's span_id) got %s",
			supervisorSpan.spanID, wsSpan.parentSpanID)
	}
}

// hangingForeverOps blocks every command body until ctx cancels. Used to
// drive the per-command timeout path in the pool.
type hangingForeverOps struct{}

func (hangingForeverOps) CloneWorkspace(ctx context.Context, _ *protocol.CreateWorkspaceCommand) (command.CreateResult, error) {
	<-ctx.Done()
	return command.CreateResult{}, ctx.Err()
}
func (hangingForeverOps) WriteFiles(ctx context.Context, _ *protocol.WriteFilesCommand) (command.WriteFilesResult, error) {
	<-ctx.Done()
	return command.WriteFilesResult{}, ctx.Err()
}
func (hangingForeverOps) RefreshAuth(ctx context.Context, _ *protocol.RefreshWorkspaceAuthCommand) (command.RefreshResult, error) {
	<-ctx.Done()
	return command.RefreshResult{}, ctx.Err()
}
func (hangingForeverOps) RunClaude(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	<-ctx.Done()
	return command.InvokeResult{}, ctx.Err()
}
func (hangingForeverOps) Cleanup(ctx context.Context, _ *protocol.CleanupWorkspaceCommand) (command.CleanupResult, error) {
	<-ctx.Done()
	return command.CleanupResult{}, ctx.Err()
}

func TestPool_TimeoutOnSend_EmitsFailureAndDropsRunner(t *testing.T) {
	// Spawn handler that hangs forever; the command carries a 30ms timeout
	// so Dispatch returns quickly with a timeout-flavoured failure event.
	// The slot should be dropped + the runner closed. Per-command Timeout()
	// is the sole deadline source — the pool holds no timeout config.
	pool := NewPool(InProcessSpawn(hangingForeverOps{}), nil)
	defer pool.CloseAll(context.Background())

	cmd := &shortTimeoutCreateCmd{
		CreateWorkspaceCommand: command.CreateWorkspaceCommand{
			Proto: protocol.CreateWorkspaceCommand{
				CommandHeader: protocol.CommandHeader{
					CommandID:   "c-1",
					WorkspaceID: "ws-1",
					Traceparent: "tp-c-1",
					Kind:        protocol.KindCreateWorkspace,
				},
			},
		},
	}
	ev := pool.Dispatch(context.Background(), cmd, nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("kind: want completed_failure on timeout, got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "timeout:") {
		t.Errorf("failure_reason: want 'timeout:' prefix, got %q", ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "CreateWorkspace") {
		t.Errorf("failure_reason should name the kind, got %q", ev.FailureReason)
	}
	// Runner is marked defunct after timeout — KnownIDs still includes
	// it (directory protection), but a new Create respawns.
	known := pool.KnownIDs()
	if _, ok := known["ws-1"]; !ok {
		t.Errorf("defunct workspace should be in KnownIDs after timeout (directory protection)")
	}
	snap := pool.Snapshot()
	found := false
	for _, e := range snap {
		if e.WorkspaceID == "ws-1" {
			found = true
			if e.Status != "exited" {
				t.Errorf("status after timeout: want exited got %q", e.Status)
			}
		}
	}
	if !found {
		t.Errorf("ws-1 should still be in Snapshot after timeout (Defunct)")
	}
}

// shortTimeoutCreateCmd wraps CreateWorkspaceCommand with a 30ms timeout for
// testing the pool's per-command deadline path.
type shortTimeoutCreateCmd struct {
	command.CreateWorkspaceCommand
}

func (s *shortTimeoutCreateCmd) Timeout() time.Duration { return 30 * time.Millisecond }

func TestPool_TimeoutOnInvokeClaudeCode_UsesWireLimit(t *testing.T) {
	// Create succeeds via StubHandler; then Invoke hangs and times out
	// per Limits.WallclockSeconds=1 on the command (the wire limit sets
	// InvokeClaudeCodeCommand.Timeout() to 1s).
	pool := NewPool(InProcessSpawn(stubThenHangOps{}), nil)
	defer pool.CloseAll(context.Background())

	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	invokeCmd := &command.InvokeClaudeCodeCommand{
		Proto: protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "c-invoke",
				WorkspaceID: "ws-1",
				Kind:        protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 1}, // 1s wire cap
		},
	}
	start := time.Now()
	ev := pool.Dispatch(context.Background(), invokeCmd, nil, 0)
	elapsed := time.Since(start)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("invoke: want completed_failure on timeout, got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "InvokeClaudeCode exceeded 1s") {
		t.Errorf("failure_reason should mention the per-kind timeout, got %q", ev.FailureReason)
	}
	if elapsed > 3*time.Second {
		t.Errorf("dispatch took too long: %s (cap should be ~1s)", elapsed)
	}
}

// stubThenHangOps accepts CreateWorkspace via StubHandler's default;
// hangs on every InvokeClaudeCode until ctx cancels.
type stubThenHangOps struct{ workspace.StubHandler }

func (stubThenHangOps) RunClaude(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	<-ctx.Done()
	return command.InvokeResult{}, ctx.Err()
}

func TestPool_TimeoutDoesNotBlockOtherWorkspaces(t *testing.T) {
	// Two workspaces dispatched concurrently. Both use StubHandler (fast),
	// so both should return well under 200ms. The per-workspace slot mutex
	// only serializes Sends to the SAME workspace; different workspaces
	// run in parallel.
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	done := make(chan time.Duration, 2)
	go func() {
		start := time.Now()
		pool.Dispatch(context.Background(), newCreateCmd("ws-fast-1", "c-1"), nil, 0)
		done <- time.Since(start)
	}()
	go func() {
		start := time.Now()
		pool.Dispatch(context.Background(), newCreateCmd("ws-fast-2", "c-2"), nil, 0)
		done <- time.Since(start)
	}()
	for i := 0; i < 2; i++ {
		select {
		case d := <-done:
			if d > 200*time.Millisecond {
				t.Errorf("dispatch %d took %s; want <200ms (independent workspaces)", i, d)
			}
		case <-time.After(1 * time.Second):
			t.Fatal("dispatch did not return in 1s")
		}
	}
}

func TestPool_CloseAll_TerminatesAllRunners(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil, 0)
	pool.Dispatch(context.Background(), newCreateCmd("ws-2", "c-2"), nil, 0)

	pool.CloseAll(context.Background())
	// After CloseAll, subsequent non-Create commands for those workspaces
	// fail since the runners are gone.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-after"), nil, 0); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-CloseAll write should fail, got %q", ev.Kind)
	}
}

// TestPool_ShutdownDuringInFlight_EmitsCompletedFailure pins the present
// contract for what happens when the supervisor cancels the root context while
// a command Send is in-flight: Pool.Dispatch must emit a completed_failure event
// with kind=completed_failure, FailureReason prefixed with "runner:", and the
// original CommandID + Traceparent preserved in the event header fields.
//
// This is the shutdown-drain contract: no in-flight command is silently
// dropped — the caller always receives a terminal event (failureEvent at
// pool.go:512-519) even when the outer context is cancelled mid-Send.
func TestPool_ShutdownDuringInFlight_EmitsCompletedFailure(t *testing.T) {
	// hangingForeverOps (defined in this file) blocks all command bodies
	// including CreateWorkspace until ctx cancels — simulates a workspace
	// operation interrupted by supervisor shutdown (root ctx cancel).
	pool := NewPool(InProcessSpawn(hangingForeverOps{}), nil)
	defer pool.CloseAll(context.Background())

	const wsID = "ws-shutdown"
	const cmdID = "cmd-shutdown"
	const traceparent = "tp-shutdown"

	cmd := &command.CreateWorkspaceCommand{
		Proto: protocol.CreateWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   cmdID,
				WorkspaceID: wsID,
				Traceparent: traceparent,
				Kind:        protocol.KindCreateWorkspace,
			},
		},
	}

	ctx, cancel := context.WithCancel(context.Background())

	result := make(chan protocol.AgentEvent, 1)
	go func() {
		result <- pool.Dispatch(ctx, cmd, nil, 0)
	}()

	// Give the dispatch goroutine time to enter the blocking Send, then
	// cancel the context (simulating supervisor shutdown).
	time.Sleep(20 * time.Millisecond) // reason: waiting for the dispatch goroutine to reach the blocking Send inside the fake RunFunc; channel rendezvous would require exposing internal pool state.
	cancel()

	var ev protocol.AgentEvent
	select {
	case ev = <-result:
	case <-time.After(3 * time.Second):
		t.Fatal("Pool.Dispatch did not return after context cancel (possible deadlock)")
	}

	// Contract assertions: completed_failure with runner: prefix, header fields preserved.
	if ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("kind: want completed_failure got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "runner:") {
		t.Errorf("failure_reason: want 'runner:' prefix, got %q", ev.FailureReason)
	}
	if ev.CommandID != cmdID {
		t.Errorf("command_id: want %q got %q", cmdID, ev.CommandID)
	}
	if ev.Traceparent != traceparent {
		t.Errorf("traceparent: want %q got %q", traceparent, ev.Traceparent)
	}
}

// TestPool_NonCreateWorkspaceCommand_UnknownWorkspace_SyntheticFailure proves
// that any non-create WorkspaceCommand for an unseen workspace_id yields a
// completed_failure with reason containing "no workspace runner".
func TestPool_NonCreateWorkspaceCommand_UnknownWorkspace_SyntheticFailure(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	kinds := []command.WorkspaceCommand{
		newWriteCmd("ws-never-created", "c-write"),
		&command.RefreshWorkspaceAuthCommand{
			Proto: protocol.RefreshWorkspaceAuthCommand{
				CommandHeader: protocol.CommandHeader{
					CommandID:   "c-refresh",
					WorkspaceID: "ws-never-created",
					Kind:        protocol.KindRefreshWorkspaceAuth,
				},
			},
		},
		newCleanupCmd("ws-never-created", "c-cleanup"),
	}
	for _, cmd := range kinds {
		ev := pool.Dispatch(context.Background(), cmd, nil, 0)
		if ev.Kind != protocol.EventCompletedFailure {
			t.Errorf("kind=%s: want completed_failure, got %q (reason=%q)",
				cmd.Header().Kind, ev.Kind, ev.FailureReason)
		}
		if !strings.Contains(ev.FailureReason, "no workspace runner") {
			t.Errorf("kind=%s: failure_reason %q missing 'no workspace runner'",
				cmd.Header().Kind, ev.FailureReason)
		}
	}
}
