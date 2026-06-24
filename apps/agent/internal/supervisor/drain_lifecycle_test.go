// Drain lifecycle tests: localLifecycle state machine, drain-exit logic,
// heartbeat cadence switch, and claim-request fields when draining.
package supervisor

import (
	"context"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

// ── localLifecycle state machine ───────────────────────────────────────────

func TestApplyConfigCAS_UnconfiguredToActive(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	if lc := s.localLifecycleStr(); lc != "unconfigured" {
		t.Fatalf("before ApplyConfig: want 'unconfigured', got %q", lc)
	}

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})

	if lc := s.localLifecycleStr(); lc != "active" {
		t.Errorf("after ApplyConfig: want 'active', got %q", lc)
	}
}

func TestApplyConfigCAS_DoesNotOverrideDraining(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()

	if lc := s.localLifecycleStr(); lc != "draining" {
		t.Fatalf("after RequestShutdown: want 'draining', got %q", lc)
	}

	// A subsequent ConfigUpdate (e.g. credential rotation) must not flip
	// draining back to active.
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})

	if lc := s.localLifecycleStr(); lc != "draining" {
		t.Errorf("after ApplyConfig on draining supervisor: want 'draining', got %q", lc)
	}
}

func TestRequestShutdownFlipsLocalLifecycle(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()

	if lc := s.localLifecycleStr(); lc != "draining" {
		t.Errorf("after RequestShutdown: want 'draining', got %q", lc)
	}
}

func TestCancelShutdownFlipsLocalLifecycle(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()
	s.CancelShutdown()

	if lc := s.localLifecycleStr(); lc != "active" {
		t.Errorf("after CancelShutdown: want 'active', got %q", lc)
	}
}

// ── buildClaimRequest reports localLifecycle ───────────────────────────────

func TestBuildClaimRequest_ReportsDrainingLifecycle(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 5})
	s.RequestShutdown()

	req := s.buildClaimRequest()
	if req.Lifecycle != "draining" {
		t.Errorf("lifecycle: want 'draining', got %q", req.Lifecycle)
	}
	// Draining: new_workspaces must be 0.
	if req.NewWorkspaces != 0 {
		t.Errorf("new_workspaces: want 0 while draining, got %d", req.NewWorkspaces)
	}
}

func TestBuildClaimRequest_ActiveReportsNewWorkspaces(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 3})

	req := s.buildClaimRequest()
	if req.Lifecycle != "active" {
		t.Errorf("lifecycle: want 'active', got %q", req.Lifecycle)
	}
	if req.NewWorkspaces != 3 {
		t.Errorf("new_workspaces: want 3, got %d", req.NewWorkspaces)
	}
}

// ── Drain-exit: maybeTriggerShutdownExit ──────────────────────────────────

func TestMaybeTriggerShutdownExit_NotDraining(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.cancelRun = cancel

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	// active (not draining) — maybeTriggerShutdownExit must not cancel.
	s.maybeTriggerShutdownExit()

	select {
	case <-ctx.Done():
		t.Error("context cancelled unexpectedly on non-draining supervisor")
	default:
	}
}

func TestMaybeTriggerShutdownExit_DrainingWithActiveWorkspaces(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.cancelRun = cancel

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()

	// Seed an active workspace so the pool isn't empty.
	if err := s.pool.reserveActiveSlot("ws-keep", 2); err != nil {
		t.Fatalf("reserveActiveSlot: %v", err)
	}

	s.maybeTriggerShutdownExit()

	select {
	case <-ctx.Done():
		t.Error("context cancelled unexpectedly when pool still has active workspaces")
	default:
	}
}

func TestMaybeTriggerShutdownExit_DrainingEmptyPool(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithCancel(context.Background())
	s.cancelRun = cancel
	// Don't defer cancel() — we assert context is done.

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()

	// Pool is empty; drain is complete.
	s.maybeTriggerShutdownExit()

	select {
	case <-ctx.Done():
		// expected — drain complete triggers cancel
	case <-time.After(100 * time.Millisecond):
		t.Error("context not cancelled after drain-complete check on empty pool")
	}
}

func TestMaybeTriggerShutdownExit_OnceGuard(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	callCount := 0
	_, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	s.cancelRun = func() {
		callCount++
		cancel()
	}

	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 2})
	s.RequestShutdown()

	s.maybeTriggerShutdownExit()
	s.maybeTriggerShutdownExit()
	s.maybeTriggerShutdownExit()

	if callCount != 1 {
		t.Errorf("cancelRun called %d times, want 1 (sync.Once guard)", callCount)
	}
}

// ── localLifecycleStr nil-safety ──────────────────────────────────────────

func TestLocalLifecycleStr_NilSafe(t *testing.T) {
	// A Supervisor constructed without New() has a nil localLifecycle pointer;
	// localLifecycleStr must return "unconfigured" without panicking.
	s := &Supervisor{
		cfg: Config{
			BaseURL:           "http://localhost:9999",
			Concurrency:       1,
			HeartbeatInterval: 30 * time.Second,
			ClaimWaitSeconds:  30,
			Spawn:             inProcessSpawn(workspacetest.StubHandler{}),
		},
		log:              nullLogger{},
		pool:             NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil),
		stsBackoff:       backoff.New(),
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
	}
	defer s.pool.CloseAll(context.Background())
	// localLifecycle zero-value (nil pointer) — must not panic.
	if lc := s.localLifecycleStr(); lc != "unconfigured" {
		t.Errorf("nil localLifecycle: want 'unconfigured', got %q", lc)
	}
}
