// Production SpawnFunc that fork+execs `os.Args[0] workspace` as a child
// process. Parent ends of stdin/stdout become the command/event pipes;
// stderr is inherited (logs land in the supervisor's stderr). Close runs
// SIGTERM → grace → SIGKILL on the process group so the child's own
// children (a Claude Code subprocess that exec'd off the workspace) are
// reaped too.
//
// Tests use `supervisortest.InProcessSpawn` (package `internal/supervisor/supervisortest`)
// instead; this file is only exercised in real deployments + the docker-compose E2E.
package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/ipc"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// ExecSpawn returns a SpawnFunc that runs `<binary> workspace` as a child
// process. `binary` should be `os.Args[0]` in production — the same binary
// re-invoked with a different subcommand. The supervisor's stderr is
// inherited so workspace logs land beside the supervisor's.
//
// The closeGrace controls the SIGTERM→SIGKILL window for the process
// group (default 5s).
func ExecSpawn(binary string, closeGrace time.Duration, log Logger) SpawnFunc {
	if closeGrace <= 0 {
		closeGrace = 5 * time.Second
	}
	if log == nil {
		log = nullLogger{}
	}
	return func(ctx context.Context, workspaceID string) (WorkspaceRunner, error) {
		// We DON'T pass ctx to exec.Command — the runner survives the
		// claim-loop ctx for the lifetime of the workspace. Closing the
		// runner explicitly tears it down.
		cmd := exec.Command(binary, "workspace")
		cmd.Stderr = os.Stderr
		// Inherit the supervisor's env so the workspace + Claude Code
		// grand-children see PATH, HOME, etc. Append TRACEPARENT carrying
		// the current span context — the workspace process can pick that
		// up at startup and use it as the spawn-time parent for any of
		// its own startup spans. Per-command parents still travel on the
		// AgentCommand wire (per the protocol header), so this env value
		// is most useful when a grand-grand-child (Claude Code) needs to
		// inherit context from outside the command stream.
		cmd.Env = os.Environ()
		if tpEnv := tracing.TraceparentEnv(ctx); tpEnv != "" {
			cmd.Env = append(cmd.Env, tpEnv)
		}
		// Detach into its own process group so SIGKILL on the group reaps
		// any grand-children (Claude Code subprocess).
		cmd.SysProcAttr = procAttrNewPGroup()

		stdin, err := cmd.StdinPipe()
		if err != nil {
			return nil, fmt.Errorf("stdin pipe: %w", err)
		}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return nil, fmt.Errorf("stdout pipe: %w", err)
		}
		if err := cmd.Start(); err != nil {
			return nil, fmt.Errorf("start workspace: %w", err)
		}
		log.Info("exec_spawn.workspace_started",
			"workspace_id", workspaceID, "pid", cmd.Process.Pid)
		return &execRunner{
			cmd:        cmd,
			stdin:      stdin,
			stdout:     stdout,
			enc:        ipc.NewEncoder(stdin),
			dec:        ipc.NewDecoder(stdout),
			closeGrace: closeGrace,
			log:        log,
			workspace:  workspaceID,
		}, nil
	}
}

type execRunner struct {
	cmd        *exec.Cmd
	stdin      io.WriteCloser
	stdout     io.ReadCloser
	enc        *ipc.Encoder
	dec        *ipc.Decoder
	closeGrace time.Duration
	log        Logger
	workspace  string

	closeOnce sync.Once
}

func (r *execRunner) Send(ctx context.Context, cmd command.WorkspaceCommand, onProgress func(protocol.AgentEvent)) (protocol.AgentEvent, error) {
	wireBytes, err := cmd.MarshalWire()
	if err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("encode command: %w", err)
	}
	if err := r.enc.Write(json.RawMessage(wireBytes)); err != nil {
		return protocol.AgentEvent{}, fmt.Errorf("write command: %w", err)
	}
	// Read events in a loop — see `inProcessRunner.Send` for the
	// rationale. The workspace subprocess emits progress events while
	// running (Claude Code stream-json output) followed by exactly one
	// terminal event; we forward each progress event to onProgress and
	// return the terminal event to the caller.
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
		// Tear down the runner so the goroutine unblocks. Caller will
		// observe ctx.Err and drop the slot.
		_ = r.stdout.Close()
		<-resultCh
		return protocol.AgentEvent{}, ctx.Err()
	case res := <-resultCh:
		if res.err != nil {
			return protocol.AgentEvent{}, fmt.Errorf("read event: %w", res.err)
		}
		return res.ev, nil
	}
}

// Close runs SIGTERM → wait closeGrace → SIGKILL on the process group.
// Idempotent — calling twice is safe.
func (r *execRunner) Close(_ context.Context) error {
	r.closeOnce.Do(func() {
		_ = r.stdin.Close() // workspace.Run sees EOF, exits cleanly
		if r.cmd.Process == nil {
			return
		}
		done := make(chan error, 1)
		go func() { done <- r.cmd.Wait() }()
		select {
		case <-done:
			return
		case <-time.After(r.closeGrace):
			r.log.Warn("exec_spawn.grace_elapsed_sigterm",
				"workspace_id", r.workspace, "pid", r.cmd.Process.Pid)
			killGroup(r.cmd.Process.Pid, syscall.SIGTERM)
		}
		select {
		case <-done:
			return
		case <-time.After(2 * time.Second):
			r.log.Warn("exec_spawn.grace_elapsed_sigkill",
				"workspace_id", r.workspace, "pid", r.cmd.Process.Pid)
			killGroup(r.cmd.Process.Pid, syscall.SIGKILL)
			<-done
		}
	})
	return nil
}
