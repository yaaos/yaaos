// Package supervisor — dedup cache for terminal AgentEvents.
//
// A bounded LRU prevents re-execution when the control plane re-delivers a
// command_id that has already reached a terminal outcome. The cache is
// in-memory only; a pod restart loses it (at-least-once + no persistent
// command log is an accepted trade-off).
package supervisor

import (
	"container/list"
	"sync"

	"github.com/yaaos/agent/internal/protocol"
)

// dedupCacheSize is the fixed capacity of the shared terminal-event cache.
// 1024 entries is far larger than the in-flight+retry window
// (≤4 claim-workers, terminal events ack within seconds), so an entry
// never evicts while replay-relevant under normal operation.
const dedupCacheSize = 1024

// dedupEntry is the value stored in the LRU.
type dedupEntry struct {
	commandID string
	event     protocol.AgentEvent
}

// dedupCache is a bounded LRU cache mapping command_id → terminal AgentEvent.
// All operations are safe for concurrent use.
type dedupCache struct {
	mu    sync.Mutex
	cap   int
	ll    *list.List               // front = most recently used
	index map[string]*list.Element // command_id → list element
}

// newDedupCache returns a dedupCache with the given capacity.
func newDedupCache(cap int) *dedupCache {
	if cap <= 0 {
		cap = dedupCacheSize
	}
	return &dedupCache{
		cap:   cap,
		ll:    list.New(),
		index: make(map[string]*list.Element, cap),
	}
}

// store inserts or updates the cached terminal event for commandID.
// When the cache is full, the least-recently-used entry is evicted first.
func (c *dedupCache) store(commandID string, ev protocol.AgentEvent) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if el, ok := c.index[commandID]; ok {
		// Already present — update in place and move to front (MRU).
		el.Value.(*dedupEntry).event = ev
		c.ll.MoveToFront(el)
		return
	}

	// Evict LRU if at capacity.
	if c.ll.Len() >= c.cap {
		lru := c.ll.Back()
		if lru != nil {
			c.ll.Remove(lru)
			delete(c.index, lru.Value.(*dedupEntry).commandID)
		}
	}

	entry := &dedupEntry{commandID: commandID, event: ev}
	el := c.ll.PushFront(entry)
	c.index[commandID] = el
}

// lookup retrieves the cached terminal event for commandID.
// A hit promotes the entry to most-recently-used. Returns (zero, false) on miss.
func (c *dedupCache) lookup(commandID string) (protocol.AgentEvent, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	el, ok := c.index[commandID]
	if !ok {
		return protocol.AgentEvent{}, false
	}
	c.ll.MoveToFront(el)
	return el.Value.(*dedupEntry).event, true
}
