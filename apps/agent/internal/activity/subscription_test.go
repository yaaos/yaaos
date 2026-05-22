package activity

import (
	"strconv"
	"sync"
	"testing"
)

func TestSubscriptionSet_AddContainsRemove(t *testing.T) {
	s := NewSubscriptionSet()
	if s.Contains("ws-1") {
		t.Fatal("empty set should not contain ws-1")
	}
	s.Add("ws-1")
	if !s.Contains("ws-1") {
		t.Fatal("Add then Contains should be true")
	}
	s.Remove("ws-1")
	if s.Contains("ws-1") {
		t.Fatal("Remove then Contains should be false")
	}
}

func TestSubscriptionSet_DoubleAddIdempotent(t *testing.T) {
	s := NewSubscriptionSet()
	s.Add("ws-1")
	s.Add("ws-1")
	if !s.Contains("ws-1") {
		t.Fatal("double Add should still Contain")
	}
	if got := s.Size(); got != 1 {
		t.Fatalf("Size after double Add: want 1 got %d", got)
	}
}

func TestSubscriptionSet_ConcurrentAccess(t *testing.T) {
	// Hammer 100 goroutines doing Add/Remove/Contains on disjoint keys.
	// Race-detector enabled in `go test -race` will flag any unlocked
	// shared-state writes.
	s := NewSubscriptionSet()
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := keyFor(i)
			s.Add(key)
			_ = s.Contains(key)
			s.Remove(key)
		}(i)
	}
	wg.Wait()
	if got := s.Size(); got != 0 {
		t.Fatalf("Size after 100 add/remove cycles: want 0 got %d", got)
	}
}

func keyFor(i int) string {
	// Deterministic per-goroutine key so concurrent runs don't collide.
	return "ws-" + strconv.Itoa(i)
}
