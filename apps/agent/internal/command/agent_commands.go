package command

import (
	"context"
	"time"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/secret"
)

// AgentConfig carries the typed configuration the control plane delivers via
// ConfigUpdateCommand. Fields grow only by addition — never a map[string]any
// bag. OTLPToken and ApiKeys values are Secrets so they never leak into
// logs or serialized structs. Environment is the OTel
// `deployment.environment.name` resource attribute, plain string.
type AgentConfig struct {
	MaxWorkspaces int
	OTLPEndpoint  string
	OTLPToken     secret.Secret
	OTLPDataset   string
	Environment   string
	// ApiKeys maps provider_id → credential for per-org API keys
	// delivered by the control plane. The agent injects them as env vars
	// when spawning Claude Code (e.g. "anthropic" → ANTHROPIC_API_KEY).
	ApiKeys map[string]secret.Secret
}

// ConfigUpdateCommand is the only AgentCommand today. It carries an AgentConfig
// from the control plane; Execute calls AgentOps.ApplyConfig with it.
//
// Term note: this AgentCommand (the interface in command.go) is unrelated to
// the now-retired protocol.AgentCommand union struct.
type ConfigUpdateCommand struct {
	CommandHeader protocol.CommandHeader
	Config        AgentConfig
}

// secretFrom wraps a plain string as a secret.Secret. A local helper keeps the
// import of secret contained to this file.
func secretFrom(s string) secret.Secret {
	return secret.New(s)
}

// Header implements Command.
func (c *ConfigUpdateCommand) Header() protocol.CommandHeader {
	return c.CommandHeader
}

// Timeout implements Command. ConfigUpdate is a lightweight in-memory swap;
// 30 s is a conservative ceiling.
func (c *ConfigUpdateCommand) Timeout() time.Duration {
	return 30 * time.Second
}

// SetTraceparent implements Command.
func (c *ConfigUpdateCommand) SetTraceparent(tp string) { c.CommandHeader.Traceparent = tp }

// Execute calls ops.ApplyConfig with the command's AgentConfig and returns a
// ConfigUpdateResult. ConfigUpdateCommand is an AgentCommand — it always runs
// in the supervisor, never in a workspace child.
func (c *ConfigUpdateCommand) Execute(_ context.Context, ops AgentOps) (Result, error) {
	ops.ApplyConfig(c.Config)
	return ConfigUpdateResult{MaxWorkspaces: c.Config.MaxWorkspaces}, nil
}

// ShutdownCommand requests the agent to drain: stop accepting new workspaces,
// accelerate heartbeat cadence, and exit once all active workspaces finish.
type ShutdownCommand struct {
	CommandHeader protocol.CommandHeader
}

// Header implements Command.
func (c *ShutdownCommand) Header() protocol.CommandHeader { return c.CommandHeader }

// Timeout implements Command. Shutdown is a lightweight state flip; 30 s is a
// conservative ceiling.
func (c *ShutdownCommand) Timeout() time.Duration { return 30 * time.Second }

// SetTraceparent implements Command.
func (c *ShutdownCommand) SetTraceparent(tp string) { c.CommandHeader.Traceparent = tp }

// Execute calls ops.RequestShutdown and returns a ShutdownResult.
// ShutdownCommand is an AgentCommand — it always runs in the supervisor.
func (c *ShutdownCommand) Execute(_ context.Context, ops AgentOps) (Result, error) {
	ops.RequestShutdown()
	return ShutdownResult{}, nil
}

// CancelShutdownCommand cancels an in-progress drain: flip local lifecycle back
// to "active" and resume accepting new workspace commands.
type CancelShutdownCommand struct {
	CommandHeader protocol.CommandHeader
}

// Header implements Command.
func (c *CancelShutdownCommand) Header() protocol.CommandHeader { return c.CommandHeader }

// Timeout implements Command.
func (c *CancelShutdownCommand) Timeout() time.Duration { return 30 * time.Second }

// SetTraceparent implements Command.
func (c *CancelShutdownCommand) SetTraceparent(tp string) { c.CommandHeader.Traceparent = tp }

// Execute calls ops.CancelShutdown and returns a CancelShutdownResult.
// CancelShutdownCommand is an AgentCommand — it always runs in the supervisor.
func (c *CancelShutdownCommand) Execute(_ context.Context, ops AgentOps) (Result, error) {
	ops.CancelShutdown()
	return CancelShutdownResult{}, nil
}
