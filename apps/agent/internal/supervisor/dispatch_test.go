// Tests for the dispatch goroutine model: re-arming, panic recovery,
// concurrency, and shutdown behavior.
package supervisor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// okEventServer returns an httptest.Server that accepts all
// POST /api/v1/commands/*/events with 200 event_recorded and records call count.
func okEventServer(t *testing.T) (*httptest.Server, *int32) {
	t.Helper()
	var callCount int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost &&
			strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events") {
			atomic.AddInt32(&callCount, 1)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, &callCount
}

// buildDispatchTestSupervisor builds a minimal configured Supervisor pointing
// at the given event server and using spawnFn for workspace processes.
func buildDispatchTestSupervisor(t *testing.T, eventSrv *httptest.Server, spawnFn SpawnFunc) *Supervisor {
	t.Helper()
	s := &Supervisor{
		cfg: Config{
			BaseURL:               eventSrv.URL,
			Concurrency:           1,
			HeartbeatInterval:     30 * time.Second,
			ClaimWaitSeconds:      30,
			ActivityBatchInterval: 250 * time.Millisecond,
			Spawn:                 spawnFn,
		},
		client:           protocol.NewClient(eventSrv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-dispatch-test",
		orgID:            "org-dispatch-test",
		provider:         noopProvider{},
		pool:             NewPool(spawnFn, nil),
		stsBackoff:       backoff.New(),
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		eventPostSteps:   []time.Duration{time.Millisecond},
		dedup:            newDedupCache(dedupCacheSize),
	}
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})
	return s
}

// mustMarshalProvisionCmd builds a minimal JSON payload for a ProvisionWorkspaceCommand
// suitable for a fake claim endpoint to return.
func mustMarshalProvisionCmd(workspaceID, commandID string) []byte {
	return []byte(`{` +
		`"kind":"ProvisionWorkspace",` +
		`"command_id":"` + commandID + `",` +
		`"workspace_id":"` + workspaceID + `",` +
		`"traceparent":"tp-` + commandID + `",` +
		`"completion_token":"",` +
		`"workflow_execution_id":""` +
		`}`)
}

// blockingRunner is a test WorkspaceRunner whose Send blocks until release is
// closed (or the context is cancelled). It records how many times Send was called.
type blockingRunner struct {
	release    chan struct{}
	sendCalled chan struct{}
	onceSend   sync.Once
}

func newBlockingRunner(release chan struct{}) *blockingRunner {
	return &blockingRunner{release: release, sendCalled: make(chan struct{})}
}

func (r *blockingRunner) Send(ctx context.Context, cmd command.WorkspaceCommand, _ func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	r.onceSend.Do(func() { close(r.sendCalled) })
	select {
	case <-r.release:
	case <-ctx.Done():
		return protocol.AgentEvent{}, ctx.Err()
	}
	return protocol.AgentEvent{
		CommandID:  cmd.Header().CommandID,
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}, nil
}

func (r *blockingRunner) Close(_ context.Context) error { return nil }

