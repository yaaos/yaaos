// Package workspace implements the per-workspace `agent workspace` child
// process. The supervisor spawns one OS process per workspace handle and
// hands it two pipes:
//
//   - command pipe (stdin) — newline-framed JSON commands from the
//     supervisor.
//   - event pipe (stdout) — newline-framed JSON AgentEvents flowing back
//     to the supervisor, which then forwards them to the control plane
//     via `POST /api/v1/commands/{id}/events`.
//
// `Run` is the dispatch loop. It reads one command at a time, calls
// command.Decode, type-asserts to WorkspaceCommand, calls Execute against
// the provided WorkspaceOps, and writes either a `completed_success` event
// (typed result converted via ToWire) or a `completed_failure` event
// (handler error) before reading the next command. EOF on the command
// pipe is the clean termination signal: Run returns nil.
package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"go.opentelemetry.io/otel/attribute"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// Now returns the timestamp stamped on emitted events. Tests override via
// `Options.Now` so event timestamps are deterministic.
type nowFunc func() time.Time

// Options tunes Run for tests and alternate WorkspaceOps impls. Zero values
// pick safe production defaults.
type Options struct {
	// Now overrides time.Now for event timestamps. Defaults to time.Now.
	Now nowFunc
}

// Run dispatches commands from `in` to `ops`, writing events to `out`.
// Returns nil on clean EOF; returns the first transport error otherwise.
// Cancelling `ctx` makes the next decode return an error and Run exits.
//
// The dispatcher is single-threaded by design: one command at a time,
// in-order. The supervisor spawns one workspace process per workspace
// handle, so concurrency lives one level up — at the supervisor's worker
// pool.
func Run(ctx context.Context, in io.Reader, out io.Writer, ops command.WorkspaceOps, opts Options) error {
	if ops == nil {
		return errors.New("workspace: nil ops")
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
		var raw json.RawMessage
		err := dec.Read(&raw)
		if errors.Is(err, ipc.ErrClosed) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("workspace: read command: %w", err)
		}
		cmd, decErr := command.Decode(raw)
		if decErr != nil {
			return fmt.Errorf("workspace: decode command: %w", decErr)
		}
		wc, ok := cmd.(command.WorkspaceCommand)
		if !ok {
			return fmt.Errorf("workspace: received non-workspace command kind %q", cmd.Header().Kind)
		}
		ev := executeCommand(ctx, wc, ops, opts.Now(), enc)
		if werr := enc.Write(ev); werr != nil {
			return fmt.Errorf("workspace: write event: %w", werr)
		}
	}
}

// executeCommand runs one WorkspaceCommand against ops and returns the
// resulting AgentEvent. Trace propagation installs a child span around
// the Execute call and propagates traceparent to the event and to
// progress emissions.
func executeCommand(ctx context.Context, cmd command.WorkspaceCommand, ops command.WorkspaceOps, now time.Time, enc *ipc.Encoder) protocol.AgentEvent {
	header := cmd.Header()
	ctx = tracing.ExtractContext(ctx, header.Traceparent)
	spanAttrs := []attribute.KeyValue{
		attribute.String("workspace_id", header.WorkspaceID),
		attribute.String("command_id", header.CommandID),
		attribute.String("kind", string(header.Kind)),
	}
	if header.WorkflowExecutionID != "" {
		spanAttrs = append(spanAttrs, attribute.String("workflow_id", header.WorkflowExecutionID))
	}
	ctx, end := tracing.StartSpan(ctx, "workspace.handle."+string(header.Kind), spanAttrs...)
	childTP := tracing.InjectTraceparent(ctx)
	if enc != nil {
		ctx = ContextWithEmitter(ctx, newEncoderEmitter(enc, header.CommandID, childTP, header.CompletionToken, time.Now))
	}
	base := protocol.AgentEvent{
		CommandID:       header.CommandID,
		ReportedAt:      now,
		Traceparent:     childTP,
		CompletionToken: header.CompletionToken,
	}
	if base.Traceparent == "" {
		base.Traceparent = header.Traceparent
	}

	res, err := cmd.Execute(ctx, ops)
	if err != nil {
		base.Kind = protocol.EventCompletedFailure
		base.FailureReason = err.Error()
		end(err)
		return base
	}
	base.Kind = protocol.EventCompletedSuccess
	base.Outputs = res.ToWire()
	end(nil)
	return base
}

// MarshalEvent is a tiny helper exposed for symmetry with the protocol layer.
// Not used by Run itself.
func MarshalEvent(ev protocol.AgentEvent) ([]byte, error) {
	return json.Marshal(ev)
}
