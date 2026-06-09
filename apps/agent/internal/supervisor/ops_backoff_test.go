package supervisor

import (
	"testing"
	"time"
)

func TestOpsBackoffSteps_UnsetEnv(t *testing.T) {
	t.Setenv(opsBackoffEnv, "")
	steps, custom := opsBackoffSteps()
	if custom {
		t.Errorf("unset env: want custom=false, got true (steps=%v)", steps)
	}
}

func TestOpsBackoffSteps_CustomValid(t *testing.T) {
	t.Setenv(opsBackoffEnv, "2,2,2,2,2")
	steps, custom := opsBackoffSteps()
	if !custom {
		t.Fatal("valid env: want custom=true, got false")
	}
	if len(steps) != 5 {
		t.Fatalf("len: want 5, got %d", len(steps))
	}
	for i, s := range steps {
		if s != 2*time.Second {
			t.Errorf("step[%d]: want 2s, got %s", i, s)
		}
	}
}

func TestOpsBackoffSteps_MalformedFallsBack(t *testing.T) {
	cases := []struct {
		name string
		env  string
	}{
		{"non_integer", "1,two,3"},
		{"non_positive", "1,0,3"},
		{"negative", "1,-5,3"},
		{"empty_token", "2,,3"}, // strings.Split yields "" → non-integer error
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(opsBackoffEnv, tc.env)
			_, custom := opsBackoffSteps()
			if custom {
				t.Errorf("malformed env %q: want custom=false (prod-ramp fallback), got true", tc.env)
			}
		})
	}
}

func TestNewOpsBackoff_DefaultRamp(t *testing.T) {
	// custom=false → prod ramp; Peek() ≈ 1m ±20%.
	sched := newOpsBackoff(nil, false)
	if sched == nil {
		t.Fatal("expected non-nil schedule")
	}
	assertPeekWithin(t, sched.Peek(), 1*time.Minute)
}

func TestNewOpsBackoff_CustomSteps(t *testing.T) {
	// custom=true → custom steps; Peek() ≈ 2s ±20%.
	sched := newOpsBackoff([]time.Duration{2 * time.Second}, true)
	if sched == nil {
		t.Fatal("expected non-nil schedule")
	}
	assertPeekWithin(t, sched.Peek(), 2*time.Second)
}

// assertPeekWithin checks d is within ±20% of want (the schedule's jitter band).
func assertPeekWithin(t *testing.T, d, want time.Duration) {
	t.Helper()
	lo := time.Duration(float64(want) * 0.80)
	hi := time.Duration(float64(want) * 1.20)
	if d < lo || d > hi {
		t.Errorf("Peek() = %s; want in [%s, %s] (%s ±20%%)", d, lo, hi, want)
	}
}
