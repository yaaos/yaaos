//go:build !agent_test

package supervisor

// acceptIdentityChange reports whether the agent may keep running under a
// different agent_id/org_id than the one pinned at startup.
//
// In the production binary this is always false: an identity change is an
// integrity violation and the caller exits the process. The env-var-honoring
// variant lives in identity_seam_on.go and is compiled only with
// `-tags agent_test`, so the production binary has no code path that reads
// YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE — the bypass cannot be enabled regardless
// of how the environment is configured.
func acceptIdentityChange() bool { return false }
