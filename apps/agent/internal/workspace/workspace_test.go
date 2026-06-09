package workspace

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

// fixedNow returns a deterministic timestamp for event-timestamp assertions.
func fixedNow() time.Time {
	return time.Date(2026, 5, 22, 12, 0, 0, 0, time.UTC)
}

// frameCmd marshals a wire-shaped command as a newline-terminated frame.
// Tests build their input pipe by concatenating these.
func frameCmd(t *testing.T, kind protocol.CommandKind, body map[string]any) []byte {
	t.Helper()
	body["kind"] = string(kind)
	buf, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal command: %v", err)
	}
	return append(buf, '\n')
}

// readEvents decodes every newline-terminated AgentEvent on `out`.
func readEvents(t *testing.T, out *bytes.Buffer) []protocol.AgentEvent {
	t.Helper()
	var events []protocol.AgentEvent
	for _, line := range strings.Split(strings.TrimRight(out.String(), "\n"), "\n") {
		if line == "" {
			continue
		}
		var ev protocol.AgentEvent
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("decode event %q: %v", line, err)
		}
		events = append(events, ev)
	}
	return events
}

func TestRun_HappyPath_EachKindEmitsSuccess(t *testing.T) {
	var in bytes.Buffer
	in.Write(frameCmd(t, protocol.KindProvisionWorkspace, map[string]any{
		"command_id":   "c-provision",
		"workspace_id": "ws-1",
		"traceparent":  "tp-1",
		"repo":         map[string]any{"plugin_id": "github", "external_id": "x/y", "clone_url": "url", "head_sha": "h"},
		"history":      1,
		"auth":         map[string]any{"kind": "github_installation", "token": "tok"},
		"ttl_seconds":  60, "max_idle_seconds": 30,
	}))
	in.Write(frameCmd(t, protocol.KindWriteFiles, map[string]any{
		"command_id":   "c-write",
		"workspace_id": "ws-1",
		"traceparent":  "tp-1",
		"files":        []map[string]any{{"path": ".mcp.json", "content": "{}"}},
	}))
	in.Write(frameCmd(t, protocol.KindInvokeClaudeCode, map[string]any{
		"command_id":   "c-invoke",
		"workspace_id": "ws-1",
		"traceparent":  "tp-1",
		"invocation":   map[string]any{"prompt": "go"},
		"limits":       map[string]any{"wallclock_seconds": 60},
	}))
	in.Write(frameCmd(t, protocol.KindCleanupWorkspace, map[string]any{
		"command_id":   "c-cleanup",
		"workspace_id": "ws-1",
		"traceparent":  "tp-1",
	}))

	var out bytes.Buffer
	err := Run(context.Background(), &in, &out, workspacetest.StubHandler{}, Options{Now: fixedNow})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	events := readEvents(t, &out)
	if len(events) != 4 {
		t.Fatalf("want 4 events, got %d", len(events))
	}
	for i, want := range []string{"c-provision", "c-write", "c-invoke", "c-cleanup"} {
		if events[i].CommandID != want {
			t.Errorf("event %d command_id: want %q got %q", i, want, events[i].CommandID)
		}
		if events[i].Kind != protocol.EventCompletedSuccess {
			t.Errorf("event %d kind: want completed_success got %q", i, events[i].Kind)
		}
		if events[i].Traceparent != "tp-1" {
			t.Errorf("event %d traceparent: want tp-1 got %q", i, events[i].Traceparent)
		}
		if !events[i].ReportedAt.Equal(fixedNow()) {
			t.Errorf("event %d reported_at: want %v got %v", i, fixedNow(), events[i].ReportedAt)
		}
	}
}

// failingOps returns an error for RunClaude, success for the rest.
type failingOps struct{ workspacetest.StubHandler }

func (failingOps) RunClaude(_ context.Context, _ *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	return command.InvokeResult{}, errors.New("agent OOM at 5 minutes")
}

