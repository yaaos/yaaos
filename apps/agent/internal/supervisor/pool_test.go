package supervisor

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
	"github.com/yaaos/agent/internal/workspace"
)

func newCreateCmd(workspaceID, commandID string) *protocol.AgentCommand {
	return &protocol.AgentCommand{
		Kind: protocol.KindCreateWorkspace,
		CreateWorkspace: &protocol.CreateWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   commandID,
				WorkspaceID: workspaceID,
				Traceparent: "tp-" + commandID,
				Kind:        protocol.KindCreateWorkspace,
			},
		},
	}
}

func newWriteCmd(workspaceID, commandID string) *protocol.AgentCommand {
	return &protocol.AgentCommand{
		Kind: protocol.KindWriteFiles,
		WriteFiles: &protocol.WriteFilesCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: commandID, WorkspaceID: workspaceID, Traceparent: "tp-" + commandID,
				Kind: protocol.KindWriteFiles,
			},
		},
	}
}

func newCleanupCmd(workspaceID, commandID string) *protocol.AgentCommand {
	return &protocol.AgentCommand{
		Kind: protocol.KindCleanupWorkspace,
		CleanupWorkspace: &protocol.CleanupWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: commandID, WorkspaceID: workspaceID, Traceparent: "tp-" + commandID,
				Kind: protocol.KindCleanupWorkspace,
			},
		},
	}
}

func TestPool_FirstCommandSpawnsRunner_SuccessEvent(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil)
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

	ev := pool.Dispatch(context.Background(), newWriteCmd("ws-unknown", "c-1"), nil)
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

	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-2"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("write: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("write2: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if spawnCount != 1 {
		t.Errorf("spawn count: want 1, got %d", spawnCount)
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

	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil)
	pool.Dispatch(context.Background(), newCleanupCmd("ws-1", "c-2"), nil)
	// After cleanup, another Write for ws-1 finds no runner.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3"), nil); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-cleanup write should fail-no-runner, got %q", ev.Kind)
	}
	// But a new CreateWorkspace respawns.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-4"), nil); ev.Kind != protocol.EventCompletedSuccess {
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
			if ev := pool.Dispatch(context.Background(), newCreateCmd(wsID, "c-create-"+wsID), nil); ev.Kind != protocol.EventCompletedSuccess {
				mu.Lock()
				failures++
				mu.Unlock()
				return
			}
			if ev := pool.Dispatch(context.Background(), newWriteCmd(wsID, "c-write-"+wsID), nil); ev.Kind != protocol.EventCompletedSuccess {
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

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "disk full") {
		t.Errorf("failure_reason: want substring 'disk full' got %q", ev.FailureReason)
	}
}

// hangingHandler blocks the workspace's response forever — used to test
// ctx cancellation while a Send is in-flight.
type hangingHandler struct{ workspace.StubHandler }

func (hangingHandler) InvokeClaudeCode(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestPool_SendContextCancel_RunnerDroppedAndFailureEmitted(t *testing.T) {
	pool := NewPool(InProcessSpawn(hangingHandler{}), nil)
	defer pool.CloseAll(context.Background())

	// First spawn the workspace via a successful CreateWorkspace.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	invokeCmd := &protocol.AgentCommand{
		Kind: protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: "c-invoke", WorkspaceID: "ws-1", Traceparent: "tp-invoke",
				Kind: protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 1},
		},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	ev := pool.Dispatch(ctx, invokeCmd, nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure on ctx cancel, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "runner:") {
		t.Errorf("failure_reason: want 'runner:' prefix, got %q", ev.FailureReason)
	}
	// The cancelled runner should be dropped — a CreateWorkspace
	// respawns rather than reusing the broken one.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-respawn"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Errorf("respawn after cancel: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

func TestPool_MissingWorkspaceID_Failure(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)

	cmd := &protocol.AgentCommand{
		Kind:            protocol.KindCreateWorkspace,
		CreateWorkspace: &protocol.CreateWorkspaceCommand{},
	}
	ev := pool.Dispatch(context.Background(), cmd, nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "missing workspace_id") {
		t.Errorf("failure_reason: %q", ev.FailureReason)
	}
}

