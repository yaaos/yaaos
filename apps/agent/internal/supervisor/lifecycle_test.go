// Lifecycle gate + max_workspaces cap: supervisor state machine tests.
//
// Tests cover:
//   - Unconfigured agent rejects WorkspaceCommands with completed_failure "agent unconfigured".
//   - After ConfigUpdateCommand, WorkspaceCommands succeed.
//   - ProvisionWorkspace past the cap returns completed_failure "cap reached".
//   - Concurrent createActive calls at the cap admit exactly max_workspaces (race-detector).
package supervisor

import (
	"context"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/identity"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

// ── Supervisor lifecycle gate ───────────────────────────────────────────────

// buildUnconfiguredSupervisor returns a Supervisor in the unconfigured state
// (config.Load() == nil), wired with an InProcessSpawn so Dispatch works.
func buildUnconfiguredSupervisor(t *testing.T) *Supervisor {
	t.Helper()
	cfg := Config{
		BaseURL: "http://localhost:9999",

		Concurrency:           1,
		HeartbeatInterval:     30 * time.Second,
		ClaimWaitSeconds:      30,
		ActivityBatchInterval: 250 * time.Millisecond,
		Spawn:                 inProcessSpawn(workspacetest.StubHandler{}),
	}
	s := &Supervisor{
		cfg:              cfg,
		client:           nil, // unused in these tests
		log:              nullLogger{},
		agentID:          "agent-test",
		orgID:            "org-test",
		provider:         noopProvider{},
		pool:             NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil),
		stsBackoff:       backoff.New(),
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
	}
	// config not stored → unconfigured
	return s
}

// applyConfigToSupervisor applies a ConfigUpdateCommand to s via routeCommand's
// in-supervisor Execute path and waits for the config pointer to be set.
func applyConfig(s *Supervisor, maxWS int) {
	cfg := command.AgentConfig{MaxWorkspaces: maxWS}
	s.ApplyConfig(cfg)
}

func TestSupervisor_Unconfigured_WorkspaceCommandFailsWithUnconfigured(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	// Route a WorkspaceCommand while unconfigured — must return completed_failure.
	cmd := newCreateCmd("ws-1", "cmd-1")
	ev := s.routeWorkspaceCmd(context.Background(), cmd, nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("kind: want completed_failure got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
	if !strings.Contains(ev.FailureReason, "agent unconfigured") {
		t.Errorf("failure_reason: want 'agent unconfigured', got %q", ev.FailureReason)
	}
}

func TestSupervisor_AfterConfig_WorkspaceCommandSucceeds(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	// Apply config — agent becomes configured.
	applyConfig(s, 5)

	// Same command now routes to the pool and succeeds.
	cmd := newCreateCmd("ws-1", "cmd-1")
	ev := s.routeWorkspaceCmd(context.Background(), cmd, nil)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("kind: want completed_success after config, got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}
}

func TestSupervisor_CreatePastCap_FailsCapReached(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	// Configure with cap=1.
	applyConfig(s, 1)

	// First create succeeds.
	ev1 := s.routeWorkspaceCmd(context.Background(), newCreateCmd("ws-a", "cmd-a"), nil)
	if ev1.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("first create: want completed_success got %q (reason=%q)", ev1.Kind, ev1.FailureReason)
	}

	// Second create should fail with cap reached.
	ev2 := s.routeWorkspaceCmd(context.Background(), newCreateCmd("ws-b", "cmd-b"), nil)
	if ev2.Kind != protocol.EventCompletedFailure {
		t.Fatalf("second create: want completed_failure got %q (reason=%q)", ev2.Kind, ev2.FailureReason)
	}
	if !strings.Contains(ev2.FailureReason, "cap reached") {
		t.Errorf("failure_reason: want 'cap reached', got %q", ev2.FailureReason)
	}
}

// ── Pool cap gate (concurrent, race-detector) ──────────────────────────────

// TestPool_ConcurrentCreateActive_CapAdmitsExactlyMaxWorkspaces proves that
// N concurrent reserveActiveSlot calls (distinct ids) with cap=M admit exactly
// M and reject N-M with ErrAtCap. Run with -race to exercise the atomic guard.
func TestPool_ConcurrentCreateActive_CapAdmitsExactlyMaxWorkspaces(t *testing.T) {
	const maxWS = 3
	const total = 10

	pool := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer pool.CloseAll(context.Background())

	var admitted atomic.Int64
	var rejected atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := fmtWS(i)
			err := pool.reserveActiveSlot(id, maxWS)
			if err == nil {
				admitted.Add(1)
			} else {
				rejected.Add(1)
			}
		}(i)
	}
	wg.Wait()

	if admitted.Load() != maxWS {
		t.Errorf("admitted: want %d got %d", maxWS, admitted.Load())
	}
	if rejected.Load() != total-maxWS {
		t.Errorf("rejected: want %d got %d", total-maxWS, rejected.Load())
	}
}

// ── Claim request lifecycle fields ─────────────────────────────────────────

