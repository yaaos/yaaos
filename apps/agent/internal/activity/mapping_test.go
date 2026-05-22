package activity

import (
	"strconv"
	"sync"
	"testing"
)

func TestWorkspaceMapping_SetGetRemove(t *testing.T) {
	m := NewWorkspaceMapping()
	if _, ok := m.Get("ws-1"); ok {
		t.Fatal("empty mapping should not have ws-1")
	}
	m.Set("ws-1", "wf-1")
	got, ok := m.Get("ws-1")
	if !ok {
		t.Fatal("Set then Get should return ok=true")
	}
	if got != "wf-1" {
		t.Errorf("got %q want wf-1", got)
	}
	m.Remove("ws-1")
	if _, ok := m.Get("ws-1"); ok {
		t.Fatal("after Remove, Get should return ok=false")
	}
}

func TestWorkspaceMapping_OverwriteOnDuplicateSet(t *testing.T) {
	// Two subscribes for the same workspace (shouldn't happen given the
	// backend's 0→1 transition gate, but be defensive) overwrite the
	// mapping. Last writer wins.
	m := NewWorkspaceMapping()
	m.Set("ws-1", "wf-1")
	m.Set("ws-1", "wf-2")
	got, _ := m.Get("ws-1")
	if got != "wf-2" {
		t.Errorf("overwrite: got %q want wf-2", got)
	}
}

func TestWorkspaceMapping_ConcurrentAccess(t *testing.T) {
	// Race detector enforces lock discipline.
	m := NewWorkspaceMapping()
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := "ws-" + strconv.Itoa(i)
			m.Set(key, "wf-"+strconv.Itoa(i))
			_, _ = m.Get(key)
			m.Remove(key)
		}(i)
	}
	wg.Wait()
	if got := m.Size(); got != 0 {
		t.Errorf("Size after 100 cycles: want 0 got %d", got)
	}
}
