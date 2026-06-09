package supervisor

import (
	"errors"
	"testing"
)

func TestClassifyConnErr(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want string
	}{
		{name: "nil", err: nil, want: ""},
		// doJSON generic path — numeric status code in message.
		{name: "401_numeric", err: errors.New("POST /api/v1/agent/commands/claim: 401 Unauthorized"), want: "auth"},
		{name: "403_numeric", err: errors.New("POST /api/v1/agent/heartbeat: 403 Forbidden"), want: "auth"},
		// ClaimCommand returns the word form (no numeric code).
		{name: "claim_unauthorized", err: errors.New("claim: unauthorized"), want: "auth"},
		// doJSON returns ": unauthorized" suffix.
		{name: "heartbeat_unauthorized", err: errors.New("POST /api/v1/agent/heartbeat: unauthorized"), want: "auth"},
		{name: "events_unauthorized", err: errors.New("POST /api/v1/commands/abc123/events: unauthorized"), want: "auth"},
		// Network-class errors.
		{name: "connection_refused", err: errors.New("dial tcp 127.0.0.1:8000: connect: connection refused"), want: "network"},
		{name: "timeout", err: errors.New("context deadline exceeded"), want: "network"},
		{name: "500_error", err: errors.New("POST /api/v1/agent/commands/claim: 500 Internal Server Error"), want: "network"},
		// "unauthorized" not preceded by ": " should not match.
		{name: "path_contains_unauthorized", err: errors.New("GET /api/v1/unauthorized_check: 200 OK"), want: "network"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := classifyConnErr(tc.err)
			if got != tc.want {
				t.Errorf("classifyConnErr(%q) = %q, want %q", tc.err, got, tc.want)
			}
		})
	}
}
