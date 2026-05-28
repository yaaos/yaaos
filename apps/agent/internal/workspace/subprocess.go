package workspace

// Streaming subprocess primitive. The `InvokeClaudeCode`
// handler body composes this — Claude Code emits stream-json events on
// stdout (one JSON object per line), and the supervisor needs to forward
// each line as an `activity_batch` over the WebSocket while the
// subprocess is still running. Reading stdout to EOF and parsing at the
// end loses that real-time signal.
//
// `RunStreaming` is intentionally Claude-Code-agnostic — the InvokeClaudeCode
// body is its caller, but the same primitive serves any
// "long-running subprocess with line-by-line stdout output"
// case (e.g. yaaos-skills indexers, the disk janitor's `du` walk).
//
// What's covered:
//   - Per-line stdout streaming via a callback. Lines are CRLF-tolerant.
//   - Stderr captured in full (up to a 1MB cap) for diagnostics.
//   - stdin piped from a caller-supplied byte slice. Written in full
//     before stream parsing begins (matches the Claude Code shape:
//     stdin = prompt, sent once before stream output starts).
//   - Wall-clock timeout via context. On ctx.Done: SIGTERM the process
//     group, wait 2s, SIGKILL. Returns ctx.Err() and partial output.
//   - Process group isolation: child runs in its own pgid (via the
//     existing `procAttrNewPGroup` helper from supervisor's exec_spawn,
//     duplicated here because Go's `internal/` rules don't let workspace
//     import supervisor).
//
// What's NOT covered (deliberate — caller's problem):
//   - JSON parsing of the stdout lines. Callback receives raw bytes;
//     each caller (Claude Code, others) knows its own wire shape.
//   - Backpressure if the callback is slow. The pipe buffer fills, the
//     subprocess blocks on write. Caller MUST keep the callback cheap
//     (push-to-channel-and-return is the canonical shape).

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"syscall"
	"time"
)

// RunStreamingOptions configures one subprocess execution.
type RunStreamingOptions struct {
	// Argv is the full command + arguments, e.g. `["claude", "--print",
	// "--output-format=stream-json", "--model", "opus"]`. Argv[0] is the
	// binary, found via PATH unless absolute.
	Argv []string

	// Stdin bytes piped to the child before stream parsing starts. May be
	// empty. Written in full before reading stdout begins — matches the
	// "send prompt once, then read stream events" pattern.
	Stdin []byte

	// Env: full environment for the child. nil means inherit the parent's.
	// Set to a fresh slice (no os.Environ) to give the child a clean env;
	// callers building Claude Code argv typically merge ANTHROPIC_API_KEY
	// + TRACEPARENT into `os.Environ()`.
	Env []string

	// Dir is the working directory for the child. Empty means the
	// parent's cwd. For InvokeClaudeCode this is the workspace tempdir.
	Dir string

	// OnStdoutLine is invoked for every newline-terminated line the
	// child writes to stdout. Lines arrive in-order; the callback's
	// goroutine is the same one driving the pipe read, so a slow
	// callback backpressures the subprocess (intentional — the
	// supervisor's WebSocket forward decides the rate).
	//
	// nil means stdout is drained into a buffer + returned whole at the
	// end (small-output mode for callers that don't care about
	// streaming).
	OnStdoutLine func(line []byte)

	// MaxStderrBytes caps the buffered stderr returned in the result.
	// Defaults to 1MB. Excess is silently dropped.
	MaxStderrBytes int
}

// RunStreamingResult is what RunStreaming returns on completion.
type RunStreamingResult struct {
	// ExitCode is the child's exit status. -1 if the process was
	// signalled (e.g. our SIGTERM on ctx cancel).
	ExitCode int

	// Stdout is the full buffered stdout, populated ONLY when
	// `OnStdoutLine` is nil. Streaming callers don't double-buffer.
	Stdout []byte

	// Stderr is buffered up to `MaxStderrBytes`.
	Stderr []byte

	// Duration is the wall-clock time the child ran.
	Duration time.Duration

	// TimedOut is true when the run terminated because ctx hit its
	// deadline / was cancelled.
	TimedOut bool
}

