// Tests that workflow_execution_id on a CommandHeader is stamped as
// workflow_id on the supervisor.dispatch.<kind> span when present, and is
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

const testWorkflowID = "11111111-2222-3333-4444-555555555555"

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
		agentID:          "agent-wfid-test",
		orgID:            "org-wfid-test",
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

// cmdWithWorkflowID builds a ProvisionWorkspaceCommand whose header carries a
// WorkflowExecutionID.
func cmdWithWorkflowID(workspaceID, commandID, workflowID string) command.WorkspaceCommand {
	return &command.ProvisionWorkspaceCommand{
		Proto: protocol.ProvisionWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:           commandID,
				WorkspaceID:         workspaceID,
				Traceparent:         "tp-" + commandID,
				Kind:                protocol.KindProvisionWorkspace,
				WorkflowExecutionID: workflowID,
			},
		},
	}
}

// TestRouteCommand_WorkflowID_PresentOnSpan verifies that when the
// CommandHeader carries a non-empty WorkflowExecutionID, the
// supervisor.dispatch.<kind> span carries a `workflow_id` attribute equal to
// that value.
func TestRouteCommand_WorkflowID_PresentOnSpan(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	s := buildSupervisorForSpanTest(t)
	defer s.pool.CloseAll(context.Background())

	cmd := cmdWithWorkflowID("ws-wf-1", "cmd-wf-1", testWorkflowID)
	s.routeCommand(context.Background(), cmd)

	spans := exp.GetSpans()
	var found bool
	var gotWorkflowID string
	for i := range spans {
		if spans[i].Name == "supervisor.dispatch.ProvisionWorkspace" {
			found = true
			for _, kv := range spans[i].Attributes {
				if string(kv.Key) == "workflow_id" {
					gotWorkflowID = kv.Value.AsString()
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
	if gotWorkflowID != testWorkflowID {
		t.Errorf("workflow_id attr: want %q, got %q", testWorkflowID, gotWorkflowID)
	}
}

// TestRouteCommand_WorkflowID_AbsentWhenEmpty verifies that when the
// CommandHeader has an empty WorkflowExecutionID, the dispatch span does NOT
// carry a `workflow_id` attribute.
func TestRouteCommand_WorkflowID_AbsentWhenEmpty(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	s := buildSupervisorForSpanTest(t)
	defer s.pool.CloseAll(context.Background())

	// newCreateCmd sets WorkflowExecutionID to "" (empty string).
	cmd := newCreateCmd("ws-nowf-1", "cmd-nowf-1")
	s.routeCommand(context.Background(), cmd)

	spans := exp.GetSpans()
	var found bool
	for i := range spans {
		if spans[i].Name == "supervisor.dispatch.ProvisionWorkspace" {
			found = true
			for _, kv := range spans[i].Attributes {
				if string(kv.Key) == "workflow_id" {
					t.Errorf("workflow_id attr must be absent when WorkflowExecutionID is empty; got %q", kv.Value.AsString())
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
