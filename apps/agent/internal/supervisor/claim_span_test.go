package supervisor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// TestSupervisor_ClaimSpan_RecordsErrorOnHTTPFailure verifies that a 500
// response from the claim endpoint is wrapped in an "agent.claim" span with
// Status.Code = Error and a RecordError exception event. Uses an
// InMemoryExporter so the assertion is deterministic and synchronous.
func TestSupervisor_ClaimSpan_RecordsErrorOnHTTPFailure(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	// Restore the no-op provider after the test so other tests see
	// a clean global tracer provider.
	t.Cleanup(func() { tracing.Init(false) })

	// httptest.Server that always returns 500 — triggers the claim-error path.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/agent/commands/claim" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	// Build a minimal supervisor wired to the 500 server. Use a very short
	// backoff step so the Sleep exits almost immediately via context cancel.
	s := &Supervisor{
		cfg: Config{
			BaseURL:          srv.URL,
			Version:          "test",
			Concurrency:      1,
			ClaimWaitSeconds: 1,
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-span-test",
		orgID:            "org-span-test",
		provider:         noopProvider{},
		claimBackoff:     backoff.NewWithSteps([]time.Duration{10 * time.Millisecond}),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
	}

	// Run one iteration of the claim loop: the 500 response causes an error,
	// the span is recorded, and the backoff sleep exits when we cancel the context.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// Wait for at least one span to appear (the claim span is recorded before
	// the backoff sleep starts).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(exp.GetSpans()) > 0 {
			break
		}
		time.Sleep(5 * time.Millisecond) // reason: waiting for HTTP roundtrip; not durably blocked in synctest sense.
	}
	cancel() // cancel so claimBackoff.Sleep exits and the loop returns.
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
	if sp.Status.Code.String() != "Error" {
		t.Errorf("agent.claim span Status.Code: want Error, got %s", sp.Status.Code.String())
	}
	var sawException bool
	for _, ev := range sp.Events {
		if strings.Contains(ev.Name, "exception") {
			sawException = true
			break
		}
	}
	if !sawException {
		t.Errorf("expected RecordError exception event on agent.claim span, events: %v", sp.Events)
	}
}
