// Tests for the bounded dedup LRU cache.
package supervisor

import (
	"fmt"
	"sync"
	"testing"

	"github.com/yaaos/agent/internal/protocol"
)

// TestDedupCache_StoreAndReplay verifies that a stored event can be retrieved
// by its command_id.
func TestDedupCache_StoreAndReplay(t *testing.T) {
	c := newDedupCache(8)
	ev := protocol.AgentEvent{
		CommandID: "cmd-1",
		Kind:      protocol.EventCompletedSuccess,
	}
	c.store("cmd-1", ev)

	got, ok := c.lookup("cmd-1")
	if !ok {
		t.Fatal("lookup: want hit, got miss")
	}
	if got.CommandID != "cmd-1" || got.Kind != protocol.EventCompletedSuccess {
		t.Errorf("lookup: got %+v", got)
	}
}

// TestDedupCache_Miss verifies that an unknown command_id returns false.
func TestDedupCache_Miss(t *testing.T) {
	c := newDedupCache(8)
	_, ok := c.lookup("unknown")
	if ok {
		t.Fatal("lookup: want miss, got hit")
	}
}

// TestDedupCache_CapEviction verifies that inserting cap+1 entries evicts the
// oldest entry (LRU order: least-recently inserted is evicted first when no
// lookups reorder).
func TestDedupCache_CapEviction(t *testing.T) {
	const cap = 4
	c := newDedupCache(cap)

	// Fill to cap.
	for i := 0; i < cap; i++ {
		id := fmt.Sprintf("cmd-%d", i)
		c.store(id, protocol.AgentEvent{CommandID: id, Kind: protocol.EventCompletedSuccess})
	}

	// All entries present.
	for i := 0; i < cap; i++ {
		id := fmt.Sprintf("cmd-%d", i)
		if _, ok := c.lookup(id); !ok {
			t.Errorf("before eviction: expected hit for %s", id)
		}
	}

	// Insert one more — cmd-0 (oldest, least-recently used) should be evicted.
	c.store("cmd-new", protocol.AgentEvent{CommandID: "cmd-new", Kind: protocol.EventCompletedSuccess})

	if _, ok := c.lookup("cmd-0"); ok {
		t.Error("after eviction: cmd-0 should have been evicted but is still present")
	}
	if _, ok := c.lookup("cmd-new"); !ok {
		t.Error("after eviction: cmd-new should be present")
	}
	// cmd-1 through cmd-3 must still be present.
	for i := 1; i < cap; i++ {
		id := fmt.Sprintf("cmd-%d", i)
		if _, ok := c.lookup(id); !ok {
			t.Errorf("after eviction: expected hit for %s", id)
		}
	}
}

// TestDedupCache_1024Cap verifies the production capacity constant and that
// inserting cap+1 entries evicts exactly one (the LRU).
func TestDedupCache_1024Cap(t *testing.T) {
	c := newDedupCache(dedupCacheSize)

	// Fill to cap without any lookups so insertion order = LRU order.
	for i := 0; i < dedupCacheSize; i++ {
		id := fmt.Sprintf("cmd-%d", i)
		c.store(id, protocol.AgentEvent{CommandID: id, Kind: protocol.EventCompletedSuccess})
	}

	// Insert one more — cmd-0 (oldest, LRU) should be evicted.
	c.store("cmd-extra", protocol.AgentEvent{CommandID: "cmd-extra"})

	if _, ok := c.lookup("cmd-0"); ok {
		t.Error("cmd-0 should have been evicted (LRU) after cap+1 insertions")
	}
	if _, ok := c.lookup("cmd-extra"); !ok {
		t.Error("cmd-extra should be present after insertion")
	}
	// The second-oldest entry should still be present.
	if _, ok := c.lookup("cmd-1"); !ok {
		t.Error("cmd-1 should be present (only cmd-0 was evicted)")
	}
}

// TestDedupCache_ConcurrentStoreAndLookup proves the mu-guarded LRU stays
// consistent under concurrent store and lookup calls. Multiple goroutines
// store distinct command IDs while others simultaneously look up the same IDs.
// Post-condition: every stored entry is either retrievable (if still in the
// cache) or has been evicted by a later store — the cache must not panic,
// corrupt internal state, or produce a hit for an ID that was never stored.
// Run with -race to exercise the mu guard under contention.
func TestDedupCache_ConcurrentStoreAndLookup(t *testing.T) {
	const cap = 32
	const writers = 4
	const writesPerWriter = 64
	c := newDedupCache(cap)

	var wg sync.WaitGroup

	// Writers: each stores a disjoint set of command IDs.
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < writesPerWriter; i++ {
				id := fmt.Sprintf("w%d-cmd-%d", w, i)
				c.store(id, protocol.AgentEvent{
					CommandID: id,
					Kind:      protocol.EventCompletedSuccess,
				})
			}
		}(w)
	}

	// Readers: concurrently look up IDs that may or may not exist yet.
	// We only assert no panic and no cross-contamination (a hit must carry
	// the correct CommandID).
	for r := 0; r < writers; r++ {
		wg.Add(1)
		go func(r int) {
			defer wg.Done()
			for i := 0; i < writesPerWriter; i++ {
				id := fmt.Sprintf("w%d-cmd-%d", r, i)
				ev, ok := c.lookup(id)
				if ok && ev.CommandID != id {
					// A hit with the wrong payload is a corruption.
					t.Errorf("lookup %q: got CommandID %q (cross-contamination)", id, ev.CommandID)
				}
			}
		}(r)
	}

	wg.Wait()

	// Sanity: the cache did not lose structural integrity — it should still
	// accept new stores and return coherent hits.
	c.store("sentinel", protocol.AgentEvent{CommandID: "sentinel", Kind: protocol.EventCompletedFailure})
	ev, ok := c.lookup("sentinel")
	if !ok {
		t.Error("post-concurrent: sentinel store+lookup miss (LRU corrupted?)")
	}
	if ok && ev.CommandID != "sentinel" {
		t.Errorf("post-concurrent: sentinel got %q", ev.CommandID)
	}
}

// TestDedupCache_LookupPromotesToMRU verifies that a lookup moves an entry to
// most-recently-used, protecting it from the next eviction.
func TestDedupCache_LookupPromotesToMRU(t *testing.T) {
	const cap = 3
	c := newDedupCache(cap)

	for _, id := range []string{"a", "b", "c"} {
		c.store(id, protocol.AgentEvent{CommandID: id})
	}
	// Access "a" — now "b" is LRU.
	_, _ = c.lookup("a")

	// Insert "d" — "b" (LRU) should be evicted, not "a".
	c.store("d", protocol.AgentEvent{CommandID: "d"})

	if _, ok := c.lookup("b"); ok {
		t.Error("b should have been evicted (LRU after a was accessed)")
	}
	if _, ok := c.lookup("a"); !ok {
		t.Error("a should still be present (promoted by lookup)")
	}
}
