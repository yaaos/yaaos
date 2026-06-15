// Tests for claim.outcome span attributes, event_post.outcome span attributes,
// and the yaaos.agent.claim.outcome counter across all exit buckets.
package supervisor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/observability/observabilitytest"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// TestClaimOutcomeCommandStampedOnSuccess verifies that when the claim endpoint
// returns a decodable command, the finished agent.claim span carries
// claim.outcome="command".
func TestClaimOutcomeCommandStampedOnSuccess(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	// A claim endpoint that returns one valid ProvisionWorkspace command then 204.
	cmdPayload := mustMarshalProvisionCmd("ws-outcome-cmd", "cmd-outcome-cmd")
	var served int32

	// event server: accept all event POSTs with 200.
	eventCh := make(chan struct{}, 10)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/agent/commands/claim":
			if served == 0 {
				served++
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write(cmdPayload)
			} else {
				w.WriteHeader(http.StatusNoContent)
			}
		case strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events"):
			eventCh <- struct{}{}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	noopSpawn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
		return &stubSuccessRunner{}, nil
	}
	s := &Supervisor{
		cfg: Config{
			BaseURL:          srv.URL,
			Version:          "test",
			Concurrency:      1,
			ClaimWaitSeconds: 1,
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-outcome-cmd",
		orgID:            "org-outcome-cmd",
		provider:         noopProvider{},
		pool:             NewPool(noopSpawn, nil),
		claimBackoff:     backoff.NewWithSteps([]time.Duration{5 * time.Millisecond}),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{5 * time.Millisecond},
		dedup:            newDedupCache(dedupCacheSize),
	}
	s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// Wait for the dispatch goroutine to post its terminal event.
	select {
	case <-eventCh:
	case <-time.After(4 * time.Second):
		t.Fatal("timed out waiting for terminal event POST")
	}
	cancel()
	<-done

	spans := exp.GetSpans()
	// Find the agent.claim span whose claim.outcome == "command".
	var found bool
	for _, sp := range spans {
		if sp.Name != "agent.claim" {
			continue
		}
		for _, a := range sp.Attributes {
			if string(a.Key) == "claim.outcome" && a.Value.AsString() == "command" {
				found = true
			}
		}
	}
	if !found {
		t.Errorf("no agent.claim span with claim.outcome=command found; all claim spans:")
		for _, sp := range spans {
			if sp.Name == "agent.claim" {
				for _, a := range sp.Attributes {
					if string(a.Key) == "claim.outcome" {
						t.Logf("  claim.outcome=%s", a.Value.AsString())
					}
				}
			}
		}
	}
}

// stubSuccessRunner is a WorkspaceRunner whose Send returns immediately with
// a success event. Used to complete a ProvisionWorkspace dispatch without a
// real subprocess.
type stubSuccessRunner struct{}

func (r *stubSuccessRunner) Send(_ context.Context, cmd command.WorkspaceCommand, _ func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	return protocol.AgentEvent{
		CommandID:  cmd.Header().CommandID,
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}, nil
}

func (r *stubSuccessRunner) Close(_ context.Context) error { return nil }

