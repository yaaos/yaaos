package supervisor

import (
	"context"
	"os"

	"github.com/yaaos/agent/internal/observability"
	"github.com/yaaos/agent/internal/protocol"
)

// osExit is the process-exit hook. It is os.Exit in every build; tests in this
// package swap it to observe the identity-mismatch exit without killing the
// test runner. Private, so it can never be swapped from another package.
var osExit = os.Exit

// reauthIfUnauthorized attempts a fresh identity exchange when `err` is a
// 401/403 response. Returns true when re-authentication succeeded and the
// caller should reset its backoff counter; false otherwise.
//
// Only one goroutine at a time performs the exchange. When N claim workers all
// receive 401 simultaneously (e.g. after a DB wipe clears workspace_agents),
// only the winner of s.reauthMu calls exchangeIdentity; the others return false
// and let their callers continue to the normal backoff path. The winner updates
// s.client.SetBearer so the next claim attempt by any worker uses the fresh
// token.
//
// Identity integrity: the returned agent_id and org_id must match the values
// pinned at startup. A mismatch exits the process (production) unless
// acceptIdentityChange() is true (agent_test builds only — see
// identity_seam_on.go), which updates the pinned identity in place so the e2e
// suite can recover after a DB wipe. This is the same rule runOneRefreshCycle
// applies on the scheduled bearer-refresh path.
func (s *Supervisor) reauthIfUnauthorized(ctx context.Context, err error) bool {
	if classifyConnErr(err) != "auth" {
		return false
	}
	// Only one goroutine performs the exchange at a time. If another is already
	// holding the lock, skip — when it finishes it will update the shared bearer
	// and the caller's next attempt will use the fresh token.
	if !s.reauthMu.TryLock() {
		return false
	}

	resp, exchErr := s.exchangeIdentity(ctx)
	if exchErr != nil {
		s.reauthMu.Unlock()
		s.log.Warn("supervisor.reauth_failed", "err", exchErr.Error())
		return false
	}

	if resp.AgentID != s.agentID || resp.OrgID != s.orgID {
		if !acceptIdentityChange() {
			s.log.Error("supervisor.identity_mismatch_on_reauth",
				"pinned_agent_id", s.agentID,
				"pinned_org_id", s.orgID,
				"returned_agent_id", resp.AgentID,
				"returned_org_id", resp.OrgID,
			)
			// Unlock before exit so any runtime finalizers can run cleanly.
			s.reauthMu.Unlock()
			osExit(1)
			return false // unreachable in production; reachable when tests swap osExit.
		}
		s.applyAcceptedIdentityChange(resp)
	}

	s.client.SetBearer(resp.Bearer)
	s.reauthMu.Unlock()
	s.log.Info("supervisor.reauth_succeeded",
		"agent_id", resp.AgentID,
		"org_id", resp.OrgID,
	)
	return true
}

// applyAcceptedIdentityChange updates the pinned identity and observability
// dimensions in place after an exchange returned a different agent_id/org_id.
// Only reached when acceptIdentityChange() is true, i.e. in agent_test builds;
// both reauth surfaces route through it so the accept rule lives in one place.
func (s *Supervisor) applyAcceptedIdentityChange(resp *protocol.IdentityExchangeResponse) {
	s.log.Warn("supervisor.identity_changed",
		"old_agent_id", s.agentID,
		"new_agent_id", resp.AgentID,
		"old_org_id", s.orgID,
		"new_org_id", resp.OrgID,
	)
	s.agentID = resp.AgentID
	s.orgID = resp.OrgID
	observability.SetStandardDimensions(s.orgID, s.agentID)
}