// TestClaimLoopReArmsImmediatelyAfterDispatchSpawn verifies that the claim
// worker issues a second claim while the first dispatch goroutine is still
// in flight. A single combined server handles both claims and event POSTs.
// The second claim is only served after the first spawn is confirmed in-flight.
func TestClaimLoopReArmsImmediatelyAfterDispatchSpawn(t *testing.T) {
	// Two commands for different workspaces.
	cmds := [][]byte{
		mustMarshalProvisionCmd("ws-rearm-1", "cmd-rearm-1"),
		mustMarshalProvisionCmd("ws-rearm-2", "cmd-rearm-2"),
	}
	var commandsServed int32
	var eventCallCount int32

	// firstSpawned is closed when the first runner's spawn function is called,
	// meaning the first dispatch goroutine is in-flight and the claim loop
	// has already re-armed (spawned the goroutine and looped back).
	firstSpawned := make(chan struct{})
	var firstOnce sync.Once

	// release lets blocking runners return.
	release := make(chan struct{})

	spawnFn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
		firstOnce.Do(func() { close(firstSpawned) })
		return newBlockingRunner(release), nil
	}

	// Combined server: handles both claim and event-post paths.
	combined := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/agent/commands/claim":
			idx := int(atomic.AddInt32(&commandsServed, 1)) - 1
			if idx >= len(cmds) {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			if idx == 1 {
				// Gate the second command behind the first dispatch being in-flight.
				select {
				case <-firstSpawned:
				case <-time.After(3 * time.Second):
					t.Error("timeout: first dispatch goroutine never spawned")
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(cmds[idx])

		case strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events"):
			atomic.AddInt32(&eventCallCount, 1)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))

		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(combined.Close)

	s := &Supervisor{
		cfg: Config{
			BaseURL:               combined.URL,
			Concurrency:           1,
			HeartbeatInterval:     30 * time.Second,
			ClaimWaitSeconds:      1,
			ActivityBatchInterval: 250 * time.Millisecond,
			Spawn:                 spawnFn,
		},
		client:           protocol.NewClient(combined.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-rearm-test",
		orgID:            "org-rearm-test",
		provider:         noopProvider{},
		pool:             NewPool(spawnFn, nil),
		stsBackoff:       backoff.New(),
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		eventPostSteps:   []time.Duration{time.Millisecond},
		dedup:            newDedupCache(dedupCacheSize),
	}
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// Wait for the first spawn (= first dispatch in-flight), then release both.
	select {
	case <-firstSpawned:
	case <-time.After(3 * time.Second):
		t.Fatal("first dispatch goroutine never spawned")
	}
	// Brief pause so the second dispatch goroutine also reaches its spawn.
	time.Sleep(50 * time.Millisecond)
	close(release) // unblock both runners

	// Wait for both event POSTs.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if atomic.LoadInt32(&eventCallCount) >= 2 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done

	if got := atomic.LoadInt32(&eventCallCount); got < 2 {
		t.Errorf("want at least 2 event POSTs (both commands completed), got %d", got)
	}
}

// TestDispatchGoroutinePanicDoesNotKillClaimLoop verifies that a panic inside
// a dispatch goroutine does not propagate out; the recover converts it to a
// completed_failure event POST.
func TestDispatchGoroutinePanicDoesNotKillClaimLoop(t *testing.T) {
	eventSrv, eventCallCount := okEventServer(t)

	// panicRunner panics on Send.
	panicSpawn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
		return &panicRunner{}, nil
	}

	s := buildDispatchTestSupervisor(t, eventSrv, panicSpawn)
	// Replace the pool with one using panicSpawn.
	s.pool = NewPool(panicSpawn, nil)
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})
	defer s.pool.CloseAll(context.Background())

	cmd := newCreateCmd("ws-panic-1", "cmd-panic-1")

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.dispatch(context.Background(), cmd)
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("dispatch goroutine hung after panic (expected clean exit)")
	}

	// A failure event must have been posted (either from panic recovery or the
	// pool's own runner-error path).
	if got := atomic.LoadInt32(eventCallCount); got == 0 {
		t.Error("want at least 1 event POST after dispatch panic, got 0")
	}
}

// panicRunner panics on Send, simulating a catastrophic dispatch failure.
type panicRunner struct{}

func (r *panicRunner) Send(_ context.Context, _ command.WorkspaceCommand, _ func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	panic("synthetic panic in dispatch goroutine")
}
func (r *panicRunner) Close(_ context.Context) error { return nil }