// TestClaimOutcomeCounterIncrementsByBucket drives all four claimLoop exit
// paths and asserts the yaaos.agent.claim.outcome counter increments once per
// outcome bucket.
func TestClaimOutcomeCounterIncrementsByBucket(t *testing.T) {
	capture := observabilitytest.InstallTestMeterProvider(t)

	t.Run("no_command", func(t *testing.T) {
		exp := tracing.Init(true)
		defer exp.Reset()
		t.Cleanup(func() { tracing.Init(false) })

		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		}))
		defer srv.Close()

		s := &Supervisor{
			cfg:              Config{BaseURL: srv.URL, Version: "test", ClaimWaitSeconds: 1},
			client:           protocol.NewClient(srv.URL, nil),
			log:              nullLogger{},
			provider:         noopProvider{},
			claimBackoff:     backoff.NewWithSteps([]time.Duration{5 * time.Millisecond}),
			heartbeatBackoff: backoff.New(),
			wsBackoff:        backoff.New(),
			stsBackoff:       backoff.New(),
		}

		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		done := make(chan struct{})
		go func() {
			defer close(done)
			s.claimLoop(ctx, 0)
		}()

		// Wait for a span then cancel.
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			if len(exp.GetSpans()) > 0 {
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
		cancel()
		<-done
	})

	t.Run("error", func(t *testing.T) {
		exp := tracing.Init(true)
		defer exp.Reset()
		t.Cleanup(func() { tracing.Init(false) })

		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		}))
		defer srv.Close()

		s := &Supervisor{
			cfg:              Config{BaseURL: srv.URL, Version: "test", ClaimWaitSeconds: 1},
			client:           protocol.NewClient(srv.URL, nil),
			log:              nullLogger{},
			provider:         noopProvider{},
			claimBackoff:     backoff.NewWithSteps([]time.Duration{5 * time.Millisecond}),
			heartbeatBackoff: backoff.New(),
			wsBackoff:        backoff.New(),
			stsBackoff:       backoff.New(),
		}

		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		done := make(chan struct{})
		go func() {
			defer close(done)
			s.claimLoop(ctx, 0)
		}()

		// Wait for a span then cancel.
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			if len(exp.GetSpans()) > 0 {
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
		cancel()
		<-done
	})

	t.Run("cancel", func(t *testing.T) {
		exp := tracing.Init(true)
		defer exp.Reset()
		t.Cleanup(func() { tracing.Init(false) })

		unblock := make(chan struct{})
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			select {
			case <-r.Context().Done():
			case <-unblock:
			}
			w.WriteHeader(499)
		}))
		defer func() {
			close(unblock)
			srv.Close()
		}()

		s := &Supervisor{
			cfg:              Config{BaseURL: srv.URL, Version: "test", ClaimWaitSeconds: 1},
			client:           protocol.NewClient(srv.URL, nil),
			log:              nullLogger{},
			provider:         noopProvider{},
			claimBackoff:     backoff.NewWithSteps([]time.Duration{5 * time.Millisecond}),
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

		time.AfterFunc(50*time.Millisecond, cancel)
		<-done
	})

	t.Run("command", func(t *testing.T) {
		exp := tracing.Init(true)
		defer exp.Reset()
		t.Cleanup(func() { tracing.Init(false) })

		cmdPayload := mustMarshalProvisionCmd("ws-counter-cmd", "cmd-counter-cmd")
		var cmdServed bool
		eventCh := make(chan struct{}, 10)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch {
			case r.URL.Path == "/api/v1/agent/commands/claim":
				if !cmdServed {
					cmdServed = true
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusOK)
					_, _ = w.Write(cmdPayload)
				} else {
					w.WriteHeader(http.StatusNoContent)
				}
			case strings.Contains(r.URL.Path, "/api/v1/commands/") &&
				strings.HasSuffix(r.URL.Path, "/events"):
				eventCh <- struct{}{}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
			default:
				http.NotFound(w, r)
			}
		}))
		defer srv.Close()

		noopSpawn := func(_ context.Context, _ string) (WorkspaceRunner, error) {
			return &stubSuccessRunner{}, nil
		}
		s := &Supervisor{
			cfg: Config{
				BaseURL:          srv.URL,
				Version:          "test",
				Concurrency:      1,
				ClaimWaitSeconds: 1,
			},
			client:           protocol.NewClient(srv.URL, nil),
			log:              nullLogger{},
			agentID:          "agent-counter-cmd",
			orgID:            "org-counter-cmd",
			provider:         noopProvider{},
			pool:             NewPool(noopSpawn, nil),
			claimBackoff:     backoff.NewWithSteps([]time.Duration{5 * time.Millisecond}),
			heartbeatBackoff: backoff.New(),
			wsBackoff:        backoff.New(),
			stsBackoff:       backoff.New(),
			eventPostSteps:   []time.Duration{5 * time.Millisecond},
			dedup:            newDedupCache(dedupCacheSize),
		}
		s.ApplyConfig(command.AgentConfig{MaxWorkspaces: 10})

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		done := make(chan struct{})
		go func() {
			defer close(done)
			s.claimLoop(ctx, 0)
		}()

		select {
		case <-eventCh:
		case <-time.After(4 * time.Second):
			t.Fatal("timed out waiting for terminal event POST")
		}

		// Wait for the agent.claim span with claim.outcome=command.
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			found := false
			for _, sp := range exp.GetSpans() {
				if sp.Name != "agent.claim" {
					continue
				}
				for _, a := range sp.Attributes {
					if string(a.Key) == "claim.outcome" && a.Value.AsString() == "command" {
						found = true
					}
				}
			}
			if found {
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
		cancel()
		<-done
	})

	// After driving all four buckets, assert the counter has at least one
	// increment per outcome. Each sub-test runs sequentially and drives its own
	// bucket, but the count per bucket is not strictly deterministic — the cancel
	// sub-test can also produce a preceding no_command increment (the blocking
	// server returns only after unblock, so a claim may resolve 204 before the
	// ctx-cancel lands). Hence the assertion below is `>= 1`, not `== 1`.
	sums := capture.CounterSums(t, "yaaos.agent.claim.outcome", "outcome")
	for _, outcome := range []string{"no_command", "error", "cancel", "command"} {
		if sums[outcome] < 1 {
			t.Errorf("yaaos.agent.claim.outcome{outcome=%s}: want >= 1, got %d (all sums: %v)",
				outcome, sums[outcome], sums)
		}
	}
}

