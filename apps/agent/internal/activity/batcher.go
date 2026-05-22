package activity

import (
	"context"
	"sync"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// FlushFunc receives one batch (per subscription key) per flush cycle.
// Implementations encode the batch as `activity_batch` and write it to
// the supervisor's WebSocket connection. A nil flush is treated as a
// no-op so the Batcher is safe to construct in tests that don't care
// about the wire side.
type FlushFunc func(key string, events []protocol.AgentEvent)

// Batcher buffers AgentEvents per subscription key and flushes one
// batch per key on each interval tick. Events whose key isn't in the
// SubscriptionSet are dropped at Publish time (cheap, no buffering).
//
// Lifecycle: NewBatcher → Start(ctx) → Publish(...)* → Stop(). Stop
// drains the buffer synchronously so no events are lost on shutdown.
// Cancelling ctx is equivalent to Stop.
type Batcher struct {
	sub      *SubscriptionSet
	interval time.Duration
	flush    FlushFunc

	mu     sync.Mutex
	buffer map[string][]protocol.AgentEvent // key → buffered events

	stopOnce sync.Once
	done     chan struct{}
	stopped  chan struct{}
}

func NewBatcher(sub *SubscriptionSet, interval time.Duration, flush FlushFunc) *Batcher {
	return &Batcher{
		sub:      sub,
		interval: interval,
		flush:    flush,
		buffer:   make(map[string][]protocol.AgentEvent),
		done:     make(chan struct{}),
		stopped:  make(chan struct{}),
	}
}

// Start launches the periodic flush goroutine. Idempotent — calling
// twice panics by design (catches misuse early).
func (b *Batcher) Start(ctx context.Context) {
	go b.run(ctx)
}

func (b *Batcher) run(ctx context.Context) {
	defer close(b.stopped)
	t := time.NewTicker(b.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			b.drain()
			return
		case <-b.done:
			b.drain()
			return
		case <-t.C:
			b.drain()
		}
	}
}

// Publish adds an event to the buffer if the key is subscribed.
// Unsubscribed keys drop on the floor — saves memory for activity the
// UI won't consume.
func (b *Batcher) Publish(key string, ev protocol.AgentEvent) {
	if !b.sub.Contains(key) {
		return
	}
	b.mu.Lock()
	b.buffer[key] = append(b.buffer[key], ev)
	b.mu.Unlock()
}

// drain swaps the buffer out under the lock, then calls the flush
// callback per key outside the lock. Empty buffers are silent — no
// useless WS frames on idle ticks.
func (b *Batcher) drain() {
	b.mu.Lock()
	if len(b.buffer) == 0 {
		b.mu.Unlock()
		return
	}
	pending := b.buffer
	b.buffer = make(map[string][]protocol.AgentEvent)
	b.mu.Unlock()

	if b.flush == nil {
		return
	}
	for key, events := range pending {
		if len(events) == 0 {
			continue
		}
		b.flush(key, events)
	}
}

// Stop signals the run loop to exit and waits for it to finish
// draining. Safe to call multiple times — only the first signals.
func (b *Batcher) Stop() {
	b.stopOnce.Do(func() {
		close(b.done)
	})
	<-b.stopped
}
