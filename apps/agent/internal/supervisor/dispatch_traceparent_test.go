// Tests that routeCommand parents supervisor.dispatch.<kind> under the span
// encoded in the command's traceparent field — i.e. the agent_command.dispatch
// span written by the backend's enqueue_command function.
package supervisor

import (
	"context"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

// buildSupervisorForTraceparentTest builds a minimal Supervisor whose event
// server accepts all POSTs with 204.
func buildSupervisorForTraceparentTest(t *testing.T) *Supervisor {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	spawnFn := inProcessSpawn(workspacetest.StubHandler{})
	cfg := Config{
		BaseURL:               srv.URL,
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
		agentID:          "agent-tp-test",
		orgID:            "org-tp-test",
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

// makeTraceparentWithKnownSpanID returns a well-formed W3C traceparent string
// with the given 16-hex span-id. The trace-id is fixed for reproducibility.
func makeTraceparentWithKnownSpanID(spanID string) string {
	return fmt.Sprintf("00-00112233445566778899aabbccddeeff-%s-01", spanID)
}

// TestRouteCommand_DispatchSpan_ParentedToCommandTraceparent asserts that the
// supervisor.dispatch.<kind> span's parent span-id equals the span-id encoded
// in the command's Traceparent field. This verifies the chain:
//
//	backend: agent_command.dispatch.ProvisionWorkspace (span S)
//	  → writes traceparent carrying S into agent_commands.payload
//	agent: routeCommand reads that traceparent, ExtractContext, opens
//	  supervisor.dispatch.ProvisionWorkspace whose parent == S.
func TestRouteCommand_DispatchSpan_ParentedToCommandTraceparent(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	s := buildSupervisorForTraceparentTest(t)
	defer s.pool.CloseAll(context.Background())

	// A known span-id that simulates the backend's agent_command.dispatch span.
	backendSpanID := "aabbccdd11223344"
	// Sanity-check it's valid 16-hex.
	if _, err := hex.DecodeString(backendSpanID); err != nil || len(backendSpanID) != 16 {
		t.Fatalf("test setup: backendSpanID must be 16 hex chars, got %q", backendSpanID)
	}
	commandTraceparent := makeTraceparentWithKnownSpanID(backendSpanID)

	cmd := &command.ProvisionWorkspaceCommand{
		Proto: protocol.ProvisionWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-tp-test-1",
				WorkspaceID: "ws-tp-test-1",
				Traceparent: commandTraceparent,
				Kind:        protocol.KindProvisionWorkspace,
			},
		},
	}

	s.routeCommand(context.Background(), cmd)

	spans := exp.GetSpans()
	var dispatchIdx = -1
	for i := range spans {
		if spans[i].Name == "supervisor.dispatch.ProvisionWorkspace" {
			dispatchIdx = i
			break
		}
	}
	if dispatchIdx < 0 {
		names := make([]string, len(spans))
		for i, sp := range spans {
			names[i] = sp.Name
		}
		t.Fatalf("no supervisor.dispatch.ProvisionWorkspace span; all spans: %v", names)
	}
	dispatchSpan := spans[dispatchIdx]

	// The dispatch span's parent must be the backend's span, not a root.
	if !dispatchSpan.Parent.IsValid() {
		t.Fatal("supervisor.dispatch span has no parent; expected parent == backend dispatch span")
	}
	parentSpanID := dispatchSpan.Parent.SpanID()
	gotParentSpanID := hex.EncodeToString(parentSpanID[:])
	if gotParentSpanID != backendSpanID {
		t.Errorf(
			"supervisor.dispatch parent span-id: want %q (backend dispatch span), got %q",
			backendSpanID, gotParentSpanID,
		)
	}
}