// TestEventPostOutcomeAckedOn200 verifies that a 200 response stamps
// event_post.outcome="acked" on the agent.event_post span.
func TestEventPostOutcomeAckedOn200(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg:              Config{BaseURL: srv.URL, Version: "test"},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		provider:         noopProvider{},
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{5 * time.Millisecond},
		dedup:            newDedupCache(0),
	}

	header := protocol.CommandHeader{
		CommandID: "cmd-acked-test",
		Kind:      protocol.KindInvokeClaudeCode,
	}
	event := protocol.AgentEvent{
		CommandID:  "cmd-acked-test",
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}

	if err := s.postTerminalEvent(t.Context(), header, event); err != nil {
		t.Fatalf("postTerminalEvent: %v", err)
	}

	spans := exp.GetSpans()
	var outcome string
	for _, sp := range spans {
		if sp.Name != "agent.event_post" {
			continue
		}
		for _, a := range sp.Attributes {
			if string(a.Key) == "event_post.outcome" {
				outcome = a.Value.AsString()
			}
		}
		break
	}
	if outcome != "acked" {
		t.Errorf("event_post.outcome on 200: want acked, got %q", outcome)
	}
}

// TestEventPostOutcomeStaleClaimOn410 verifies that a 410 response stamps
// event_post.outcome="stale_claim" on the agent.event_post span.
func TestEventPostOutcomeStaleClaimOn410(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg:              Config{BaseURL: srv.URL, Version: "test"},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		provider:         noopProvider{},
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{5 * time.Millisecond},
		dedup:            newDedupCache(0),
	}

	header := protocol.CommandHeader{
		CommandID: "cmd-stale-outcome-test",
		Kind:      protocol.KindInvokeClaudeCode,
	}
	event := protocol.AgentEvent{
		CommandID:  "cmd-stale-outcome-test",
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}

	if err := s.postTerminalEvent(t.Context(), header, event); err != nil {
		t.Fatalf("postTerminalEvent: %v", err)
	}

	spans := exp.GetSpans()
	var outcome string
	for _, sp := range spans {
		if sp.Name != "agent.event_post" {
			continue
		}
		for _, a := range sp.Attributes {
			if string(a.Key) == "event_post.outcome" {
				outcome = a.Value.AsString()
			}
		}
	}
	if outcome != "stale_claim" {
		t.Errorf("event_post.outcome on 410: want stale_claim, got %q", outcome)
	}
}

// TestEventPostOutcomeNetworkError verifies that a transient transport error
// stamps event_post.outcome="network_error" on the agent.event_post span.
func TestEventPostOutcomeNetworkError(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	// Server returns 500 on first attempt, then 200 so the loop exits.
	attempt := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempt++
		if attempt == 1 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg:              Config{BaseURL: srv.URL, Version: "test"},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		provider:         noopProvider{},
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{1 * time.Millisecond},
		dedup:            newDedupCache(0),
	}

	header := protocol.CommandHeader{
		CommandID: "cmd-network-error-test",
		Kind:      protocol.KindInvokeClaudeCode,
	}
	event := protocol.AgentEvent{
		CommandID:  "cmd-network-error-test",
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}

	if err := s.postTerminalEvent(t.Context(), header, event); err != nil {
		t.Fatalf("postTerminalEvent: %v", err)
	}

	// Find the first agent.event_post span (500 attempt) and assert network_error.
	spans := exp.GetSpans()
	var firstPostOutcome string
	for _, sp := range spans {
		if sp.Name == "agent.event_post" {
			for _, a := range sp.Attributes {
				if string(a.Key) == "event_post.outcome" {
					firstPostOutcome = a.Value.AsString()
					break
				}
			}
			break // only check the first span
		}
	}
	if firstPostOutcome != "network_error" {
		t.Errorf("event_post.outcome on first attempt (500): want network_error, got %q", firstPostOutcome)
	}
}
