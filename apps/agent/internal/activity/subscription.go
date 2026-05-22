// Package activity holds the agent-side primitives for the
// demand-pull WebSocket activity-batching path. The SubscriptionSet
// mirrors the backend's `SubscriberRegistry`: backend → agent
// `subscribe`/`unsubscribe` messages drive Add/Remove; the Batcher
// reads Contains to filter outbound events.
package activity

import "sync"

// SubscriptionSet is a thread-safe set of workspace IDs the agent is
// currently asked to forward activity for. The backend's subscriber
// registry drives Add/Remove on 0→1 / 1→0 SSE-subscriber transitions.
//
// Reads are hot path (every Publish into the Batcher); writes happen
// only on subscribe / unsubscribe WS messages, so RWMutex fits.
type SubscriptionSet struct {
	mu  sync.RWMutex
	set map[string]struct{}
}

func NewSubscriptionSet() *SubscriptionSet {
	return &SubscriptionSet{set: make(map[string]struct{})}
}

func (s *SubscriptionSet) Add(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.set[key] = struct{}{}
}

func (s *SubscriptionSet) Remove(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.set, key)
}

func (s *SubscriptionSet) Contains(key string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.set[key]
	return ok
}

func (s *SubscriptionSet) Size() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.set)
}
