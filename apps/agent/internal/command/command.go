// Package command owns the polymorphic Command interface, all concrete command
// types, the WorkspaceOps/AgentOps capability seams, typed results, and the
// Decode factory.
//
// Two command families:
//   - WorkspaceCommand — executed in the workspace child process via WorkspaceOps;
//     the five workspace kinds belong here.
//   - AgentCommand — executed in the supervisor via AgentOps; ConfigUpdateCommand
//     is the only kind today.
//
// Adding a command kind: implement WorkspaceCommand or AgentCommand, add one
// case to Decode. See apps/agent/docs/command.md for the full how-to.
package command

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// Command is the polymorphic root implemented by every command kind. It
// provides the fields the supervisor needs before dispatching, plus the
// per-kind deadline so commands carry their own time budget.
type Command interface {
	// Header returns the embedded wire header (command_id, workspace_id,
	// traceparent, kind). Callers use this for logging and ack flow without
	// needing to know the concrete type.
	Header() protocol.CommandHeader
	// Timeout returns the maximum wall-clock duration the supervisor allows
	// for this command. Durations come from the wire (InvokeClaudeCode) or
	// Go-side defaults (all other kinds).
	Timeout() time.Duration
}

// WorkspaceCommand is a Command that executes work inside a workspace child
// process. The five workspace kinds implement this interface.
type WorkspaceCommand interface {
	Command
	Execute(ctx context.Context, ops WorkspaceOps) (Result, error)
}

// AgentCommand is a Command that executes in the supervisor itself. Only
// ConfigUpdateCommand implements this today.
//
// Term note: this AgentCommand is unrelated to the now-retired
// protocol.AgentCommand union wrapper. Same term, different concept.
type AgentCommand interface {
	Command
	Execute(ctx context.Context, ops AgentOps) (Result, error)
}

// Decode is the one surviving kind-switch. It peeks the `kind` field of raw
// JSON, unmarshals into the right concrete type, and returns it as a Command.
// Unknown kinds are an error — the caller MUST NOT dispatch a command shape it
// doesn't understand (mirrors the behaviour of the retired
// protocol.AgentCommand.UnmarshalJSON).
func Decode(raw []byte) (Command, error) {
	var probe struct {
		Kind protocol.CommandKind `json:"kind"`
	}
	if err := json.Unmarshal(raw, &probe); err != nil {
		return nil, fmt.Errorf("command: probe kind: %w", err)
	}
	switch probe.Kind {
	case protocol.KindCreateWorkspace:
		var v protocol.CreateWorkspaceCommand
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, fmt.Errorf("command: decode CreateWorkspace: %w", err)
		}
		return &CreateWorkspaceCommand{Proto: v}, nil
	case protocol.KindWriteFiles:
		var v protocol.WriteFilesCommand
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, fmt.Errorf("command: decode WriteFiles: %w", err)
		}
		return &WriteFilesCommand{Proto: v}, nil
	case protocol.KindRefreshWorkspaceAuth:
		var v protocol.RefreshWorkspaceAuthCommand
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, fmt.Errorf("command: decode RefreshWorkspaceAuth: %w", err)
		}
		return &RefreshWorkspaceAuthCommand{Proto: v}, nil
	case protocol.KindInvokeClaudeCode:
		var v protocol.InvokeClaudeCodeCommand
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, fmt.Errorf("command: decode InvokeClaudeCode: %w", err)
		}
		return &InvokeClaudeCodeCommand{Proto: v}, nil
	case protocol.KindCleanupWorkspace:
		var v protocol.CleanupWorkspaceCommand
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, fmt.Errorf("command: decode CleanupWorkspace: %w", err)
		}
		return &CleanupWorkspaceCommand{Proto: v}, nil
	case protocol.KindConfigUpdate:
		var w configUpdateWire
		if err := json.Unmarshal(raw, &w); err != nil {
			return nil, fmt.Errorf("command: decode ConfigUpdate: %w", err)
		}
		return &ConfigUpdateCommand{
			CommandHeader: w.CommandHeader,
			Config: AgentConfig{
				MaxWorkspaces: w.MaxWorkspaces,
				OTLPEndpoint:  w.OTLPEndpoint,
				OTLPToken:     secretFrom(w.OTLPToken),
				OTLPDataset:   w.OTLPDataset,
			},
		}, nil
	default:
		return nil, fmt.Errorf("command: unknown kind %q", probe.Kind)
	}
}
