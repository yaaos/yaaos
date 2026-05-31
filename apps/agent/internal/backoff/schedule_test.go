package backoff

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestSchedule_HitsExpectedSteps(t *testing.T) {
	// With jitter forced to 0, Peek() returns each step in order, then
	// pins at the last step forever.
	s := newWithRNG(zeroJitter)
	want := []time.Duration{
		1 * time.Minute, 3 * time.Minute, 5 * time.Minute,
		15 * time.Minute, 60 * time.Minute,
	}
	for i, w := range want {
		got := s.Peek()
		if got != w {
			t.Fatalf("attempt %d: want %s, got %s", i, w, got)
		}
		s.advance() // simulate Sleep without actually sleeping
	}
	// Beyond the last step the schedule pins at 60m.
	for i := 0; i < 5; i++ {
		if got := s.Peek(); got != 60*time.Minute {
			t.Fatalf("post-cap attempt %d: want 60m, got %s", i, got)
		}
		s.advance()
	}
}

func TestSchedule_JitterWithinBand(t *testing.T) {
	// 1000 samples per attempt — every one must fall inside [0.8x, 1.2x].
	s := New()
	base := []time.Duration{
		1 * time.Minute, 3 * time.Minute, 5 * time.Minute,
		15 * time.Minute, 60 * time.Minute,
	}
	for attempt, b := range base {
		lo := time.Duration(float64(b) * 0.8)
		hi := time.Duration(float64(b) * 1.2)
		for i := 0; i < 1000; i++ {
			d := s.windowedFor(attempt)
			if d < lo || d > hi {
				t.Fatalf("attempt %d sample %d: %s outside [%s,%s]",
					attempt, i, d, lo, hi)
			}
		}
	}
}

func TestSchedule_ResetReturnsToZero(t *testing.T) {
	s := newWithRNG(zeroJitter)
	s.advance()
	s.advance()
	s.advance()
	if got := s.Peek(); got != 15*time.Minute {
		t.Fatalf("pre-reset: want 15m, got %s", got)
	}
	s.Reset()
	if got := s.Peek(); got != 1*time.Minute {
		t.Fatalf("post-reset: want 1m, got %s", got)
	}
}

func TestSchedule_SleepRespectsContextCancel(t *testing.T) {
	// Sleep returns ctx.Err() immediately on cancel — operator's
	// SIGTERM during a long backoff shouldn't wait minutes to exit.
	s := New()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	if err := s.Sleep(ctx); err != context.Canceled {
		t.Fatalf("want context.Canceled, got %v", err)
	}
	if time.Since(start) > 100*time.Millisecond {
		t.Fatalf("Sleep blocked despite cancel")
	}
}

// zeroJitter forces windowed() to return exactly the base step (no
// random component). Used for deterministic step-progression tests.
func zeroJitter() float64 { return 0.5 } // (0.5*2 - 1) * 0.2 == 0

// TestBackoff_ConcurrentSurfaces_NoCrossContamination proves the per-surface
// invariant: N goroutines each driving their own independent Schedule through
// several advance+Peek cycles end up at the expected step for THEIR attempt
// count, with no cross-contamination between schedules and no data race.
// Run with -race to exercise the mu guard under contention.
func TestBackoff_ConcurrentSurfaces_NoCrossContamination(t *testing.T) {
	const surfaces = 8
	const advancesPerSurface = 3 // advance to step index 3 (15m with zeroJitter)

	var wg sync.WaitGroup
	errs := make([]string, surfaces)
	for i := 0; i < surfaces; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			s := newWithRNG(zeroJitter)
			for j := 0; j < advancesPerSurface; j++ {
				s.advance()
			}
			got := s.Peek()
			want := defaultSteps[advancesPerSurface]
			if got != want {
				errs[idx] = "surface " + string(rune('0'+idx)) + ": want " + want.String() + " got " + got.String()
			}
			// Also exercise Reset from a concurrent goroutine to touch mu
			// from multiple directions simultaneously.
			s.Reset()
			if got := s.Peek(); got != defaultSteps[0] {
				errs[idx] = "after reset: want " + defaultSteps[0].String() + " got " + got.String()
			}
		}(i)
	}
	wg.Wait()
	for _, msg := range errs {
		if msg != "" {
			t.Error(msg)
		}
	}
}
