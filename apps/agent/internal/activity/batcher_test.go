package activity

import (
	"context"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// recordingFlush captures each batch the Batcher emits so tests can
// assert ordering and grouping.
type recordingFlush struct {
	mu      sync.Mutex
	batches []flushedBatch
}

type flushedBatch struct {
	key    string
	events []protocol.AgentEvent
}

func (r *recordingFlush) fn(key string, events []protocol.AgentEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	// Copy slice so subsequent buffer reuse can't mutate captured state.
	evs := make([]protocol.AgentEvent, len(events))
	copy(evs, events)
	r.batches = append(r.batches, flushedBatch{key: key, events: evs})
}

func (r *recordingFlush) snapshot() []flushedBatch {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]flushedBatch, len(r.batches))
	copy(out, r.batches)
	return out
}

func TestBatcher_PublishWithoutSubscriptionIsDropped(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sub := NewSubscriptionSet()
		rec := &recordingFlush{}
		b := NewBatcher(sub, 20*time.Millisecond, rec.fn)

		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		b.Start(ctx)

		// No subscription for ws-1 → Publish should be a no-op.
		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past several ticks; timer fires but no key is subscribed.
		time.Sleep(50 * time.Millisecond)
		synctest.Wait()

		if len(rec.snapshot()) != 0 {
			t.Fatalf("unsubscribed key should not flush, got %d batches", len(rec.snapshot()))
		}
		b.Stop()
	})
}

func TestBatcher_FlushesSubscribedEventsAtInterval(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sub := NewSubscriptionSet()
		sub.Add("ws-1")
		rec := &recordingFlush{}
		b := NewBatcher(sub, 30*time.Millisecond, rec.fn)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		b.Start(ctx)

		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})

		// Advance fake time past one flush cycle; synctest fires the ticker deterministically.
		time.Sleep(100 * time.Millisecond)
		b.Stop()

		batches := rec.snapshot()
		if len(batches) == 0 {
			t.Fatal("expected at least one batch, got 0")
		}
		// Sum events across all flushes for ws-1 — must equal what we published.
		total := 0
		for _, batch := range batches {
			if batch.key != "ws-1" {
				t.Errorf("unexpected key %q", batch.key)
			}
			total += len(batch.events)
		}
		if total != 2 {
			t.Errorf("total events across batches: want 2 got %d", total)
		}
	})
}

func TestBatcher_GroupsEventsPerKey(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sub := NewSubscriptionSet()
		sub.Add("ws-1")
		sub.Add("ws-2")
		rec := &recordingFlush{}
		b := NewBatcher(sub, 30*time.Millisecond, rec.fn)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		b.Start(ctx)

		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-a", Kind: protocol.EventProgress})
		b.Publish("ws-2", protocol.AgentEvent{CommandID: "c-b", Kind: protocol.EventProgress})
		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-a", Kind: protocol.EventProgress})

		// Advance fake time past one flush cycle; both keys flush in the same tick.
		time.Sleep(80 * time.Millisecond)
		b.Stop()

		// ws-1 + ws-2 must each appear in batches; ws-1 must carry 2 events total,
		// ws-2 must carry 1. They are independent — never combined in a single batch.
		totalByKey := map[string]int{}
		for _, batch := range rec.snapshot() {
			for _, ev := range batch.events {
				if ev.CommandID == "c-a" && batch.key != "ws-1" {
					t.Errorf("c-a event flushed under wrong key %q", batch.key)
				}
				if ev.CommandID == "c-b" && batch.key != "ws-2" {
					t.Errorf("c-b event flushed under wrong key %q", batch.key)
				}
			}
			totalByKey[batch.key] += len(batch.events)
		}
		if totalByKey["ws-1"] != 2 {
			t.Errorf("ws-1 total: want 2 got %d", totalByKey["ws-1"])
		}
		if totalByKey["ws-2"] != 1 {
			t.Errorf("ws-2 total: want 1 got %d", totalByKey["ws-2"])
		}
	})
}

func TestBatcher_StopDrainsRemainingBuffer(t *testing.T) {
	// Publish events then Stop immediately. Stop must flush any buffered
	// events synchronously so nothing is lost on shutdown.
	sub := NewSubscriptionSet()
	sub.Add("ws-1")
	rec := &recordingFlush{}
	b := NewBatcher(sub, 10*time.Second, rec.fn) // very long interval so timer doesn't fire
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Start(ctx)

	b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
	b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
	b.Stop()

	batches := rec.snapshot()
	total := 0
	for _, batch := range batches {
		total += len(batch.events)
	}
	if total != 2 {
		t.Errorf("Stop should drain buffered events, got %d total", total)
	}
}

func TestBatcher_EmptyTickDoesNotInvokeFlush(t *testing.T) {
	// If no events were published in an interval, the flush callback must
	// not be invoked — saves a useless WS round-trip per tick.
	synctest.Test(t, func(t *testing.T) {
		sub := NewSubscriptionSet()
		sub.Add("ws-1")
		rec := &recordingFlush{}
		b := NewBatcher(sub, 15*time.Millisecond, rec.fn)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		b.Start(ctx)

		// Advance fake time through several ticks with no publishes.
		time.Sleep(70 * time.Millisecond)
		b.Stop()

		if got := len(rec.snapshot()); got != 0 {
			t.Errorf("empty ticks should not flush, got %d batches", got)
		}
	})
}

func TestBatcher_UnsubscribeMidFlightStopsFutureBatches(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sub := NewSubscriptionSet()
		sub.Add("ws-1")
		rec := &recordingFlush{}
		b := NewBatcher(sub, 20*time.Millisecond, rec.fn)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		b.Start(ctx)

		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past first flush cycle.
		time.Sleep(50 * time.Millisecond)
		synctest.Wait()
		sub.Remove("ws-1")
		// After Remove, further publishes drop on the floor.
		b.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})
		// Advance fake time past another flush cycle; no second batch should appear.
		time.Sleep(50 * time.Millisecond)
		b.Stop()

		total := 0
		for _, batch := range rec.snapshot() {
			total += len(batch.events)
		}
		if total != 1 {
			t.Errorf("after Remove, only the pre-Remove event should flush; got %d total", total)
		}
	})
}
