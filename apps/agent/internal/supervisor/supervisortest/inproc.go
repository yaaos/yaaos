// Package supervisortest provides an in-process SpawnFunc implementation for
// use in tests. Import only from _test.go files — depguard enforces this.
//
// InProcessSpawn returns a function whose type is assignment-compatible with
// supervisor.SpawnFunc. supervisortest does not import supervisor to avoid an
// import cycle with same-package supervisor tests.
package supervisortest

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace"
)

// WorkspaceRunner mirrors supervisor.WorkspaceRunner. The two interfaces are
// structurally identical; values of either type satisfy the other.
type WorkspaceRunner interface {
	Send(ctx context.Context, cmd command.WorkspaceCommand, onProgress func(protocol.AgentEvent)) (protocol.AgentEvent, error)
	Close(ctx context.Context) error
}

// InProcessSpawn returns a function that runs workspace.Run(ops) in a
// goroutine connected by io.Pipe pairs. Its type is structurally identical to
// supervisor.SpawnFunc and can be assigned to it directly. Callers must supply
// a non-nil ops; use workspacetest.StubHandler{} for a no-op implementation.
func InProcessSpawn(ops command.WorkspaceOps) func(ctx context.Context, workspaceID string) (WorkspaceRunner, error) {
	if ops == nil {
		panic("supervisortest.InProcessSpawn: ops must not be nil; use workspacetest.StubHandler{} for a no-op")
	}
	return func(ctx context.Context, _ string) (WorkspaceRunner, error) {
		cmdR, cmdW := io.Pipe()
		evR, evW := io.Pipe()
		runCtx, runCancel := context.WithCancel(ctx)
		runner := &inProcessRunner{
			cmdW:      cmdW,
			evR:       evR,
			cmdR:      cmdR,
			evW:       evW,
			enc:       ipc.NewEncoder(cmdW),
			dec:       ipc.NewDecoder(evR),
			runCancel: runCancel,
			done:      make(chan struct{}),
		}
		go func() {
			defer close(runner.done)
			_ = workspace.Run(runCtx, cmdR, evW, ops, workspace.Options{})
			_ = evW.Close() // signal EOF to the parent decoder
		}()
		return runner, nil
	}
}

// inProcessRunner wraps workspace.Run in a goroutine fed by io.Pipe pairs.
type inProcessRunner struct {
	cmdW *io.PipeWriter
	evR  *io.PipeReader

	cmdR *io.PipeReader // kept so the goroutine sees EOF when we close
	evW  *io.PipeWriter

	enc *ipc.Encoder
	dec *ipc.Decoder

	runCancel context.CancelFunc
	done      chan struct{}
}

func (r *inProcessRunner) Send(ctx context.Context, cmd command.WorkspaceCommand, onProgress func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	wireBytes, err := cmd.MarshalWire()
	if err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("encode command: %w", err)
	}
	if err := r.enc.Write(json.RawMessage(wireBytes)); err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("write command: %w", err)
	}
	resultCh := make(chan readResult, 1)
	go func() {
		for {
			var ev protocol.AgentEvent
			if err := r.dec.Read(&ev); err != nil {
				resultCh <- readResult{err: err}
				return
			}
			if ev.Kind == protocol.EventProgress {
				if onProgress != nil {
					onProgress(ev)
				}
				continue
			}
			resultCh <- readResult{ev: ev}
			return
		}
	}()
	select {
	case <-ctx.Done():
		_ = r.evR.CloseWithError(ctx.Err())
		<-resultCh
		return protocol.AgentEvent{}, ctx.Err()
	case res := <-resultCh:
		if res.err != nil {
			return protocol.AgentEvent{}, fmt.Errorf("read event: %w", res.err)
		}
		return res.ev, nil
	}
}

type readResult struct {
	ev  protocol.AgentEvent
	err error
}

func (r *inProcessRunner) Close(_ context.Context) error {
	_ = r.cmdW.Close()
	r.runCancel()
	select {
	case <-r.done:
	case <-time.After(2 * time.Second):
		_ = r.cmdR.Close()
		_ = r.evW.Close()
		<-r.done
	}
	return nil
}
