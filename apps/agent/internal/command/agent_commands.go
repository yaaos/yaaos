package command

import (
	"context"
	"time"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/secret"
)

// AgentConfig carries the typed configuration the control plane delivers via
// ConfigUpdateCommand. Fields grow only by addition — never a map[string]any
// bag. OTLPToken is a Secret so it never leaks into logs or serialized structs.
// Environment is the OTel `deployment.environment.name` resource attribute,
// plain string, never logged with the token.
type AgentConfig struct {
	MaxWorkspaces int
	OTLPEndpoint  string
	OTLPToken     secret.Secret
	OTLPDataset   string
	Environment   string
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
