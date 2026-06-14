package supervisor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// TestSupervisor_ClaimSpan_ShutdownCancellationIsNotError verifies that
// context cancellation during a long-poll (e.g. SIGTERM during the 30 s
// hang) closes the "agent.claim" span with Status.Code = Unset, not Error.
//
// This mirrors TestSupervisor_ClaimSpan_NoCommandIsNotError for the shutdown
// path: the transport-level error (context.Canceled / EOF) is the expected
// outcome of graceful shutdown, not a real claim failure.
func TestSupervisor_ClaimSpan_ShutdownCancellationIsNotError(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	// httptest.Server that blocks until the test signals it or the request
	// context is cancelled — simulates the 30 s long-poll hanging when SIGTERM
	// fires. The unblock channel lets the test drain the server before Close.
	unblock := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/agent/commands/claim" {
			// Block until the test cancels the request OR explicitly unblocks.
			select {
			case <-r.Context().Done():
			case <-unblock:
			}
			w.WriteHeader(499)
			return
		}
		http.NotFound(w, r)
	}))
	defer func() {
		close(unblock) // drain any in-flight handler before Close.
		srv.Close()
	}()

	s := &Supervisor{
		cfg: Config{
			BaseURL:          srv.URL,
			Version:          "test",
			Concurrency:      1,
			ClaimWaitSeconds: 1,
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-shutdown-test",
		orgID:            "org-shutdown-test",
		provider:         noopProvider{},
		claimBackoff:     backoff.NewWithSteps([]time.Duration{10 * time.Millisecond}),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
	}

	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// reason: waiting for the long-poll HTTP request to reach the server
	// before cancelling; real-time delay (OS network I/O) — not durably
	// blocked in synctest sense.
	time.AfterFunc(50*time.Millisecond, cancel)
	<-done

	spans := exp.GetSpans()
	claimIdx := -1
	for i := range spans {
		if spans[i].Name == "agent.claim" {
			claimIdx = i
			break
		}
	}
	if claimIdx < 0 {
		names := make([]string, len(spans))
		for i, sp := range spans {
			names[i] = sp.Name
		}
		t.Fatalf("no agent.claim span found; all spans: %v", names)
	}
	sp := spans[claimIdx]
	if got := sp.Status.Code.String(); got != "Unset" {
		t.Errorf("agent.claim span Status.Code on shutdown cancellation: want Unset, got %s", got)
	}
	// No exception event should be recorded for a graceful shutdown.
	for _, ev := range sp.Events {
		if ev.Name == "exception" {
			t.Errorf("unexpected exception event on agent.claim span for shutdown cancellation: event=%v", ev)
		}
	}
}
