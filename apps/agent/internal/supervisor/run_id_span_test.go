// Tests that run_id on a CommandHeader is stamped as
// run_id on the supervisor.dispatch.<kind> span when present, and is
// absent from the span when the header carries an empty string.
package supervisor

import (
	"context"
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

const testRunID = "11111111-2222-3333-4444-555555555555"

// buildSupervisorForSpanTest constructs a minimal Supervisor backed by a
// stub server that accepts event POSTs and an in-process workspace runner.
func buildSupervisorForSpanTest(t *testing.T) *Supervisor {
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
		agentID:          "agent-rid-test",
		orgID:            "org-rid-test",
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

// cmdWithRunID builds a ProvisionWorkspaceCommand whose header carries a
// RunID.
func cmdWithRunID(workspaceID, commandID, runID string) command.WorkspaceCommand {
	return &command.ProvisionWorkspaceCommand{
		Proto: protocol.ProvisionWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   commandID,
				WorkspaceID: workspaceID,
				Traceparent: "tp-" + commandID,
				Kind:        protocol.KindProvisionWorkspace,
				RunID:       runID,
			},
		},
	}
}

// TestRouteCommand_RunID_PresentOnSpan verifies that when the
// CommandHeader carries a non-empty RunID, the
// supervisor.dispatch.<kind> span carries a `run_id` attribute equal to
// that value.
func TestRouteCommand_RunID_PresentOnSpan(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	s := buildSupervisorForSpanTest(t)
	defer s.pool.CloseAll(context.Background())

	cmd := cmdWithRunID("ws-wf-1", "cmd-wf-1", testRunID)
	s.routeCommand(context.Background(), cmd)

	spans := exp.GetSpans()
	var found bool
	var gotRunID string
	for i := range spans {
		if spans[i].Name == "supervisor.dispatch.ProvisionWorkspace" {
			found = true
			for _, kv := range spans[i].Attributes {
				if string(kv.Key) == "run_id" {
					gotRunID = kv.Value.AsString()
					break
				}
			}
			break
		}
	}
	if !found {
		names := make([]string, len(spans))
		for i, sp := range spans {
			names[i] = sp.Name
		}
		t.Fatalf("no supervisor.dispatch.ProvisionWorkspace span; all spans: %v", names)
	}
	if gotRunID != testRunID {
		t.Errorf("run_id attr: want %q, got %q", testRunID, gotRunID)
	}
}

// TestRouteCommand_RunID_AbsentWhenEmpty verifies that when the
// CommandHeader has an empty RunID, the dispatch span does NOT
// carry a `run_id` attribute.
func TestRouteCommand_RunID_AbsentWhenEmpty(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	s := buildSupervisorForSpanTest(t)
	defer s.pool.CloseAll(context.Background())

	// newCreateCmd sets RunID to "" (empty string).
	cmd := newCreateCmd("ws-nowf-1", "cmd-nowf-1")
	s.routeCommand(context.Background(), cmd)

	spans := exp.GetSpans()
	var found bool
	for i := range spans {
		if spans[i].Name == "supervisor.dispatch.ProvisionWorkspace" {
			found = true
			for _, kv := range spans[i].Attributes {
				if string(kv.Key) == "run_id" {
					t.Errorf("run_id attr must be absent when RunID is empty; got %q", kv.Value.AsString())
				}
			}
			break
		}
	}
	if !found {
		names := make([]string, len(spans))
		for i, sp := range spans {
			names[i] = sp.Name
		}
		t.Fatalf("no supervisor.dispatch.ProvisionWorkspace span; all spans: %v", names)
	}
}