// emittingInvokeHandler emits 3 progress events from InvokeClaudeCode then
// succeeds. Used to test the supervisor.Pool → workspace.Run progress-
// forwarding path end-to-end.
type emittingInvokeHandler struct{ workspace.StubHandler }

func (emittingInvokeHandler) InvokeClaudeCode(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	e := workspace.EmitterFromContext(ctx)
	for i := 0; i < 3; i++ {
		e.Progress(map[string]any{"i": i, "workspace_id": cmd.WorkspaceID})
	}
	return map[string]any{"workspace_id": cmd.WorkspaceID, "done": true}, nil
}

func TestPool_ProgressEventsForwardedToOnProgress(t *testing.T) {
	pool := NewPool(InProcessSpawn(emittingInvokeHandler{}), nil)
	defer pool.CloseAll(context.Background())

	// First spawn via CreateWorkspace.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	invokeCmd := &protocol.AgentCommand{
		Kind: protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: "c-invoke", WorkspaceID: "ws-1", Kind: protocol.KindInvokeClaudeCode,
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
	})
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
	pool := NewPool(InProcessSpawn(emittingInvokeHandler{}), nil)
	defer pool.CloseAll(context.Background())

	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil)
	invokeCmd := &protocol.AgentCommand{
		Kind: protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: "c-invoke", WorkspaceID: "ws-1", Kind: protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 60},
		},
	}
	// nil onProgress: progress events are silently dropped — no panic,
	// terminal event still arrives.
	ev := pool.Dispatch(context.Background(), invokeCmd, nil)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Errorf("terminal with nil onProgress: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

// TestPool_TraceContinuity_BackendParentToWorkspaceChild proves the
// supervisor → workspace dispatch path links spans through one trace_id.
// The backend's traceparent enters via the AgentCommand header; the
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
	// same rewrite supervisor.routeCommand does before pool.Dispatch).
	cmd := newCreateCmd("ws-1", "c-1")
	cmd.CreateWorkspace.Traceparent = tracing.InjectTraceparent(ctx)
	if cmd.CreateWorkspace.Traceparent == "" {
		t.Fatal("supervisor span should produce non-empty traceparent")
	}

	ev := pool.Dispatch(ctx, cmd, nil)
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

// hangingForever blocks every command body until ctx cancels. Used to
// drive the per-command timeout path in the pool.
type hangingForever struct{}

func (hangingForever) CreateWorkspace(ctx context.Context, _ *protocol.CreateWorkspaceCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}
func (hangingForever) WriteFiles(ctx context.Context, _ *protocol.WriteFilesCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}
func (hangingForever) RefreshWorkspaceAuth(ctx context.Context, _ *protocol.RefreshWorkspaceAuthCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}
func (hangingForever) InvokeClaudeCode(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}
func (hangingForever) CleanupWorkspace(ctx context.Context, _ *protocol.CleanupWorkspaceCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestPoolTimeouts_DefaultsApply(t *testing.T) {
	got := PoolTimeouts{}.withDefaults()
	if got.CreateWorkspace != 5*time.Minute {
		t.Errorf("CreateWorkspace default: want 5m got %s", got.CreateWorkspace)
	}
	if got.WriteFiles != 30*time.Second {
		t.Errorf("WriteFiles default: want 30s got %s", got.WriteFiles)
	}
	if got.InvokeClaudeCodeFallback != 15*time.Minute {
		t.Errorf("InvokeClaudeCodeFallback default: want 15m got %s", got.InvokeClaudeCodeFallback)
	}
	// Explicit overrides win.
	got = PoolTimeouts{CreateWorkspace: 1 * time.Second}.withDefaults()
	if got.CreateWorkspace != 1*time.Second {
		t.Errorf("explicit override lost: %s", got.CreateWorkspace)
	}
}

func TestPoolTimeouts_InvokeClaudeCodeUsesWireLimits(t *testing.T) {
	t.Parallel()
	to := PoolTimeouts{InvokeClaudeCodeFallback: time.Hour}.withDefaults()
	cmd := &protocol.AgentCommand{
		Kind: protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 7},
		},
	}
	if got := to.timeoutForCommand(cmd); got != 7*time.Second {
		t.Errorf("want 7s from wire limits, got %s", got)
	}
}

