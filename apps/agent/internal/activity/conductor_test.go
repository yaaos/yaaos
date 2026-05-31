package activity

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// recordingSender captures encoded activity_batch frames the Conductor
// emits via its outbound write callback.
type recordingSender struct {
	mu     sync.Mutex
	frames [][]byte
	fail   error // if set, Send returns this error
}

func (r *recordingSender) Send(frame []byte) error {
	if r.fail != nil {
		return r.fail
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	cp := make([]byte, len(frame))
	copy(cp, frame)
	r.frames = append(r.frames, cp)
	return nil
}

func (r *recordingSender) snapshot() [][]byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([][]byte, len(r.frames))
	copy(out, r.frames)
	return out
}

func TestConductor_HandleInbound_SubscribeUpdatesBothSetAndMapping(t *testing.T) {
	send := &recordingSender{}
	c := NewConductor(20*time.Millisecond, send.Send)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.Start(ctx)
	defer c.Stop()

	raw := []byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`)
	if err := c.HandleInbound(raw); err != nil {
		t.Fatalf("HandleInbound: %v", err)
	}
	if !c.subs.Contains("ws-1") {
		t.Error("subscribe should add to SubscriptionSet")
	}
	wf, ok := c.mapping.Get("ws-1")
	if !ok || wf != "wf-1" {
		t.Errorf("mapping after subscribe: got (%q, %v)", wf, ok)
	}
}

func TestConductor_HandleInbound_UnsubscribeRemovesBoth(t *testing.T) {
	send := &recordingSender{}
	c := NewConductor(20*time.Millisecond, send.Send)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.Start(ctx)
	defer c.Stop()

	_ = c.HandleInbound([]byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))
	_ = c.HandleInbound([]byte(`{"type":"unsubscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))

	if c.subs.Contains("ws-1") {
		t.Error("unsubscribe should remove from SubscriptionSet")
	}
	if _, ok := c.mapping.Get("ws-1"); ok {
		t.Error("unsubscribe should remove from WorkspaceMapping")
	}
}

func TestConductor_HandleInbound_MalformedReturnsError(t *testing.T) {
	c := NewConductor(20*time.Millisecond, func([]byte) error { return nil })
	if err := c.HandleInbound([]byte(`{garbage`)); err == nil {
		t.Fatal("malformed JSON should error")
	}
}

func TestConductor_PublishFlushesEncodedFrame(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		send := &recordingSender{}
		c := NewConductor(20*time.Millisecond, send.Send)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		c.Start(ctx)

		_ = c.HandleInbound([]byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))
		c.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		c.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})

		// Advance fake time past several flush cycles; ticker fires deterministically.
		time.Sleep(80 * time.Millisecond)
		c.Stop()

		frames := send.snapshot()
		if len(frames) == 0 {
			t.Fatal("expected at least one outbound frame")
		}
		// Sum events across all frames — must equal what we published.
		total := 0
		for _, f := range frames {
			var env map[string]any
			if err := json.Unmarshal(f, &env); err != nil {
				t.Fatalf("decode frame: %v\nframe: %s", err, string(f))
			}
			if env["type"] != "activity_batch" {
				t.Errorf("type: got %v", env["type"])
			}
			if env["workflow_execution_id"] != "wf-1" {
				t.Errorf("workflow_execution_id: got %v", env["workflow_execution_id"])
			}
			evs, _ := env["events"].([]any)
			total += len(evs)
		}
		if total != 2 {
			t.Errorf("total events across frames: want 2 got %d", total)
		}
	})
}

func TestConductor_PublishWithoutSubscriptionEmitsNothing(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		send := &recordingSender{}
		c := NewConductor(20*time.Millisecond, send.Send)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		c.Start(ctx)
		t.Cleanup(c.Stop)

		// No subscribe — Publish should drop.
		c.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past several flush cycles; no frame should appear.
		time.Sleep(60 * time.Millisecond)
		synctest.Wait()

		if got := len(send.snapshot()); got != 0 {
			t.Errorf("unsubscribed publish should not flush, got %d frames", got)
		}
	})
}

func TestConductor_PublishWithoutMappingDropsBatch(t *testing.T) {
	// Defensive: if a workspace is in the SubscriptionSet but missing
	// from the WorkspaceMapping (shouldn't happen with the slice-79
	// payload shape, but be conservative), the Conductor drops the
	// batch rather than sending an activity_batch with an empty
	// workflow_execution_id which the backend would reject.
	synctest.Test(t, func(t *testing.T) {
		send := &recordingSender{}
		c := NewConductor(20*time.Millisecond, send.Send)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		c.Start(ctx)
		t.Cleanup(c.Stop)

		// Subscribe via the SubscriptionSet directly to simulate the
		// missing-mapping case.
		c.subs.Add("ws-1")
		c.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past several flush cycles; batch should be dropped.
		time.Sleep(60 * time.Millisecond)
		synctest.Wait()

		if got := len(send.snapshot()); got != 0 {
			t.Errorf("missing mapping should drop batch, got %d frames", got)
		}
	})
}

func TestConductor_SendErrorDoesNotPanic(t *testing.T) {
	// A flaky WS write must not crash the flush loop — subsequent
	// publishes should keep working once the underlying transport
	// recovers (caller's responsibility).
	synctest.Test(t, func(t *testing.T) {
		send := &recordingSender{fail: errFakeWS}
		c := NewConductor(15*time.Millisecond, send.Send)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		c.Start(ctx)
		t.Cleanup(c.Stop)

		_ = c.HandleInbound([]byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))
		c.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past a flush cycle to let the attempt + log happen.
		time.Sleep(50 * time.Millisecond)
		synctest.Wait()

		// No assertion beyond not-crashed.
	})
}

var errFakeWS = &fakeError{"fake ws write fail"}

type fakeError struct{ s string }

func (e *fakeError) Error() string { return e.s }