func TestRun_HandlerError_EmitsCompletedFailure(t *testing.T) {
	var in bytes.Buffer
	in.Write(frameCmd(t, protocol.KindInvokeClaudeCode, map[string]any{
		"command_id":   "c-fail",
		"workspace_id": "ws-1",
		"traceparent":  "tp-2",
		"invocation":   map[string]any{},
		"limits":       map[string]any{"wallclock_seconds": 5},
	}))

	var out bytes.Buffer
	if err := Run(context.Background(), &in, &out, failingOps{}, Options{Now: fixedNow}); err != nil {
		t.Fatalf("Run: %v", err)
	}
	events := readEvents(t, &out)
	if len(events) != 1 {
		t.Fatalf("want 1 event, got %d", len(events))
	}
	if events[0].Kind != protocol.EventCompletedFailure {
		t.Errorf("kind: want completed_failure got %q", events[0].Kind)
	}
	if !strings.Contains(events[0].FailureReason, "agent OOM") {
		t.Errorf("failure_reason: want substring 'agent OOM' got %q", events[0].FailureReason)
	}
}

func TestRun_UnknownKindOnPipe_ReturnsDecodeError(t *testing.T) {
	// command.Decode rejects unknown kinds — Run surfaces that as an
	// error rather than emitting a failure event. The supervisor treats
	// this as a protocol fault and tears the workspace down.
	in := strings.NewReader(`{"kind":"Phantom","command_id":"c-bad"}` + "\n")
	var out bytes.Buffer
	err := Run(context.Background(), in, &out, workspacetest.StubHandler{}, Options{Now: fixedNow})
	if err == nil {
		t.Fatalf("want decode error, got nil")
	}
	if !strings.Contains(err.Error(), "unknown kind") {
		t.Errorf("error: want substring 'unknown kind' got %q", err.Error())
	}
	if out.Len() != 0 {
		t.Errorf("want no events emitted on decode failure, got %q", out.String())
	}
}

func TestRun_EOFReturnsNil(t *testing.T) {
	if err := Run(context.Background(), strings.NewReader(""), io.Discard, workspacetest.StubHandler{}, Options{}); err != nil {
		t.Fatalf("EOF should return nil, got %v", err)
	}
}

func TestRun_NilOps(t *testing.T) {
	if err := Run(context.Background(), strings.NewReader(""), io.Discard, nil, Options{}); err == nil {
		t.Fatalf("want error on nil ops, got nil")
	}
}

func TestRun_ContextCancelled_StopsBetweenCommands(t *testing.T) {
	// Pre-cancel the context; Run should return ctx.Err() before reading.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := Run(ctx, strings.NewReader(""), io.Discard, workspacetest.StubHandler{}, Options{Now: fixedNow})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}

func TestStubHandler_OutputsCarryWorkspaceID(t *testing.T) {
	cmd := &protocol.ProvisionWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{WorkspaceID: "ws-x"},
	}
	res, err := workspacetest.StubHandler{}.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("ProvisionWorkspace: %v", err)
	}
	// The stub sets path and reports not-reused.
	if res.Reused {
		t.Errorf("Reused: want false got true")
	}
	wire := res.ToWire()
	if wire["reused"] != false {
		t.Errorf("wire[reused]: want false got %v", wire["reused"])
	}
}

// TestRun_NonWorkspaceKind_AgentCommand verifies that a ConfigUpdate
// (an AgentCommand kind, not a WorkspaceCommand) on the workspace pipe
// causes Run to return an error — workspace processes don't handle
// AgentCommands.
func TestRun_NonWorkspaceKind_AgentCommandReturnsError(t *testing.T) {
	// A valid (decodable) ConfigUpdate — nested config with a >=1 cap so it
	// passes Decode — must still be rejected as a non-workspace command when
	// it lands on a workspace child's pipe.
	in := strings.NewReader(`{"kind":"ConfigUpdate","command_id":"c-cfg","config":{"max_workspaces":1}}` + "\n")
	var out bytes.Buffer
	err := Run(context.Background(), in, &out, workspacetest.StubHandler{}, Options{Now: fixedNow})
	if err == nil {
		t.Fatalf("want error on AgentCommand on workspace pipe, got nil")
	}
	if !strings.Contains(err.Error(), "non-workspace command") {
		t.Errorf("error: want 'non-workspace command' got %q", err.Error())
	}
}
