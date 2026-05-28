// Package workspace implements the per-workspace `agent workspace` child
// process. The supervisor spawns one OS process per workspace handle and
// hands it two pipes:
//
//   - command pipe (stdin) — newline-framed JSON AgentCommands from the
//     supervisor.
//   - event pipe (stdout) — newline-framed JSON AgentEvents flowing back
//     to the supervisor, which then forwards them to the control plane
//     via `POST /api/v1/commands/{id}/events`.
//
// `Run` is the dispatch loop. It reads one command at a time, routes by
// `kind` to the matching `Handler` method, and writes either a
// `completed_success` event (per-kind outputs) or a `completed_failure`
// event (handler error) before reading the next command. EOF on the
// command pipe is the clean termination signal: Run returns nil.
//
// The dispatch frame is decoupled from command bodies: `git clone`,
// `WriteFiles`, the Claude Code subprocess, and `os.RemoveAll` all bolt
// onto the `Handler` interface, so the dispatcher itself is agnostic to
// the work each command does.
package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"go.opentelemetry.io/otel/attribute"

	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// Handler executes the action for each AgentCommand kind. Implementations
// return `(outputs, nil)` on success — outputs are placed verbatim on the
// emitted `completed_success` event. A non-nil error becomes a
// `completed_failure` event with the error's `Error()` string as the
// failure reason.
//
// Each method receives the typed concrete command (never the wrapper) so
// implementations can pull fields directly without re-dispatching on kind.
type Handler interface {
	CreateWorkspace(ctx context.Context, cmd *protocol.CreateWorkspaceCommand) (map[string]any, error)
	WriteFiles(ctx context.Context, cmd *protocol.WriteFilesCommand) (map[string]any, error)
	RefreshWorkspaceAuth(ctx context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (map[string]any, error)
	InvokeClaudeCode(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (map[string]any, error)
	CleanupWorkspace(ctx context.Context, cmd *protocol.CleanupWorkspaceCommand) (map[string]any, error)
}

// Now returns the timestamp stamped on emitted events. Tests override via
// `Options.Now` so event timestamps are deterministic.
type nowFunc func() time.Time

// Options tunes Run for tests and alternate Handler impls. Zero values
// pick safe production defaults.
type Options struct {
	// Now overrides time.Now for event timestamps. Defaults to time.Now.
	Now nowFunc
}

// Run dispatches commands from `in` to `h`, writing events to `out`.
// Returns nil on clean EOF; returns the first transport error otherwise.
// Cancelling `ctx` makes the next decode return an error and Run exits.
//
// The dispatcher is single-threaded by design: one command at a time,
// in-order. The supervisor spawns one workspace process per workspace
// handle, so concurrency lives one level up — at the supervisor's worker
// pool.
func Run(ctx context.Context, in io.Reader, out io.Writer, h Handler, opts Options) error {
	if h == nil {
		return errors.New("workspace: nil handler")
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	dec := ipc.NewDecoder(in)
	enc := ipc.NewEncoder(out)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		var cmd protocol.AgentCommand
		err := dec.Read(&cmd)
		if errors.Is(err, ipc.ErrClosed) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("workspace: read command: %w", err)
		}
		ev := dispatch(ctx, &cmd, h, opts.Now(), enc)
		if werr := enc.Write(ev); werr != nil {
			return fmt.Errorf("workspace: write event: %w", werr)
		}
	}
}

// dispatch routes one command into the right Handler method and converts
// the result into an AgentEvent. Pure transformation — pulled out so it
// can be exercised without pipe plumbing.
//
// Trace propagation: extracts the parent traceparent from the command
// header (the supervisor's dispatch span), starts a child
// `workspace.handle.<kind>` span around the Handler call, and writes its
// own traceparent onto the outgoing event so the supervisor sees the
// workspace's span as the upstream child.
//
// Progress emission: installs an `Emitter` into the handler's
// ctx that writes `kind=progress` AgentEvents to the same `enc`
// dispatcher uses for the terminal event. Handlers (e.g.
// `InvokeClaudeCode`'s stream-json line callback) read the emitter via
// `EmitterFromContext(ctx)` and call `Progress(outputs)` per line. The
// `ipc.Encoder` is goroutine-safe so concurrent progress writes from
// the handler interleave correctly with the dispatcher's final write.
func dispatch(ctx context.Context, cmd *protocol.AgentCommand, h Handler, now time.Time, enc *ipc.Encoder) protocol.AgentEvent {
	header := cmd.Header()
	ctx = tracing.ExtractContext(ctx, header.Traceparent)
	ctx, end := tracing.StartSpan(ctx, "workspace.handle."+string(cmd.Kind),
		attribute.String("workspace_id", header.WorkspaceID),
		attribute.String("command_id", header.CommandID),
		attribute.String("kind", string(cmd.Kind)),
	)
	childTP := tracing.InjectTraceparent(ctx)
	if enc != nil {
		ctx = ContextWithEmitter(ctx, newEncoderEmitter(enc, header.CommandID, childTP, time.Now))
	}
	base := protocol.AgentEvent{
		CommandID:   header.CommandID,
		ReportedAt:  now,
		Traceparent: childTP,
	}
	if base.Traceparent == "" {
		base.Traceparent = header.Traceparent
	}
	var (
		outputs map[string]any
		err     error
	)
	switch cmd.Kind {
	case protocol.KindCreateWorkspace:
		outputs, err = h.CreateWorkspace(ctx, cmd.CreateWorkspace)
	case protocol.KindWriteFiles:
		outputs, err = h.WriteFiles(ctx, cmd.WriteFiles)
	case protocol.KindRefreshWorkspaceAuth:
		outputs, err = h.RefreshWorkspaceAuth(ctx, cmd.RefreshWorkspaceAuth)
	case protocol.KindInvokeClaudeCode:
		outputs, err = h.InvokeClaudeCode(ctx, cmd.InvokeClaudeCode)
	case protocol.KindCleanupWorkspace:
		outputs, err = h.CleanupWorkspace(ctx, cmd.CleanupWorkspace)
	default:
		base.Kind = protocol.EventCompletedFailure
		base.FailureReason = fmt.Sprintf("unknown command kind %q", cmd.Kind)
		end(fmt.Errorf("unknown command kind %q", cmd.Kind))
		return base
	}
	if err != nil {
		base.Kind = protocol.EventCompletedFailure
		base.FailureReason = err.Error()
		end(err)
		return base
	}
	base.Kind = protocol.EventCompletedSuccess
	base.Outputs = outputs
	end(nil)
	return base
}

// StubHandler returns no-op success for every command kind. Useful for
// integration tests that exercise the dispatch loop + pipe plumbing
// without standing up a real workspace. Real bodies replace this in
// later slices.
type StubHandler struct{}

func (StubHandler) CreateWorkspace(_ context.Context, cmd *protocol.CreateWorkspaceCommand) (map[string]any, error) {
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"status":       "created",
	}, nil
}

func (StubHandler) WriteFiles(_ context.Context, cmd *protocol.WriteFilesCommand) (map[string]any, error) {
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"files_count":  len(cmd.Files),
	}, nil
}

func (StubHandler) RefreshWorkspaceAuth(_ context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (map[string]any, error) {
	return map[string]any{"workspace_id": cmd.WorkspaceID, "refreshed": true}, nil
}

func (StubHandler) InvokeClaudeCode(_ context.Context, cmd *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"status":       "stub_no_invocation",
	}, nil
}

func (StubHandler) CleanupWorkspace(_ context.Context, cmd *protocol.CleanupWorkspaceCommand) (map[string]any, error) {
	return map[string]any{"workspace_id": cmd.WorkspaceID, "destroyed": true}, nil
}

// MarshalEvent is a tiny helper exposed for symmetry with the Python
// supervisor's parsing layer. Not used by Run itself.
func MarshalEvent(ev protocol.AgentEvent) ([]byte, error) {
	return json.Marshal(ev)
}
