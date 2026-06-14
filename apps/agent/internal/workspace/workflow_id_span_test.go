// Tests that WorkflowExecutionID on a CommandHeader is stamped as workflow_id
// on the workspace.handle.<kind> span when present, and is absent when empty.
package workspace

import (
	"context"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

const testWorkflowIDWorkspace = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

func fixedTime() time.Time { return time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC) }

// buildCleanupCmd builds a CleanupWorkspaceCommand for use in span tests.
func buildCleanupCmd(workspaceID, commandID, workflowID string) command.WorkspaceCommand {
	return &command.CleanupWorkspaceCommand{
		Proto: protocol.CleanupWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:           commandID,
				WorkspaceID:         workspaceID,
				Traceparent:         "tp-" + commandID,
				Kind:                protocol.KindCleanupWorkspace,
				WorkflowExecutionID: workflowID,
			},
		},
	}
}

// TestExecuteCommand_WorkflowID_PresentOnSpan verifies that when a command
// carries a non-empty WorkflowExecutionID, the workspace.handle.<kind> span
// carries a `workflow_id` attribute equal to that value.
func TestExecuteCommand_WorkflowID_PresentOnSpan(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	cmd := buildCleanupCmd("ws-wf-exec-1", "cmd-wf-exec-1", testWorkflowIDWorkspace)
	ops := workspacetest.StubHandler{}
	executeCommand(context.Background(), cmd, ops, fixedTime(), nil)

	spans := exp.GetSpans()
	var found bool
	var gotWorkflowID string
	for i := range spans {
		if spans[i].Name == "workspace.handle.CleanupWorkspace" {
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
		t.Fatalf("no workspace.handle.CleanupWorkspace span; all spans: %v", names)
	}
	if gotWorkflowID != testWorkflowIDWorkspace {
		t.Errorf("workflow_id attr: want %q, got %q", testWorkflowIDWorkspace, gotWorkflowID)
	}
}

// TestExecuteCommand_WorkflowID_AbsentWhenEmpty verifies that when the
// CommandHeader has an empty WorkflowExecutionID, the workspace.handle.<kind>
// span does NOT carry a `workflow_id` attribute.
func TestExecuteCommand_WorkflowID_AbsentWhenEmpty(t *testing.T) {
	exp := tracing.Init(true)
	defer exp.Reset()
	t.Cleanup(func() { tracing.Init(false) })

	// WorkflowExecutionID is empty string (zero value).
	cmd := buildCleanupCmd("ws-nowf-exec-1", "cmd-nowf-exec-1", "")
	ops := workspacetest.StubHandler{}
	executeCommand(context.Background(), cmd, ops, fixedTime(), nil)

	spans := exp.GetSpans()
	var found bool
	for i := range spans {
		if spans[i].Name == "workspace.handle.CleanupWorkspace" {
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
		t.Fatalf("no workspace.handle.CleanupWorkspace span; all spans: %v", names)
	}
}
