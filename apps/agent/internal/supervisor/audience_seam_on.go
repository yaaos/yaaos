//go:build agent_dev

package supervisor

import "os"

// audienceOverride (agent_dev build) honors YAAOS_AGENT_AUDIENCE_OVERRIDE so
// local dev stacks where the agent connects over an internal Docker service name
// (e.g. "web:8080") but the backend's public hostname is a host-mapped address
// (e.g. "localhost:8080") can reconcile the mismatch without changing
// YAAOS_PUBLIC_ORIGIN (which would break browser-facing OAuth/email links).
//
// This file is compiled only under `-tags agent_dev`. The production binary uses
// the always-empty variant in audience_seam_off.go, so the env var is inert
// outside dev builds.
func audienceOverride() string { return os.Getenv("YAAOS_AGENT_AUDIENCE_OVERRIDE") }