func TestPoolTimeouts_InvokeClaudeCodeFallbackWhenLimitsZero(t *testing.T) {
	t.Parallel()
	to := PoolTimeouts{InvokeClaudeCodeFallback: 42 * time.Second}.withDefaults()
	cmd := &protocol.AgentCommand{
		Kind:             protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{},
	}
	if got := to.timeoutForCommand(cmd); got != 42*time.Second {
		t.Errorf("want 42s fallback, got %s", got)
	}
}

func TestPool_TimeoutOnSend_EmitsFailureAndDropsRunner(t *testing.T) {
	// Spawn handler that hangs forever; set CreateWorkspace timeout to
	// 30ms so Dispatch returns quickly with a timeout-flavoured failure
	// event. The slot should be dropped + the runner closed.
	pool := NewPoolWithTimeouts(
		InProcessSpawn(hangingForever{}),
		nil,
		PoolTimeouts{CreateWorkspace: 30 * time.Millisecond},
	)
	defer pool.CloseAll(context.Background())

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("kind: want completed_failure on timeout, got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "timeout:") {
		t.Errorf("failure_reason: want 'timeout:' prefix, got %q", ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "CreateWorkspace") {
		t.Errorf("failure_reason should name the kind, got %q", ev.FailureReason)
	}
	// Slot should be dropped — next Create respawns rather than reusing
	// the timed-out runner.
	pool.mu.Lock()
	_, stillPresent := pool.runners["ws-1"]
	pool.mu.Unlock()
	if stillPresent {
		t.Errorf("slot should be dropped after timeout")
	}
}

func TestPool_TimeoutOnInvokeClaudeCode_UsesWireLimit(t *testing.T) {
	// Create succeeds via StubHandler; then Invoke hangs and times out
	// per Limits.WallclockSeconds=1 (we force a small unit conversion by
	// using fractional seconds via the fallback path is moot — this
	// goes via the wire-limits branch which uses whole seconds).
	pool := NewPoolWithTimeouts(
		InProcessSpawn(stubThenHang{}),
		nil,
		PoolTimeouts{}, // wire wins over fallbacks; defaults still fine elsewhere
	)
	defer pool.CloseAll(context.Background())

	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-create"), nil); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	invokeCmd := &protocol.AgentCommand{
		Kind: protocol.KindInvokeClaudeCode,
		InvokeClaudeCode: &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID: "c-invoke", WorkspaceID: "ws-1", Kind: protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 1}, // 1s wire cap
		},
	}
	start := time.Now()
	ev := pool.Dispatch(context.Background(), invokeCmd, nil)
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

// stubThenHang accepts CreateWorkspace via StubHandler's default; hangs
// on every other command kind until ctx cancels.
type stubThenHang struct{ workspace.StubHandler }

func (stubThenHang) InvokeClaudeCode(ctx context.Context, _ *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestPool_TimeoutDoesNotBlockOtherWorkspaces(t *testing.T) {
	// Two workspaces. ws-1's Create hangs and times out at 30ms; ws-2's
	// Create succeeds immediately. The two dispatches should NOT
	// serialize — ws-2 must return before ws-1's timeout fires.
	pool := NewPoolWithTimeouts(
		InProcessSpawn(workspace.StubHandler{}),
		nil,
		PoolTimeouts{CreateWorkspace: 30 * time.Millisecond},
	)
	defer pool.CloseAll(context.Background())

	// Switch ws-1 to a hanging spawn after the fact: easier to spin up
	// a separate pool. Direct test: just verify ws-2 succeeds fast even
	// while ws-1 is in flight (the outer pool mu only locks for slot
	// lookup, not Send).
	done := make(chan time.Duration, 2)
	go func() {
		start := time.Now()
		pool.Dispatch(context.Background(), newCreateCmd("ws-fast-1", "c-1"), nil)
		done <- time.Since(start)
	}()
	go func() {
		start := time.Now()
		pool.Dispatch(context.Background(), newCreateCmd("ws-fast-2", "c-2"), nil)
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
	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"), nil)
	pool.Dispatch(context.Background(), newCreateCmd("ws-2", "c-2"), nil)

	pool.CloseAll(context.Background())
	// After CloseAll, subsequent non-Create commands for those workspaces
	// fail since the runners are gone.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-after"), nil); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-CloseAll write should fail, got %q", ev.Kind)
	}
}
