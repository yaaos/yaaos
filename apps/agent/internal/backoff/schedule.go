// Package backoff implements the agent's connection-failure retry
// schedule. The ramp is 1m / 3m / 5m / 15m / 60m-forever with ±20%
// jitter, applied identically to auth (401/403) and network (5xx,
// connection refused) failures — operators distinguish via the local
// log file, not via different cadences.
//
// One Schedule instance per surface (claim, heartbeat, ws dial, STS
// bootstrap). A misconfigured ARN on the bootstrap surface shouldn't
// slow heartbeat retries on an unrelated transient blip; the
// per-surface ownership keeps backoffs independent.
//
// Reset() returns the counter to zero — call after the first success.
// Sleep() advances the counter then blocks for the next jittered delay,
// returning early on ctx cancel.
package backoff

import (
	"context"
	"math/rand/v2" // nosemgrep: go.lang.security.audit.crypto.math_random.math-random-used -- jitter is timing-only; crypto/rand would just burn entropy without changing any threat model.
	"sync"
	"time"
)

// jitterPercent is the ±band applied to each step. 20% means each
// scheduled delay falls in [0.8 × base, 1.2 × base]. Prevents thundering
// herd when N agents reconnect simultaneously after a backend recovery.
const jitterPercent = 20

var defaultSteps = []time.Duration{
	1 * time.Minute,
	3 * time.Minute,
	5 * time.Minute,
	15 * time.Minute,
	60 * time.Minute,
}

// Schedule is one surface's per-failure backoff counter. Safe for
// concurrent use.
type Schedule struct {
	mu      sync.Mutex
	steps   []time.Duration
	attempt int
	rng     func() float64 // returns 0..1; defaults to rand.Float64
}

// New returns a Schedule pre-populated with the default 1m/3m/5m/15m/60m
// ramp.
func New() *Schedule {
	return &Schedule{steps: defaultSteps, rng: rand.Float64}
}

// NewWithSteps returns a Schedule with a caller-supplied step list.
// The last step pins forever (same as the default ramp). Used when the
// default 1m-60m ramp is too coarse — e.g. event-post retry where the
// target is a transient HTTP blip, not a multi-minute outage.
func NewWithSteps(steps []time.Duration) *Schedule {
	if len(steps) == 0 {
		steps = defaultSteps
	}
	s := make([]time.Duration, len(steps))
	copy(s, steps)
	return &Schedule{steps: s, rng: rand.Float64}
}

// Peek returns the next scheduled delay (jittered) WITHOUT advancing
// the counter. Use to drive the `connection.backoff_seconds` gauge.
func (s *Schedule) Peek() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.windowedLocked(s.attempt)
}

// Sleep blocks for the next jittered delay AND advances the counter
// for the next call. Returns ctx.Err() if cancelled before the delay
// elapses.
func (s *Schedule) Sleep(ctx context.Context) error {
	s.mu.Lock()
	d := s.windowedLocked(s.attempt)
	if s.attempt < len(s.steps)-1 {
		s.attempt++
	}
	s.mu.Unlock()

	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Reset returns the counter to zero. Call on the first successful
// operation after one or more failures.
func (s *Schedule) Reset() {
	s.mu.Lock()
	s.attempt = 0
	s.mu.Unlock()
}

// windowedLocked is the internal helper that returns step[i] with
// ±jitterPercent applied. Caller must hold s.mu.
func (s *Schedule) windowedLocked(i int) time.Duration {
	if i >= len(s.steps) {
		i = len(s.steps) - 1
	}
	base := s.steps[i]
	// jitter ∈ [-jitterPercent/100, +jitterPercent/100]
	j := (s.rng()*2 - 1) * (float64(jitterPercent) / 100)
	return time.Duration(float64(base) * (1 + j))
}

// ── test hooks ───────────────────────────────────────────────────────

// newWithRNG returns a Schedule with a caller-supplied rng. Tests use
// a deterministic source to assert exact step values.
func newWithRNG(rng func() float64) *Schedule {
	return &Schedule{steps: defaultSteps, rng: rng}
}

// advance bumps the internal counter without sleeping — only for tests
// that want to walk the steps without burning wall-clock.
func (s *Schedule) advance() {
	s.mu.Lock()
	if s.attempt < len(s.steps)-1 {
		s.attempt++
	}
	s.mu.Unlock()
}

// windowedFor returns the jittered window for an arbitrary attempt
// number — used by the jitter-band test to sweep all steps without
// mutating internal state.
func (s *Schedule) windowedFor(i int) time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.windowedLocked(i)
}
