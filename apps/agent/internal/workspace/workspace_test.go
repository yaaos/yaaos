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

	"github.com/yaaos/agent/internal/protocol"
)

// fixedNow returns a deterministic timestamp for event-timestamp assertions.
func fixedNow() time.Time {
	return time.Date(2026, 5, 22, 12, 0, 0, 0, time.UTC)
}

// frameCmd marshals a wire-shaped AgentCommand as a newline-terminated
// frame. Tests build their input pipe by concatenating these.
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
	in.Write(frameCmd(t, protocol.KindCreateWorkspace, map[string]any{
		"command_id":   "c-create",
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
	err := Run(context.Background(), &in, &out, StubHandler{}, Options{Now: fixedNow})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	events := readEvents(t, &out)
	if len(events) != 4 {
		t.Fatalf("want 4 events, got %d", len(events))
	}
	for i, want := range []string{"c-create", "c-write", "c-invoke", "c-cleanup"} {
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

// failingHandler returns an error for InvokeClaudeCode, success for the rest.
type failingHandler struct{ StubHandler }

func (failingHandler) InvokeClaudeCode(_ context.Context, _ *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	return nil, errors.New("agent OOM at 5 minutes")
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
	if err := Run(context.Background(), &in, &out, failingHandler{}, Options{Now: fixedNow}); err != nil {
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

func TestRun_UnknownKind_DispatchEmitsFailure(t *testing.T) {
	// AgentCommand.UnmarshalJSON refuses unknown kinds upstream of dispatch,
	// so to exercise the dispatch-level fallback we call dispatch() directly
	// with a hand-built unknown kind.
	cmd := &protocol.AgentCommand{Kind: protocol.CommandKind("Phantom")}
	ev := dispatch(context.Background(), cmd, StubHandler{}, fixedNow(), nil)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure, got %q", ev.Kind)
	}
	if !strings.Contains(ev.FailureReason, "unknown command kind") {
		t.Errorf("failure_reason: want substring 'unknown command kind' got %q", ev.FailureReason)
	}
}

func TestRun_UnknownKindOnPipe_ReturnsDecodeError(t *testing.T) {
	// The wire-level decoder rejects unknown kinds — Run surfaces that
	// rather than emitting a failure event. The supervisor treats this as
	// a protocol fault and tears the workspace down.
	in := strings.NewReader(`{"kind":"Phantom","command_id":"c-bad"}` + "\n")
	var out bytes.Buffer
	err := Run(context.Background(), in, &out, StubHandler{}, Options{Now: fixedNow})
	if err == nil {
		t.Fatalf("want decode error, got nil")
	}
	if !strings.Contains(err.Error(), "unknown command kind") {
		t.Errorf("error: want substring 'unknown command kind' got %q", err.Error())
	}
	if out.Len() != 0 {
		t.Errorf("want no events emitted on decode failure, got %q", out.String())
	}
}

func TestRun_EOFReturnsNil(t *testing.T) {
	if err := Run(context.Background(), strings.NewReader(""), io.Discard, StubHandler{}, Options{}); err != nil {
		t.Fatalf("EOF should return nil, got %v", err)
	}
}

func TestRun_NilHandler(t *testing.T) {
	if err := Run(context.Background(), strings.NewReader(""), io.Discard, nil, Options{}); err == nil {
		t.Fatalf("want error on nil handler, got nil")
	}
}

func TestRun_ContextCancelled_StopsBetweenCommands(t *testing.T) {
	// Pre-cancel the context; Run should return ctx.Err() before reading.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := Run(ctx, strings.NewReader(""), io.Discard, StubHandler{}, Options{Now: fixedNow})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}

func TestStubHandler_OutputsCarryWorkspaceID(t *testing.T) {
	cmd := &protocol.CreateWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{WorkspaceID: "ws-x"},
	}
	out, err := StubHandler{}.CreateWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("CreateWorkspace: %v", err)
	}
	if out["workspace_id"] != "ws-x" {
		t.Errorf("workspace_id: want ws-x got %v", out["workspace_id"])
	}
	if out["status"] != "created" {
		t.Errorf("status: want created got %v", out["status"])
	}
}
