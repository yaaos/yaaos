package supervisor

import (
	"context"
	"os"

	"github.com/yaaos/agent/internal/observability"
)

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
// Normal behaviour (YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE unset): the returned
// agent_id and org_id must match the values pinned at startup, identical to
// the identity-integrity check in bearerRefreshLoop. A mismatch causes the
// process to exit.
//
// Test-stack behaviour (YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE=1): identity
// changes are accepted — the pinned agent_id / org_id and the base logger
// dimensions are updated in-place. This allows the test suite's resetStack()
// to truncate workspace_agents and then re-seed the org so the agent can
// recover without restarting the container. Never set in production.
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

	acceptChange := os.Getenv("YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE") == "1"
	if resp.AgentID != s.agentID || resp.OrgID != s.orgID {
		if !acceptChange {
			s.log.Error("supervisor.identity_mismatch_on_reauth",
				"pinned_agent_id", s.agentID,
				"pinned_org_id", s.orgID,
				"returned_agent_id", resp.AgentID,
				"returned_org_id", resp.OrgID,
			)
			// Unlock before exit so any runtime finalizers can run cleanly.
			s.reauthMu.Unlock()
			os.Exit(1)
		}
		// Test-only: accept the new identity so the agent continues
		// without restarting after a DB wipe.
		s.log.Warn("supervisor.identity_changed_on_reauth",
			"old_agent_id", s.agentID,
			"new_agent_id", resp.AgentID,
			"old_org_id", s.orgID,
			"new_org_id", resp.OrgID,
		)
		s.agentID = resp.AgentID
		s.orgID = resp.OrgID
		observability.SetStandardDimensions(s.orgID, s.agentID)
	}

	s.client.SetBearer(resp.Bearer)
	s.reauthMu.Unlock()
	s.log.Info("supervisor.reauth_succeeded",
		"agent_id", resp.AgentID,
		"org_id", resp.OrgID,
	)
	return true
}
