//go:build agent_test

// These tests exercise the identity-change seam (acceptIdentityChange), which
// is compiled to honor YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE only under the
// agent_test build tag. bin/ci runs this package with `-tags agent_test`.

package supervisor

import (
	"context"
	"errors"
	"testing"
	"time"
)

// classifyConnErr-recognized error shapes (see classify_test.go).
var (
	errAuth    = errors.New("claim: unauthorized")
	errNetwork = errors.New("dial tcp 127.0.0.1:8000: connect: connection refused")
)

func newReauthSupervisor(t *testing.T, baseURL string, prov *stubProvider) *Supervisor {
	t.Helper()
	if prov == nil {
		prov = &stubProvider{}
	}
	return buildSupervisorForIdentityTest(
		Config{BaseURL: baseURL, Version: "0.0.1"}, prov, "agent-abc", "org-xyz")
}

// (a) A non-auth error returns false without attempting an exchange.
func TestReauth_NonAuthError_ReturnsFalse(t *testing.T) {
	s := newReauthSupervisor(t, "http://unused", nil)
	if s.reauthIfUnauthorized(context.Background(), errNetwork) {
		t.Error("non-auth error should return false")
	}
}

// (b) The TryLock loser returns false immediately while the winner holds the
// lock. Run under -race to assert no data race on the shared lock/identity.
func TestReauth_TryLockLoser_ReturnsFalse(t *testing.T) {
	s := newReauthSupervisor(t, "http://unused", nil)
	s.reauthMu.Lock() // simulate the winning goroutine mid-exchange
	defer s.reauthMu.Unlock()

	done := make(chan bool, 1)
	go func() { done <- s.reauthIfUnauthorized(context.Background(), errAuth) }()
	select {
	case got := <-done:
		if got {
			t.Error("TryLock loser should return false")
		}
	case <-time.After(time.Second):
		t.Fatal("reauth blocked on the held lock instead of returning false")
	}
}

// (c) A failed exchange returns false and releases the lock for the next attempt.
func TestReauth_ExchangeFailure_ReturnsFalse(t *testing.T) {
	s := newReauthSupervisor(t, "http://unused", &stubProvider{SignErr: errors.New("sign boom")})
	if s.reauthIfUnauthorized(context.Background(), errAuth) {
		t.Error("exchange failure should return false")
	}
	if !s.reauthMu.TryLock() {
		t.Error("reauthMu must be released after an exchange failure")
	}
	s.reauthMu.Unlock()
}

// (d) A matching identity returns true and leaves the pinned identity unchanged.
func TestReauth_MatchingIdentity_ReturnsTrue(t *testing.T) {
	srv := fakeExchangeServer(t, "agent-abc", "org-xyz", "bearer-v2")
	defer srv.Close()
	s := newReauthSupervisor(t, srv.URL, nil)

	if !s.reauthIfUnauthorized(context.Background(), errAuth) {
		t.Error("matching identity should return true")
	}
	if s.agentID != "agent-abc" || s.orgID != "org-xyz" {
		t.Errorf("identity should be unchanged, got %s/%s", s.agentID, s.orgID)
	}
}

// (e) With the seam enabled, a mismatched identity is accepted in place.
func TestReauth_MismatchedIdentity_AcceptChange_UpdatesIdentity(t *testing.T) {
	t.Setenv("YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE", "1")
	srv := fakeExchangeServer(t, "agent-NEW", "org-NEW", "bearer-v2")
	defer srv.Close()
	s := newReauthSupervisor(t, srv.URL, nil)

	if !s.reauthIfUnauthorized(context.Background(), errAuth) {
		t.Fatal("accept-change reauth should return true")
	}
	if s.agentID != "agent-NEW" || s.orgID != "org-NEW" {
		t.Errorf("identity should be updated to the new values, got %s/%s", s.agentID, s.orgID)
	}
}

// (f) Without the seam enabled, a mismatched identity exits the process and the
// pinned identity is never mutated. osExit is swapped to observe the exit.
func TestReauth_MismatchedIdentity_Reject_CallsExit(t *testing.T) {
	t.Setenv("YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE", "0") // anything but "1" → reject
	srv := fakeExchangeServer(t, "agent-IMPOSTER", "org-xyz", "bearer-bad")
	defer srv.Close()
	s := newReauthSupervisor(t, srv.URL, nil)

	var exited bool
	var exitCode int
	orig := osExit
	osExit = func(code int) { exited = true; exitCode = code }
	defer func() { osExit = orig }()

	got := s.reauthIfUnauthorized(context.Background(), errAuth)
	if !exited {
		t.Fatal("a rejected identity change should call osExit")
	}
	if exitCode != 1 {
		t.Errorf("exit code: want 1, got %d", exitCode)
	}
	if got {
		t.Error("reauth should return false after a rejected identity change")
	}
	if s.agentID != "agent-abc" || s.orgID != "org-xyz" {
		t.Errorf("pinned identity must not change on reject, got %s/%s", s.agentID, s.orgID)
	}
}
