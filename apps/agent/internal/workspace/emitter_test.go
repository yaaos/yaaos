package workspace

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

func TestContextWithEmitter_RoundTrip(t *testing.T) {
	var got map[string]any
	e := funcEmitter(func(outs map[string]any) bool {
		got = outs
		return true
	})
	ctx := ContextWithEmitter(context.Background(), e)
	pulled := EmitterFromContext(ctx)
	pulled.Progress(map[string]any{"k": "v"})
	if got["k"] != "v" {
		t.Errorf("Progress didn't call callback, got %v", got)
	}
}

func TestEmitterFromContext_DefaultIsNoop(t *testing.T) {
	e := EmitterFromContext(context.Background())
	// noopEmitter never panics and always returns false.
	if e.Progress(map[string]any{}) {
		t.Errorf("noop emitter should return false")
	}
}

func TestEncoderEmitter_WritesProgressFrames(t *testing.T) {
	var buf bytes.Buffer
	enc := ipc.NewEncoder(&buf)
	e := newEncoderEmitter(enc, "c-1", "tp-1", "tok-123", nil)

	ok := e.Progress(map[string]any{"stream_line": "{\"type\":\"tool_use\"}"})
	if !ok {
		t.Fatalf("Progress should succeed on a healthy encoder")
	}
	// Parse the written frame.
	var ev protocol.AgentEvent
	if err := json.Unmarshal(bytes.TrimSpace(buf.Bytes()), &ev); err != nil {
		t.Fatalf("decode emitted frame: %v\nbytes: %s", err, buf.String())
	}
	if ev.Kind != protocol.EventProgress {
		t.Errorf("kind: want progress got %q", ev.Kind)
	}
	if ev.CommandID != "c-1" {
		t.Errorf("command_id: want c-1 got %q", ev.CommandID)
	}
	if ev.Traceparent != "tp-1" {
		t.Errorf("traceparent: want tp-1 got %q", ev.Traceparent)
	}
	if ev.CompletionToken != "tok-123" {
		t.Errorf("completion_token: want tok-123 got %q", ev.CompletionToken)
	}
	if ev.Outputs["stream_line"] != "{\"type\":\"tool_use\"}" {
		t.Errorf("outputs.stream_line: got %v", ev.Outputs["stream_line"])
	}
}

// emittingHandler emits 3 progress events then succeeds. Drives the
// multi-event dispatch path end-to-end. Implements command.WorkspaceOps
// by embedding StubHandler for all ops except RunClaude.
type emittingHandler struct{ workspacetest.StubHandler }

func (emittingHandler) RunClaude(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	e := EmitterFromContext(ctx)
	for i := 0; i < 3; i++ {
		e.Progress(map[string]any{"i": i, "workspace_id": cmd.WorkspaceID})
	}
	return command.InvokeResult{WorkspaceID: cmd.WorkspaceID}, nil
}

func TestRun_MultiEventEmission_ProgressThenTerminal(t *testing.T) {
	// Drive workspace.Run with one InvokeClaudeCode command; the
	// handler emits 3 progress events + a terminal success. The
	// recipient (us) reads 4 framed AgentEvents off the event pipe in
	// that order.
	var in bytes.Buffer
	cmdBytes, _ := json.Marshal(map[string]any{
		"command_id":       "c-invoke",
		"workspace_id":     "ws-1",
		"traceparent":      "tp-1",
		"completion_token": "tok-invoke",
		"kind":             "InvokeClaudeCode",
		"invocation":       map[string]any{},
		"limits":           map[string]any{"wallclock_seconds": 60},
	})
	in.Write(cmdBytes)
	in.WriteByte('\n')

	var out bytes.Buffer
	if err := Run(context.Background(), &in, &out, emittingHandler{}, Options{}); err != nil {
		t.Fatalf("Run: %v", err)
	}

	// Decode all events emitted (progress + terminal).
	dec := ipc.NewDecoder(bytes.NewReader(out.Bytes()))
	var events []protocol.AgentEvent
	for {
		var ev protocol.AgentEvent
		err := dec.Read(&ev)
		if err != nil {
			break
		}
		events = append(events, ev)
	}
	if len(events) != 4 {
		t.Fatalf("want 4 events (3 progress + 1 terminal), got %d", len(events))
	}
	for i := 0; i < 3; i++ {
		if events[i].Kind != protocol.EventProgress {
			t.Errorf("event %d: want progress got %q", i, events[i].Kind)
		}
		if events[i].CommandID != "c-invoke" {
			t.Errorf("event %d: command_id mismatch", i)
		}
		if events[i].CompletionToken != "tok-invoke" {
			t.Errorf("event %d: completion_token want tok-invoke got %q", i, events[i].CompletionToken)
		}
		// Outputs.i may decode as float64 via json.Unmarshal — accept both.
		if got := events[i].Outputs["i"]; got != float64(i) && got != i {
			t.Errorf("event %d: outputs.i want %d got %v", i, i, got)
		}
	}
	if events[3].Kind != protocol.EventCompletedSuccess {
		t.Errorf("terminal: want completed_success got %q", events[3].Kind)
	}
	if events[3].CompletionToken != "tok-invoke" {
		t.Errorf("terminal: completion_token want tok-invoke got %q", events[3].CompletionToken)
	}
	// Terminal outputs come from InvokeResult.ToWire() — assert the
	// workspace_id field which is always present.
	if events[3].Outputs["workspace_id"] != "ws-1" {
		t.Errorf("terminal outputs: want workspace_id=ws-1 got %v", events[3].Outputs)
	}
}

func TestEncoderEmitter_ConcurrentProgressIsSafe(t *testing.T) {
	// ipc.Encoder serializes writes; concurrent Progress callers must
	// produce well-ordered (non-interleaved) frames. Drive 100
	// goroutines hammering Progress + assert each frame is parseable.
	var buf bytes.Buffer
	enc := ipc.NewEncoder(&buf)
	e := newEncoderEmitter(enc, "c-1", "tp-1", "", nil)

	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			e.Progress(map[string]any{"i": i})
		}(i)
	}
	wg.Wait()

	// Every frame must parse. If writes interleaved, json.Unmarshal
	// would fail on at least one.
	count := 0
	for _, line := range strings.Split(strings.TrimRight(buf.String(), "\n"), "\n") {
		if line == "" {
			continue
		}
		var ev protocol.AgentEvent
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Errorf("frame %d unparseable: %v\nline: %s", count, err, line)
		}
		count++
	}
	if count != 100 {
		t.Errorf("want 100 frames, got %d", count)
	}
}

// funcEmitter is a test adapter: a function value satisfies Emitter.
type funcEmitter func(map[string]any) bool

func (f funcEmitter) Progress(outs map[string]any) bool { return f(outs) }
