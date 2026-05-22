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

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"))
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

	ev := pool.Dispatch(context.Background(), newWriteCmd("ws-unknown", "c-1"))
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

	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1")); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-2")); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("write: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3")); ev.Kind != protocol.EventCompletedSuccess {
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

	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"))
	pool.Dispatch(context.Background(), newCleanupCmd("ws-1", "c-2"))
	// After cleanup, another Write for ws-1 finds no runner.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-3")); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-cleanup write should fail-no-runner, got %q", ev.Kind)
	}
	// But a new CreateWorkspace respawns.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-4")); ev.Kind != protocol.EventCompletedSuccess {
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
			if ev := pool.Dispatch(context.Background(), newCreateCmd(wsID, "c-create-"+wsID)); ev.Kind != protocol.EventCompletedSuccess {
				mu.Lock()
				failures++
				mu.Unlock()
				return
			}
			if ev := pool.Dispatch(context.Background(), newWriteCmd(wsID, "c-write-"+wsID)); ev.Kind != protocol.EventCompletedSuccess {
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

	ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"))
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
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1")); ev.Kind != protocol.EventCompletedSuccess {
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
	ev := pool.Dispatch(ctx, invokeCmd)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure on ctx cancel, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "runner:") {
		t.Errorf("failure_reason: want 'runner:' prefix, got %q", ev.FailureReason)
	}
	// The cancelled runner should be dropped — a CreateWorkspace
	// respawns rather than reusing the broken one.
	if ev := pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-respawn")); ev.Kind != protocol.EventCompletedSuccess {
		t.Errorf("respawn after cancel: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

func TestPool_MissingWorkspaceID_Failure(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)

	cmd := &protocol.AgentCommand{
		Kind:            protocol.KindCreateWorkspace,
		CreateWorkspace: &protocol.CreateWorkspaceCommand{},
	}
	ev := pool.Dispatch(context.Background(), cmd)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "missing workspace_id") {
		t.Errorf("failure_reason: %q", ev.FailureReason)
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

	ev := pool.Dispatch(ctx, cmd)
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

func TestPool_CloseAll_TerminatesAllRunners(t *testing.T) {
	pool := NewPool(InProcessSpawn(workspace.StubHandler{}), nil)
	pool.Dispatch(context.Background(), newCreateCmd("ws-1", "c-1"))
	pool.Dispatch(context.Background(), newCreateCmd("ws-2", "c-2"))

	pool.CloseAll(context.Background())
	// After CloseAll, subsequent non-Create commands for those workspaces
	// fail since the runners are gone.
	if ev := pool.Dispatch(context.Background(), newWriteCmd("ws-1", "c-after")); ev.Kind != protocol.EventCompletedFailure {
		t.Errorf("post-CloseAll write should fail, got %q", ev.Kind)
	}
}
