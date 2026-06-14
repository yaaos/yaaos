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

// TestSupervisor_ClaimSpan_NoCommandIsNotError verifies that a 204 response
// from the claim endpoint (ErrNoCommand — the normal long-poll outcome) closes
// the "agent.claim" span with Status.Code = Unset, not Error.
func TestSupervisor_ClaimSpan_NoCommandIsNotError(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	// httptest.Server that returns 204 — the normal "no command available" path.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/agent/commands/claim" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg: Config{
			BaseURL:          srv.URL,
			Version:          "test",
			Concurrency:      1,
			ClaimWaitSeconds: 1,
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-nocommand-test",
		orgID:            "org-nocommand-test",
		provider:         noopProvider{},
		claimBackoff:     backoff.NewWithSteps([]time.Duration{10 * time.Millisecond}),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
	}

	// Cancel after first iteration (204 resets backoff and continues;
	// cancelling the context exits the loop).
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// Wait for at least one span to appear then cancel so the loop exits.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(exp.GetSpans()) > 0 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
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
		t.Errorf("agent.claim span Status.Code on 204: want Unset, got %s", got)
	}
	// No exception events should be recorded for a normal 204.
	for _, ev := range sp.Events {
		if ev.Name == "exception" {
			t.Errorf("unexpected exception event on agent.claim span for 204 (ErrNoCommand): event=%v", ev)
		}
	}
}
