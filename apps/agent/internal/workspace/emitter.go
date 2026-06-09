// Progress-event emitter — workspace handlers (long-running ones, e.g.
// `InvokeClaudeCode`) use this to push in-flight `EventProgress`
// AgentEvents upstream while their work is still running. The supervisor
// consumes them via `WorkspaceRunner.Send`'s `onProgress` callback and
// forwards each one to the control plane.
//
// Without this, a long Claude Code invocation produces exactly one
// terminal event when it completes — the backend can't tell whether
// the agent's progressing or hung, and the UI can't show live activity.
//
// The emitter is installed into the dispatch ctx by `workspace.Run`
// before each Handler call. Handlers pull it via `EmitterFromContext`
// and call `Progress(outputs)` per stream-event. The default emitter
// writes a `kind=progress` AgentEvent to the same IPC encoder the
// dispatcher uses for the terminal event — `ipc.Encoder` is
// goroutine-safe so concurrent Progress calls + the dispatcher's final
// write are serialised internally.

package workspace

import (
	"context"
	"time"

	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
)

// Emitter pushes a progress AgentEvent upstream. The returned bool is
// reserved for backpressure (false would mean "drop this event"); the
// current implementation always returns true since the IPC pipe blocks
// naturally on the supervisor's slow read.
type Emitter interface {
	Progress(outputs map[string]any) bool
}

// emitterCtxKey is the private context-key type for the emitter
// install. Using a struct rather than a string sidesteps the
// `staticcheck` "ctx key collisions" warning.
type emitterCtxKey struct{}

// ContextWithEmitter returns a derived ctx carrying `e`. Handlers
// invoked from `workspace.Run` see the emitter via
// `EmitterFromContext`. Tests that drive a handler directly can install
// their own Emitter via this helper.
func ContextWithEmitter(ctx context.Context, e Emitter) context.Context {
	return context.WithValue(ctx, emitterCtxKey{}, e)
}

// EmitterFromContext returns the emitter installed by `workspace.Run`,
// or `noopEmitter{}` if none is set. Handlers can call `Progress` on
// the result unconditionally — the no-op path is silent.
func EmitterFromContext(ctx context.Context) Emitter {
	if e, ok := ctx.Value(emitterCtxKey{}).(Emitter); ok && e != nil {
		return e
	}
	return noopEmitter{}
}

// encoderEmitter is the production emitter `workspace.Run` installs. It
// writes a `kind=progress` AgentEvent to the underlying IPC encoder.
// The header fields (`command_id`, `traceparent`, `reported_at`) come
// from the command the dispatcher is currently servicing.
type encoderEmitter struct {
	enc             *ipc.Encoder
	commandID       string
	traceparent     string
	completionToken string
	now             func() time.Time
}

// newEncoderEmitter wires an emitter to an IPC encoder for one
// in-flight command. `now` is injected so tests can pin the timestamp.
// completionToken comes from the command header and is echoed on every
// progress AgentEvent so the backend authorizes it by hash.
func newEncoderEmitter(enc *ipc.Encoder, commandID, traceparent, completionToken string, now func() time.Time) *encoderEmitter {
	if now == nil {
		now = time.Now
	}
	return &encoderEmitter{enc: enc, commandID: commandID, traceparent: traceparent, completionToken: completionToken, now: now}
}

func (e *encoderEmitter) Progress(outputs map[string]any) bool {
	if e == nil || e.enc == nil {
		return false
	}
	ev := protocol.AgentEvent{
		CommandID:       e.commandID,
		Kind:            protocol.EventProgress,
		Outputs:         outputs,
		ReportedAt:      e.now().UTC(),
		Traceparent:     e.traceparent,
		CompletionToken: e.completionToken,
	}
	// IPC encoder serialises writes — concurrent Progress calls + the
	// dispatcher's terminal-event write produce well-ordered frames on
	// the pipe.
	if err := e.enc.Write(ev); err != nil {
		// Writing failed (pipe closed, parent gone). Caller has no
		// useful recourse — the dispatcher will fail on its own next
		// write. Drop the event.
		return false
	}
	return true
}

// noopEmitter is the fallback for handlers running outside
// `workspace.Run` (unit tests that drive the handler directly without
// dispatcher setup). Progress is silently dropped.
type noopEmitter struct{}

func (noopEmitter) Progress(map[string]any) bool { return false }
