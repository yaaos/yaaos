package supervisor

import (
	"testing"
	"time"
)

const stsEnv = "YAAOS_AGENT_STS_BACKOFF_SECONDS"

func TestParseStsBackoffSeconds_Default(t *testing.T) {
	// Empty input is handled by parseStsBackoffEnv (env == "" short-circuits),
	// not by parseBackoffSeconds. We test the happy and error paths of the
	// shared parse helper here.
	steps, err := parseBackoffSeconds(stsEnv, "60,180,300,900,3600")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []time.Duration{
		60 * time.Second,
		180 * time.Second,
		300 * time.Second,
		900 * time.Second,
		3600 * time.Second,
	}
	if len(steps) != len(want) {
		t.Fatalf("len: want %d, got %d", len(want), len(steps))
	}
	for i, w := range want {
		if steps[i] != w {
			t.Errorf("step[%d]: want %s, got %s", i, w, steps[i])
		}
	}
}

func TestParseStsBackoffSeconds_CustomValid(t *testing.T) {
	steps, err := parseBackoffSeconds(stsEnv, "2,2,2,2,2")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
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

func TestParseStsBackoffSeconds_MalformedNonInteger(t *testing.T) {
	_, err := parseBackoffSeconds(stsEnv, "1,two,3")
	if err == nil {
		t.Fatal("expected error for non-integer token, got nil")
	}
}

func TestParseStsBackoffSeconds_MalformedNonPositive(t *testing.T) {
	_, err := parseBackoffSeconds(stsEnv, "1,0,3")
	if err == nil {
		t.Fatal("expected error for non-positive value, got nil")
	}
}

func TestParseStsBackoffSeconds_MalformedNegative(t *testing.T) {
	_, err := parseBackoffSeconds(stsEnv, "1,-5,3")
	if err == nil {
		t.Fatal("expected error for negative value, got nil")
	}
}

func TestParseStsBackoffEnv_DefaultOnUnset(t *testing.T) {
	// Env unset → parseStsBackoffEnv returns a schedule based on the prod ramp.
	// We verify Peek() returns the first step of the prod ramp (1 min ± 20%).
	t.Setenv("YAAOS_AGENT_STS_BACKOFF_SECONDS", "")
	sched := parseStsBackoffEnv()
	if sched == nil {
		t.Fatal("expected non-nil schedule")
	}
	d := sched.Peek()
	lo := time.Duration(float64(1*time.Minute) * 0.80)
	hi := time.Duration(float64(1*time.Minute) * 1.20)
	if d < lo || d > hi {
		t.Errorf("Peek() = %s; want in [%s, %s] (1m ±20%%)", d, lo, hi)
	}
}

func TestParseStsBackoffEnv_CustomOnValid(t *testing.T) {
	// Valid env → schedule uses the custom steps; Peek() ≈ 2s.
	t.Setenv("YAAOS_AGENT_STS_BACKOFF_SECONDS", "2,2,2,2,2")
	sched := parseStsBackoffEnv()
	if sched == nil {
		t.Fatal("expected non-nil schedule")
	}
	d := sched.Peek()
	lo := time.Duration(float64(2*time.Second) * 0.80)
	hi := time.Duration(float64(2*time.Second) * 1.20)
	if d < lo || d > hi {
		t.Errorf("Peek() = %s; want in [%s, %s] (2s ±20%%)", d, lo, hi)
	}
}

func TestParseStsBackoffEnv_DefaultOnMalformed(t *testing.T) {
	// Malformed env → WARN + fall back to prod ramp; Peek() ≈ 1m.
	t.Setenv("YAAOS_AGENT_STS_BACKOFF_SECONDS", "not,valid")
	sched := parseStsBackoffEnv()
	if sched == nil {
		t.Fatal("expected non-nil schedule")
	}
	d := sched.Peek()
	lo := time.Duration(float64(1*time.Minute) * 0.80)
	hi := time.Duration(float64(1*time.Minute) * 1.20)
	if d < lo || d > hi {
		t.Errorf("Peek() = %s; want in [%s, %s] (1m ±20%% — fallback ramp)", d, lo, hi)
	}
}
