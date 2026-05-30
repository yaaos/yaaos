// Tests for dedup-replay and event-post retry behavior in routeCommand.
package supervisor

import (
	"context"
	"errors"
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
	"github.com/yaaos/agent/internal/workspace"
)

// ── Helpers ────────────────────────────────────────────────────────────────

// fakeEventServer is an httptest.Server that handles
// POST /api/v1/commands/{id}/events. It can be configured to fail a fixed
// number of times before returning 200, and can return 410 Gone immediately.
type fakeEventServer struct {
	mu         sync.Mutex
	failCount  int   // return 500 this many times before succeeding
	returnGone bool  // always return 410
	callCount  int32 // atomic counter for total POSTs received
	server     *httptest.Server
}

func newFakeEventServer(t *testing.T, failCount int, returnGone bool) *fakeEventServer {
	t.Helper()
	fs := &fakeEventServer{failCount: failCount, returnGone: returnGone}
	fs.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/api/v1/commands/") {
			http.NotFound(w, r)
			return
		}
		atomic.AddInt32(&fs.callCount, 1)
		fs.mu.Lock()
		remaining := fs.failCount
		if remaining > 0 {
			fs.failCount--
		}
		gone := fs.returnGone
		fs.mu.Unlock()

		if gone {
			w.WriteHeader(http.StatusGone)
			return
		}
		if remaining > 0 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(fs.server.Close)
	return fs
}

// buildSupervisorForRetryTest builds a Supervisor whose event-post retry uses
// near-zero delays so tests don't stall. It registers a fake HTTP server for
// the PostCommandEvent path and pre-applies a config.
func buildSupervisorForRetryTest(t *testing.T, srv *fakeEventServer, spawnFn SpawnFunc) *Supervisor {
	t.Helper()
	if spawnFn == nil {
		spawnFn = InProcessSpawn(workspace.StubHandler{})
	}
	cfg := Config{
		BaseURL:               srv.server.URL,
		AgentPodID:            "pod-test",
		Concurrency:           1,
		HeartbeatInterval:     30 * time.Second,
		ClaimWaitSeconds:      30,
		ActivityBatchInterval: 250 * time.Millisecond,
		Spawn:                 spawnFn,
	}
	s := &Supervisor{
		cfg:              cfg,
		client:           protocol.NewClient(cfg.BaseURL, nil),
		log:              nullLogger{},
		agentID:          "agent-test",
		orgID:            "org-test",
		provider:         noopProvider{},
		pool:             NewPool(spawnFn, nil),
		stsBackoff:       backoff.New(),
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		eventPostSteps:   []time.Duration{time.Millisecond},
		dedup:            newDedupCache(dedupCacheSize),
	}
	// Apply config so workspace commands are accepted.
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})
	return s
}

// ── Dedup-replay: service test ─────────────────────────────────────────────

// TestRouteCommand_DedupReplay dispatches a command_id, then dispatches the
// same command_id again. The second dispatch must return the cached terminal
// event without calling WorkspaceOps (no second spawn).
func TestRouteCommand_DedupReplay(t *testing.T) {
	var spawnCount int32
	inner := InProcessSpawn(workspace.StubHandler{})
	countingSpawn := func(ctx context.Context, id string) (WorkspaceRunner, error) {
		atomic.AddInt32(&spawnCount, 1)
		return inner(ctx, id)
	}

	srv := newFakeEventServer(t, 0, false)
	s := buildSupervisorForRetryTest(t, srv, countingSpawn)
	defer s.pool.CloseAll(context.Background())

	cmd := newCreateCmd("ws-dedup", "cmd-dedup-1")

	// First dispatch — should spawn a runner and post the event.
	ctx := context.Background()
	s.routeCommand(ctx, cmd)

	spawnAfterFirst := atomic.LoadInt32(&spawnCount)
	callsAfterFirst := atomic.LoadInt32(&srv.callCount)

	if spawnAfterFirst == 0 {
		t.Fatal("first dispatch: expected at least one spawn")
	}
	if callsAfterFirst == 0 {
		t.Fatal("first dispatch: expected at least one event POST")
	}

	// Second dispatch with the same command_id — must NOT spawn again and must
	// return/post the cached terminal event.
	s.routeCommand(ctx, cmd)

	spawnAfterSecond := atomic.LoadInt32(&spawnCount)
	if spawnAfterSecond != spawnAfterFirst {
		t.Errorf("second dispatch: spawn count changed from %d to %d (WorkspaceOps called again)",
			spawnAfterFirst, spawnAfterSecond)
	}

	// The dedup path still posts the cached event (one more POST).
	callsAfterSecond := atomic.LoadInt32(&srv.callCount)
	if callsAfterSecond <= callsAfterFirst {
		t.Errorf("second dispatch: expected cached event to be posted, call count did not increase (%d→%d)",
			callsAfterFirst, callsAfterSecond)
	}
}

