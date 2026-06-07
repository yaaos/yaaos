package supervisor

import (
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/yaaos/agent/internal/backoff"
)

// stsBackoffDeadline is the 1h ceiling applied to every STS backoff schedule,
// whether default or env-overridden. The deadline causes the supervisor to exit
// after 1h of continuous identity-exchange failure so the container orchestrator
// can restart it (a misconfigured ARN won't fix itself by retrying forever).
const stsBackoffDeadline = 1 * time.Hour

// parseStsBackoffEnv reads YAAOS_AGENT_STS_BACKOFF_SECONDS and returns a
// backoff.Schedule wired for STS identity-exchange retries.
//
//   - Unset → the prod ramp (1m/3m/5m/15m/60m) with the 1h deadline.
//   - Valid comma-separated positive integers → custom steps with the 1h deadline.
//   - Malformed or any non-positive value → WARN + fall back to the prod ramp.
func parseStsBackoffEnv() *backoff.Schedule {
	v := os.Getenv("YAAOS_AGENT_STS_BACKOFF_SECONDS")
	if v == "" {
		return backoff.NewWithDeadline(stsBackoffDeadline)
	}
	steps, err := parseStsBackoffSeconds(v)
	if err != nil {
		slog.Warn("supervisor.sts_backoff_parse_failed",
			"env", "YAAOS_AGENT_STS_BACKOFF_SECONDS",
			"value", v,
			"err", err.Error(),
			"fallback", "prod ramp (1m/3m/5m/15m/60m)",
		)
		return backoff.NewWithDeadline(stsBackoffDeadline)
	}
	return backoff.NewWithStepsAndDeadline(steps, stsBackoffDeadline)
}

// parseStsBackoffSeconds parses a comma-separated list of positive integers
// (seconds) into a []time.Duration. Returns an error on any empty token,
// non-integer token, or non-positive value.
func parseStsBackoffSeconds(s string) ([]time.Duration, error) {
	parts := strings.Split(s, ",")
	if len(parts) == 0 {
		return nil, &parseError{input: s, reason: "empty input"}
	}
	steps := make([]time.Duration, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		n, err := strconv.Atoi(p)
		if err != nil {
			return nil, &parseError{input: s, reason: "non-integer token: " + p}
		}
		if n <= 0 {
			return nil, &parseError{input: s, reason: "non-positive value: " + p}
		}
		steps = append(steps, time.Duration(n)*time.Second)
	}
	return steps, nil
}

type parseError struct {
	input  string
	reason string
}

func (e *parseError) Error() string {
	return "YAAOS_AGENT_STS_BACKOFF_SECONDS: " + e.reason + " (input: " + e.input + ")"
}
