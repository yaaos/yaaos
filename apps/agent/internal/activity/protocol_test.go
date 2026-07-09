package activity

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/yaaos/agent/internal/protocol"
)

func TestDecodeInbound_Subscribe(t *testing.T) {
	raw := []byte(`{"type":"subscribe","workspace_id":"ws-1","run_id":"wf-1"}`)
	msg, err := DecodeInbound(raw)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if msg.Kind != InboundSubscribe {
		t.Errorf("kind: want subscribe got %q", msg.Kind)
	}
	if msg.WorkspaceID != "ws-1" {
		t.Errorf("workspace_id: got %q", msg.WorkspaceID)
	}
	if msg.RunID != "wf-1" {
		t.Errorf("run_id: got %q", msg.RunID)
	}
}

func TestDecodeInbound_Unsubscribe(t *testing.T) {
	raw := []byte(`{"type":"unsubscribe","workspace_id":"ws-1","run_id":"wf-1"}`)
	msg, err := DecodeInbound(raw)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if msg.Kind != InboundUnsubscribe {
		t.Errorf("kind: want unsubscribe got %q", msg.Kind)
	}
}

func TestDecodeInbound_UnknownKindReturnsError(t *testing.T) {
	raw := []byte(`{"type":"set_fire","workspace_id":"ws-1"}`)
	_, err := DecodeInbound(raw)
	if err == nil {
		t.Fatal("unknown kind should error")
	}
	if !strings.Contains(err.Error(), "unknown") {
		t.Errorf("error should describe the unknown kind, got: %v", err)
	}
}

func TestDecodeInbound_MissingWorkspaceIDIsError(t *testing.T) {
	raw := []byte(`{"type":"subscribe","run_id":"wf-1"}`)
	_, err := DecodeInbound(raw)
	if err == nil {
		t.Fatal("missing workspace_id should error")
	}
}

func TestDecodeInbound_MalformedJSONIsError(t *testing.T) {
	_, err := DecodeInbound([]byte(`{not json`))
	if err == nil {
		t.Fatal("malformed JSON should error")
	}
}

func TestEncodeBatch_RoundTrip(t *testing.T) {
	events := []protocol.AgentEvent{
		{CommandID: "c-1", Kind: protocol.EventProgress, Outputs: map[string]any{"i": 1}},
		{CommandID: "c-1", Kind: protocol.EventProgress, Outputs: map[string]any{"i": 2}},
	}
	raw, err := EncodeBatch("wf-1", events)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	// Decode into a generic map and assert wire-level shape.
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("decode: %v\nbytes: %s", err, string(raw))
	}
	if decoded["type"] != "activity_batch" {
		t.Errorf("type: got %v", decoded["type"])
	}
	if decoded["run_id"] != "wf-1" {
		t.Errorf("run_id: got %v", decoded["run_id"])
	}
	evs, ok := decoded["events"].([]any)
	if !ok {
		t.Fatalf("events: want []any got %T", decoded["events"])
	}
	if len(evs) != 2 {
		t.Errorf("events len: want 2 got %d", len(evs))
	}
}

func TestEncodeBatch_EmptyEventsStillWritesEnvelope(t *testing.T) {
	// Caller is responsible for skipping empty batches (Batcher already
	// does), but encoding an empty list should not error — keeps the
	// primitive simple.
	raw, err := EncodeBatch("wf-1", nil)
	if err != nil {
		t.Fatalf("encode nil: %v", err)
	}
	if !strings.Contains(string(raw), `"events":[]`) && !strings.Contains(string(raw), `"events":null`) {
		t.Errorf("expected an events field in the envelope, got %s", string(raw))
	}
}