// TestClaimRequest_LifecycleFields verifies that buildClaimRequest produces
// the right lifecycle string and active workspace IDs.
func TestClaimRequest_LifecycleFields(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())

	// Unconfigured → lifecycle="unconfigured", workspace_ids=[] new_workspaces=0
	req := s.buildClaimRequest()
	if req.Lifecycle != "unconfigured" {
		t.Errorf("lifecycle: want 'unconfigured', got %q", req.Lifecycle)
	}
	if len(req.WorkspaceIDs) != 0 {
		t.Errorf("workspace_ids: want empty, got %v", req.WorkspaceIDs)
	}
	if req.NewWorkspaces != 0 {
		t.Errorf("new_workspaces: want 0, got %d", req.NewWorkspaces)
	}

	// Apply config → lifecycle="configured"
	applyConfig(s, 5)
	req2 := s.buildClaimRequest()
	if req2.Lifecycle != "configured" {
		t.Errorf("lifecycle: want 'configured', got %q", req2.Lifecycle)
	}
}

// TestClaimRequest_ShortPollWhenBusyWorkspacesExist verifies that
// buildClaimRequest uses a short wait_seconds (1s) whenever active workspaces
// exist but none are idle — even when new_workspaces capacity is available.
// This bounds the delay between a workspace finishing a command and the claim
// loop re-arming with the updated workspace_ids — critical for sequential
// multi-command flows where the dispatch goroutine model re-arms the claim loop
// before Pool.Dispatch has cleared the workspace's in-flight command ID.
func TestClaimRequest_ShortPollWhenBusyWorkspacesExist(t *testing.T) {
	cases := []struct {
		name  string
		maxWS int // max_workspaces from ConfigUpdate
	}{
		{"at_cap_max1", 1},    // 1 busy workspace, no room for new
		{"below_cap_max4", 4}, // 1 busy workspace, room for 3 more (backend sends max_workspaces=4)
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := buildUnconfiguredSupervisor(t)
			defer s.pool.CloseAll(context.Background())

			applyConfig(s, tc.maxWS)

			// No workspaces yet → full wait (no busy workspaces to wait for).
			req := s.buildClaimRequest()
			if req.WaitSeconds != s.cfg.ClaimWaitSeconds {
				t.Errorf("want full wait_seconds=%d with no workspaces, got %d", s.cfg.ClaimWaitSeconds, req.WaitSeconds)
			}

			// Insert one Active workspace and mark it busy.
			const wsID = "ws-busy-test"
			s.pool.createActive(wsID, nil) // nil runner; only registry state matters
			s.pool.setCommandID(wsID, "cmd-123")

			// Busy workspace present, none idle → short poll regardless of new_workspaces.
			req2 := s.buildClaimRequest()
			if req2.WaitSeconds != 1 {
				t.Errorf("[%s] want wait_seconds=1 when busy workspace exists, got %d", tc.name, req2.WaitSeconds)
			}
			if len(req2.WorkspaceIDs) != 0 {
				t.Errorf("[%s] want empty workspace_ids when busy, got %v", tc.name, req2.WorkspaceIDs)
			}

			// Workspace becomes idle → full wait (now listed in workspace_ids).
			s.pool.clearCommandID(wsID)
			req3 := s.buildClaimRequest()
			if req3.WaitSeconds != s.cfg.ClaimWaitSeconds {
				t.Errorf("[%s] want full wait_seconds=%d when workspace idle, got %d", tc.name, s.cfg.ClaimWaitSeconds, req3.WaitSeconds)
			}
			if len(req3.WorkspaceIDs) != 1 || req3.WorkspaceIDs[0] != wsID {
				t.Errorf("[%s] want workspace_ids=[%s], got %v", tc.name, wsID, req3.WorkspaceIDs)
			}
		})
	}
}

// TestClaimRequest_ShortPollWhenPendingDispatch verifies that buildClaimRequest
// uses a 1-second short poll whenever the pool reports a pending dispatch, even
// when the pool has no active workspaces. This prevents a 30-second stall caused
// by the claim loop re-arming with empty workspace_ids before a dispatch
// goroutine's Pool.Dispatch has registered the workspace.
func TestClaimRequest_ShortPollWhenPendingDispatch(t *testing.T) {
	s := buildUnconfiguredSupervisor(t)
	defer s.pool.CloseAll(context.Background())
	applyConfig(s, 4)

	// No workspaces, no pending dispatch → full wait.
	req := s.buildClaimRequest()
	if req.WaitSeconds != s.cfg.ClaimWaitSeconds {
		t.Errorf("want full wait_seconds=%d with no pending dispatch, got %d", s.cfg.ClaimWaitSeconds, req.WaitSeconds)
	}

	// Simulate a dispatch goroutine in-flight (marked before go s.dispatch).
	s.pool.MarkDispatchPending()

	// Pending dispatch → short poll even with no pool workspaces.
	req2 := s.buildClaimRequest()
	if req2.WaitSeconds != 1 {
		t.Errorf("want wait_seconds=1 with pending dispatch, got %d", req2.WaitSeconds)
	}

	// Settled (goroutine completed) → full wait returns.
	s.pool.MarkDispatchSettled()
	req3 := s.buildClaimRequest()
	if req3.WaitSeconds != s.cfg.ClaimWaitSeconds {
		t.Errorf("want full wait_seconds=%d after dispatch settles, got %d", s.cfg.ClaimWaitSeconds, req3.WaitSeconds)
	}
}

// ── Identity test helper (ensures noopProvider implements identity.Provider) ──
var _ identity.Provider = noopProvider{}