// TestParallelDispatchGoroutinesForDifferentWorkspaces verifies that dispatch
// goroutines for distinct workspaces run concurrently — both reach in-flight
// state simultaneously and both complete.
func TestParallelDispatchGoroutinesForDifferentWorkspaces(t *testing.T) {
	eventSrv, eventCallCount := okEventServer(t)

	// bothInFlight is closed when two runners are simultaneously blocking.
	bothInFlight := make(chan struct{})
	var inFlight int32
	var bothOnce sync.Once

	// release unblocks all runners once both are in flight.
	release := make(chan struct{})

	concurrentSpawn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
		r := &concurrentRunner{
			onSend: func() {
				n := atomic.AddInt32(&inFlight, 1)
				if n >= 2 {
					bothOnce.Do(func() { close(bothInFlight) })
				}
			},
			release: release,
		}
		return r, nil
	}

	s := buildDispatchTestSupervisor(t, eventSrv, concurrentSpawn)
	s.pool = NewPool(concurrentSpawn, nil)
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	cmd1 := newCreateCmd("ws-par-1", "cmd-par-1")
	cmd2 := newCreateCmd("ws-par-2", "cmd-par-2")

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); s.dispatch(ctx, cmd1) }()
	go func() { defer wg.Done(); s.dispatch(ctx, cmd2) }()

	// Wait until both goroutines are simultaneously in-flight.
	select {
	case <-bothInFlight:
		close(release) // unblock both
	case <-ctx.Done():
		t.Fatal("timeout: two goroutines never reached in-flight simultaneously")
	}

	wg.Wait()

	if got := atomic.LoadInt32(&inFlight); got < 2 {
		t.Errorf("want peak in-flight >= 2, got %d", got)
	}
	if got := atomic.LoadInt32(eventCallCount); got < 2 {
		t.Errorf("want at least 2 event POSTs, got %d", got)
	}
}

// concurrentRunner signals onSend then blocks on release (or ctx cancel).
type concurrentRunner struct {
	onSend  func()
	release chan struct{}
}

func (r *concurrentRunner) Send(ctx context.Context, cmd command.WorkspaceCommand, _ func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	if r.onSend != nil {
		r.onSend()
	}
	select {
	case <-r.release:
	case <-ctx.Done():
		return protocol.AgentEvent{}, ctx.Err()
	}
	return protocol.AgentEvent{
		CommandID:  cmd.Header().CommandID,
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}, nil
}

func (r *concurrentRunner) Close(_ context.Context) error { return nil }

// TestRootContextCancelInterruptsInFlightDispatch verifies that cancelling the
// root context while a dispatch goroutine is in-flight causes the supervisor to
// exit cleanly without hanging on the goroutine.
func TestRootContextCancelInterruptsInFlightDispatch(t *testing.T) {
	eventSrv, _ := okEventServer(t)

	// dispatchStarted is closed when the runner's Send has been called.
	dispatchStarted := make(chan struct{})
	var startOnce sync.Once

	// release is never closed — the runner only unblocks on ctx cancel.
	release := make(chan struct{})
	blockingSpawn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
		r := &concurrentRunner{
			onSend: func() {
				startOnce.Do(func() { close(dispatchStarted) })
			},
			release: release,
		}
		return r, nil
	}

	s := buildDispatchTestSupervisor(t, eventSrv, blockingSpawn)
	s.pool = NewPool(blockingSpawn, nil)
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)

	cmd := newCreateCmd("ws-cancel-1", "cmd-cancel-1")

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.dispatch(ctx, cmd)
	}()

	// Wait until the runner is blocked, then cancel the context.
	select {
	case <-dispatchStarted:
	case <-time.After(3 * time.Second):
		t.Fatal("dispatch did not start within 3s")
	}
	cancel()

	// CloseAll SIGTERMs the in-process runner — unblocks Pool.Dispatch.
	s.pool.CloseAll(context.Background())

	select {
	case <-done:
		// Clean exit — expected.
	case <-time.After(3 * time.Second):
		t.Fatal("dispatch goroutine did not exit after context cancel + pool close")
	}
}

// Verify that blockingRunner satisfies WorkspaceRunner (compile-time check).
var _ WorkspaceRunner = (*blockingRunner)(nil)
var _ WorkspaceRunner = (*panicRunner)(nil)
var _ WorkspaceRunner = (*concurrentRunner)(nil)

// Verify SpawnFunc type compatibility (compile-time).
var _ SpawnFunc = func(_ context.Context, _ string) (WorkspaceRunner, error) { return nil, nil }