// ── Event-post retry: unit tests ───────────────────────────────────────────

// TestEventPostRetry_SucceedsAfterTransientFailures verifies that the retry
// loop keeps posting until the server accepts the event.
func TestEventPostRetry_SucceedsAfterTransientFailures(t *testing.T) {
	const failN = 3
	srv := newFakeEventServer(t, failN, false)
	s := buildSupervisorForRetryTest(t, srv, nil)
	defer s.pool.CloseAll(context.Background())

	// Route a command through the full path. The event server rejects the
	// first failN POSTs with 500, then accepts.
	cmd := newCreateCmd("ws-retry", "cmd-retry-1")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	s.routeCommand(ctx, cmd)

	calls := atomic.LoadInt32(&srv.callCount)
	if calls < failN+1 {
		t.Errorf("want at least %d POST attempts, got %d", failN+1, calls)
	}
}

// TestEventPostRetry_StaleClaimStopsImmediately verifies that a 410 Gone
// response stops the retry loop without further attempts.
func TestEventPostRetry_StaleClaimStopsImmediately(t *testing.T) {
	srv := newFakeEventServer(t, 0, true) // always 410
	s := buildSupervisorForRetryTest(t, srv, nil)
	defer s.pool.CloseAll(context.Background())

	cmd := newCreateCmd("ws-stale", "cmd-stale-1")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	s.routeCommand(ctx, cmd)

	calls := atomic.LoadInt32(&srv.callCount)
	// Exactly one attempt — 410 stops the loop.
	if calls != 1 {
		t.Errorf("want exactly 1 POST attempt on 410 Got, got %d", calls)
	}
}

// ── Dedup with retry ───────────────────────────────────────────────────────

// TestDedupReplay_RetryLoopActive verifies that a replayed (deduped) event
// also goes through the retry loop.
func TestDedupReplay_RetryLoopActive(t *testing.T) {
	const failN = 2
	srv := newFakeEventServer(t, failN, false)
	s := buildSupervisorForRetryTest(t, srv, nil)
	defer s.pool.CloseAll(context.Background())

	cmd := newCreateCmd("ws-dedup-retry", "cmd-dr-1")
	ctx := context.Background()

	// First dispatch: establishes the cache entry and uses failN+1 calls.
	s.routeCommand(ctx, cmd)
	callsAfterFirst := atomic.LoadInt32(&srv.callCount)

	// Reset the server so it fails again for the dedup replay.
	srv.mu.Lock()
	srv.failCount = failN
	srv.mu.Unlock()

	// Second dispatch: dedup hit → replays cached event through retry loop.
	s.routeCommand(ctx, cmd)

	callsAfterSecond := atomic.LoadInt32(&srv.callCount)
	// The dedup path should have retried failN times then succeeded.
	if callsAfterSecond-callsAfterFirst < failN+1 {
		t.Errorf("dedup replay: want at least %d additional POST attempts, got %d",
			failN+1, callsAfterSecond-callsAfterFirst)
	}
}

// ── Protocol client sentinel ──────────────────────────────────────────────

// TestProtocolClient_410ReturnsErrStaleClaim verifies that the protocol
// client maps a 410 HTTP response to protocol.ErrStaleClaim.
func TestProtocolClient_410ReturnsErrStaleClaim(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
	}))
	defer srv.Close()

	c := protocol.NewClient(srv.URL, nil)
	err := c.PostCommandEvent(context.Background(), "cmd-x", protocol.AgentEvent{
		CommandID:  "cmd-x",
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	})
	if !errors.Is(err, protocol.ErrStaleClaim) {
		t.Errorf("want ErrStaleClaim, got %v", err)
	}
}