// RunStreaming spawns argv, pipes stdin to it, streams stdout
// line-by-line through OnStdoutLine (or buffers it when nil), captures
// stderr, and returns the result. On ctx cancel / deadline, terminates
// the process group via SIGTERM then SIGKILL after 2s grace.
//
// Errors returned:
//   - exec startup failures (binary not on PATH, permission denied, ...).
//   - non-zero exit code from the child: the result is still populated;
//     callers inspect ExitCode + Stderr.
//   - ctx.Err() when the run was cancelled — TimedOut is set.
//
// Result and error can be combined: a non-zero exit + ctx cancel both
// populate the result so callers see the partial state regardless of
// which signal won the race.
func RunStreaming(ctx context.Context, opts RunStreamingOptions) (*RunStreamingResult, error) {
	if len(opts.Argv) == 0 {
		return nil, errors.New("RunStreaming: empty argv")
	}
	if opts.MaxStderrBytes <= 0 {
		opts.MaxStderrBytes = 1 << 20 // 1 MiB
	}

	start := time.Now()
	cmd := exec.Command(opts.Argv[0], opts.Argv[1:]...)
	cmd.Env = opts.Env
	cmd.Dir = opts.Dir
	cmd.SysProcAttr = procAttrNewPGroupWS()

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start: %w", err)
	}

	// stdin: write what we have, close. A slow consumer will backpressure;
	// the goroutine ensures stdout reading isn't blocked by stdin writing.
	go func() {
		defer func() { _ = stdin.Close() }()
		if len(opts.Stdin) > 0 {
			_, _ = stdin.Write(opts.Stdin)
		}
	}()

	// stdout: either buffer in full or stream line-by-line via callback.
	var stdoutBuf bytes.Buffer
	stdoutDone := make(chan struct{})
	go func() {
		defer close(stdoutDone)
		if opts.OnStdoutLine == nil {
			_, _ = io.Copy(&stdoutBuf, stdout)
			return
		}
		scanner := bufio.NewScanner(stdout)
		// stream-json events can be large; bump the buffer cap so a
		// single fat line doesn't trigger ErrTooLong.
		scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for scanner.Scan() {
			line := scanner.Bytes()
			// Defensive copy: scanner reuses the underlying buffer
			// across iterations, so the callback can't safely hold a
			// reference past the next Scan.
			lineCopy := make([]byte, len(line))
			copy(lineCopy, line)
			opts.OnStdoutLine(lineCopy)
		}
	}()

	// stderr: capped buffer.
	var stderrBuf bytes.Buffer
	stderrDone := make(chan struct{})
	go func() {
		defer close(stderrDone)
		lim := io.LimitReader(stderr, int64(opts.MaxStderrBytes))
		_, _ = io.Copy(&stderrBuf, lim)
		// Drain the rest so the child doesn't block on a full stderr
		// pipe even if we're not buffering further.
		_, _ = io.Copy(io.Discard, stderr)
	}()

	// Wait in a goroutine, but only AFTER both stdout + stderr readers
	// have drained. `cmd.Wait` closes parent-side pipes the instant the
	// child exits — calling it before our io.Copy goroutines have seen
	// EOF races the closer against the reader, occasionally clipping the
	// captured output to empty bytes (observed as a slice-73 flake on
	// short-lived commands like `pwd` and `echo $VAR`). Per `*Cmd.Wait`
	// docs: "incorrect to call Wait before all reads from the pipe have
	// completed".
	//
	// On ctx cancel we need to signal the child without waiting for
	// readers (the child might be hung holding the pipe open) — see the
	// killer path below.
	waitDone := make(chan error, 1)
	go func() {
		<-stdoutDone
		<-stderrDone
		waitDone <- cmd.Wait()
	}()

	timedOut := false
	var waitErr error
	select {
	case waitErr = <-waitDone:
		// Process exited on its own; readers already drained (waiter
		// only fires after both done channels close).
	case <-ctx.Done():
		timedOut = true
		// SIGTERM the process group + give a 2s grace, then SIGKILL.
		// Killing the child closes its stdout/stderr, which lets our
		// reader goroutines EOF and the waiter then call cmd.Wait.
		killGroupWS(cmd.Process.Pid, syscall.SIGTERM)
		select {
		case waitErr = <-waitDone:
		case <-time.After(2 * time.Second):
			killGroupWS(cmd.Process.Pid, syscall.SIGKILL)
			waitErr = <-waitDone
		}
	}

	exitCode := -1
	if cmd.ProcessState != nil {
		exitCode = cmd.ProcessState.ExitCode()
	}
	res := &RunStreamingResult{
		ExitCode: exitCode,
		Stderr:   stderrBuf.Bytes(),
		Duration: time.Since(start),
		TimedOut: timedOut,
	}
	if opts.OnStdoutLine == nil {
		res.Stdout = stdoutBuf.Bytes()
	}
	if timedOut {
		return res, ctx.Err()
	}
	if waitErr != nil {
		// Non-zero exit code is wrapped in *exec.ExitError — the result
		// is still populated; the caller decides whether to treat that
		// as a failure.
		return res, waitErr
	}
	return res, nil
}
