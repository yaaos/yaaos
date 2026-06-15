package supervisor

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// TestSupervisor_EventPostSpan_410StaleClaim verifies that an HTTP 410
// response closes the "agent.event_post" span with Status.Code = Unset
// (not an error — stale claim is a normal backend lifecycle signal),
// produces exactly one attempt (no retry), emits no exception event on
// the span, and returns nil from postTerminalEvent.
func TestSupervisor_EventPostSpan_410StaleClaim(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	const cmdID = "cmd-stale-test"

	// Track how many times the endpoint was called to confirm no retry.
	var callCount int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost &&
			strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events") {
			callCount++
			// Backend returns 410 Gone on a retired command row.
			w.WriteHeader(http.StatusGone)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg: Config{
			BaseURL: srv.URL,
			Version: "test",
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-stale-test",
		orgID:            "org-stale-test",
		provider:         noopProvider{},
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{10 * time.Millisecond},
		dedup:            newDedupCache(0),
	}

	header := protocol.CommandHeader{
		CommandID: cmdID,
		Kind:      protocol.KindInvokeClaudeCode,
	}
	event := protocol.AgentEvent{
		CommandID:  cmdID,
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}

	ctx := t.Context()
	err := s.postTerminalEvent(ctx, header, event)
	if err != nil {
		t.Fatalf("postTerminalEvent returned non-nil error on 410: %v", err)
	}

	// Exactly one attempt — stale claim must not retry.
	if callCount != 1 {
		t.Errorf("want exactly 1 POST attempt on 410, got %d", callCount)
	}

	spans := exp.GetSpans()
	// Collect indices of agent.event_post spans.
	var postIdxs []int
	for i := range spans {
		if spans[i].Name == "agent.event_post" {
			postIdxs = append(postIdxs, i)
		}
	}
	if len(postIdxs) == 0 {
		names := make([]string, len(spans))
		for i, sp := range spans {
			names[i] = sp.Name
		}
		t.Fatalf("no agent.event_post span found; all spans: %v", names)
	}
	if len(postIdxs) != 1 {
		t.Errorf("want exactly 1 agent.event_post span for single attempt, got %d", len(postIdxs))
	}
	sp := spans[postIdxs[0]]
	// Span must close Unset — stale claim is not a span error.
	if got := sp.Status.Code.String(); got != "Unset" {
		t.Errorf("agent.event_post span Status.Code on 410: want Unset, got %s", got)
	}
	// No exception event — 410 is not recorded as an error on the span.
	for _, ev := range sp.Events {
		if ev.Name == "exception" {
			t.Errorf("unexpected exception event on agent.event_post span for 410: event=%v", ev)
		}
	}
	// No command_event.outcome attribute — there is no ack body on a 410.
	for _, a := range sp.Attributes {
		if string(a.Key) == "command_event.outcome" {
			t.Errorf("unexpected command_event.outcome attribute on 410 span: %q", a.Value.AsString())
		}
	}
	// event_post.outcome must be stale_claim on 410.
	var eventPostOutcome string
	for _, a := range sp.Attributes {
		if string(a.Key) == "event_post.outcome" {
			eventPostOutcome = a.Value.AsString()
		}
	}
	if eventPostOutcome != "stale_claim" {
		t.Errorf("agent.event_post span event_post.outcome on 410: want stale_claim, got %q", eventPostOutcome)
	}
}

// TestSupervisor_EventPostSpan_TransientErrorThenSuccessRecordsBoth verifies
// that a 500 followed by 200 produces two "agent.event_post" spans — the first
// with Status.Code = Error, the second with Status.Code = Unset.
// This is the regression guard: suppressing 410 must not accidentally suppress
// real errors.
func TestSupervisor_EventPostSpan_TransientErrorThenSuccessRecordsBoth(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	const cmdID = "cmd-retry-test"

	attempt := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost &&
			strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events") {
			attempt++
			if attempt == 1 {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	s := &Supervisor{
		cfg: Config{
			BaseURL: srv.URL,
			Version: "test",
		},
		client:           protocol.NewClient(srv.URL, nil),
		log:              nullLogger{},
		agentID:          "agent-retry-test",
		orgID:            "org-retry-test",
		provider:         noopProvider{},
		claimBackoff:     backoff.New(),
		heartbeatBackoff: backoff.New(),
		wsBackoff:        backoff.New(),
		stsBackoff:       backoff.New(),
		eventPostSteps:   []time.Duration{1 * time.Millisecond},
		dedup:            newDedupCache(0),
	}

	header := protocol.CommandHeader{
		CommandID: cmdID,
		Kind:      protocol.KindInvokeClaudeCode,
	}
	event := protocol.AgentEvent{
		CommandID:  cmdID,
		Kind:       protocol.EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	}

	ctx := t.Context()
	err := s.postTerminalEvent(ctx, header, event)
	if err != nil {
		t.Fatalf("postTerminalEvent returned non-nil error after retry: %v", err)
	}

	spans := exp.GetSpans()
	var postIdxs []int
	for i := range spans {
		if spans[i].Name == "agent.event_post" {
			postIdxs = append(postIdxs, i)
		}
	}
	if len(postIdxs) != 2 {
		t.Errorf("want 2 agent.event_post spans (1 error + 1 success), got %d", len(postIdxs))
		for _, i := range postIdxs {
			t.Logf("  [%d] name=%s status=%s", i, spans[i].Name, spans[i].Status.Code.String())
		}
		return
	}
	// First span: the 500 → should be Error.
	if got := spans[postIdxs[0]].Status.Code.String(); got != "Error" {
		t.Errorf("first agent.event_post span (500): want Error, got %s", got)
	}
	// Second span: the 200 → should be Unset with event_recorded outcome.
	if got := spans[postIdxs[1]].Status.Code.String(); got != "Unset" {
		t.Errorf("second agent.event_post span (200): want Unset, got %s", got)
	}
	var commandEventOutcome string
	var eventPostOutcome string
	for _, a := range spans[postIdxs[1]].Attributes {
		switch string(a.Key) {
		case "command_event.outcome":
			commandEventOutcome = a.Value.AsString()
		case "event_post.outcome":
			eventPostOutcome = a.Value.AsString()
		}
	}
	if commandEventOutcome != "event_recorded" {
		t.Errorf("second span command_event.outcome: want event_recorded, got %q", commandEventOutcome)
	}
	if eventPostOutcome != "acked" {
		t.Errorf("second span event_post.outcome: want acked, got %q", eventPostOutcome)
	}
	// First span (500): event_post.outcome must be network_error.
	var firstEventPostOutcome string
	for _, a := range spans[postIdxs[0]].Attributes {
		if string(a.Key) == "event_post.outcome" {
			firstEventPostOutcome = a.Value.AsString()
		}
	}
	if firstEventPostOutcome != "network_error" {
		t.Errorf("first span event_post.outcome (500): want network_error, got %q", firstEventPostOutcome)
	}
}
