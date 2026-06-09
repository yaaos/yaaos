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
	const env = "YAAOS_AGENT_STS_BACKOFF_SECONDS"
	v := os.Getenv(env)
	if v == "" {
		return backoff.NewWithDeadline(stsBackoffDeadline)
	}
	steps, err := parseBackoffSeconds(env, v)
	if err != nil {
		slog.Warn("supervisor.sts_backoff_parse_failed",
			"env", env,
			"value", v,
			"err", err.Error(),
			"fallback", "prod ramp (1m/3m/5m/15m/60m)",
		)
		return backoff.NewWithDeadline(stsBackoffDeadline)
	}
	return backoff.NewWithStepsAndDeadline(steps, stsBackoffDeadline)
}

// parseBackoffSeconds parses a comma-separated list of positive integers
// (seconds) into a []time.Duration, shared by the STS and ops backoff surfaces.
// Returns an error on any non-integer token or non-positive value; envName is
// carried in the error so the failing surface is identifiable. The empty-token
// case (e.g. "2,,3") surfaces as a non-integer error on the empty token —
// strings.Split always yields at least one element, so there is no separate
// empty-input branch.
func parseBackoffSeconds(envName, s string) ([]time.Duration, error) {
	parts := strings.Split(s, ",")
	steps := make([]time.Duration, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		n, err := strconv.Atoi(p)
		if err != nil {
			return nil, &parseError{env: envName, input: s, reason: "non-integer token: " + p}
		}
		if n <= 0 {
			return nil, &parseError{env: envName, input: s, reason: "non-positive value: " + p}
		}
		steps = append(steps, time.Duration(n)*time.Second)
	}
	return steps, nil
}

type parseError struct {
	env    string
	input  string
	reason string
}

func (e *parseError) Error() string {
	return e.env + ": " + e.reason + " (input: " + e.input + ")"
}
