//go:build agent_test

package supervisor

import "os"

// acceptIdentityChange (agent_test build) honors YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE=1
// so the e2e suite's resetStack() can truncate workspace_agents and re-seed the
// org; the agent then re-authenticates with a fresh agent_id and continues
// without a container restart.
//
// This file is compiled only under `-tags agent_test`. The production binary
// uses the always-false variant in identity_seam_off.go, so the env var is
// inert outside test builds.
func acceptIdentityChange() bool {
	return os.Getenv("YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE") == "1"
}
