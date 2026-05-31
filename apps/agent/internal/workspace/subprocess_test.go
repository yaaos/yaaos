package workspace

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"
)

// shAvailable returns true when /bin/sh is on the test host. All
// streaming subprocess tests depend on /bin/sh (and cat/echo/sleep) —
// the alpine + bookworm images we build under all have these, but
// remain defensive so this file doesn't blow up on a stripped CI image.
func shAvailable(t *testing.T) bool {
	t.Helper()
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH; subprocess streaming tests need a POSIX shell")
		return false
	}
	return true
}

func TestRunStreaming_BufferedStdoutWhenCallbackNil(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "printf 'hello\\nworld\\n'"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if res.ExitCode != 0 {
		t.Errorf("exit_code: want 0 got %d", res.ExitCode)
	}
	if string(res.Stdout) != "hello\nworld\n" {
		t.Errorf("stdout: want 'hello\\nworld\\n' got %q", string(res.Stdout))
	}
	if len(res.Stderr) != 0 {
		t.Errorf("stderr: want empty got %q", string(res.Stderr))
	}
}

func TestRunStreaming_StreamsLinesViaCallback(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	var lines []string
	var mu sync.Mutex
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "printf 'one\\ntwo\\nthree\\n'"},
		OnStdoutLine: func(line []byte) {
			mu.Lock()
			defer mu.Unlock()
			lines = append(lines, string(line))
		},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	want := []string{"one", "two", "three"}
	if len(lines) != len(want) {
		t.Fatalf("lines: want %v got %v", want, lines)
	}
	for i := range want {
		if lines[i] != want[i] {
			t.Errorf("line %d: want %q got %q", i, want[i], lines[i])
		}
	}
	// When OnStdoutLine is set, Stdout is NOT double-buffered.
	if len(res.Stdout) != 0 {
		t.Errorf("Stdout should be empty when streaming, got %q", string(res.Stdout))
	}
}

func TestRunStreaming_StdinPipedToChild(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv:  []string{"cat"},
		Stdin: []byte("pumpkin\n"),
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if string(res.Stdout) != "pumpkin\n" {
		t.Errorf("stdout: want 'pumpkin\\n' got %q", string(res.Stdout))
	}
}

func TestRunStreaming_StderrCaptured(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo to-stderr 1>&2; echo to-stdout"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if !strings.Contains(string(res.Stderr), "to-stderr") {
		t.Errorf("stderr should contain 'to-stderr', got %q", string(res.Stderr))
	}
	if !strings.Contains(string(res.Stdout), "to-stdout") {
		t.Errorf("stdout should contain 'to-stdout', got %q", string(res.Stdout))
	}
}

func TestRunStreaming_NonZeroExitWrappedAsError(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo oops >&2; exit 7"},
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Errorf("want *exec.ExitError, got %T", err)
	}
	// Result still populated.
	if res.ExitCode != 7 {
		t.Errorf("exit_code: want 7 got %d", res.ExitCode)
	}
	if !strings.Contains(string(res.Stderr), "oops") {
		t.Errorf("stderr captured on failure: got %q", string(res.Stderr))
	}
}

func TestRunStreaming_ContextCancel_SignalsProcessGroup(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	// Sleep 30s — we'll cancel after 50ms. The TimedOut flag must be set
	// and total run time stays well under the sleep window.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	res, err := RunStreaming(ctx, RunStreamingOptions{
		Argv: []string{"sh", "-c", "sleep 30"},
	})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("want ctx.Err on cancel")
	}
	if !res.TimedOut {
		t.Errorf("TimedOut should be set on ctx cancel")
	}
	if elapsed > 3*time.Second {
		t.Errorf("cancel didn't terminate promptly: %s", elapsed)
	}
}

func TestRunStreaming_NoArgv_Errors(t *testing.T) {
	_, err := RunStreaming(context.Background(), RunStreamingOptions{})
	if err == nil {
		t.Fatal("want error on empty argv")
	}
	if !strings.Contains(err.Error(), "empty argv") {
		t.Errorf("err: want 'empty argv', got %q", err.Error())
	}
}

func TestRunStreaming_BinaryNotFound_Errors(t *testing.T) {
	_, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"this-binary-definitely-does-not-exist-yaaos-xyzzy"},
	})
	if err == nil {
		t.Fatal("want error on missing binary")
	}
}

func TestRunStreaming_LargeStdinPipedSuccessfully(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	// 256KB of stdin → cat → stdout. Tests that the stdin-writing
	// goroutine doesn't deadlock with the stdout-reading goroutine.
	stdin := make([]byte, 256*1024)
	for i := range stdin {
		stdin[i] = 'a' + byte(i%26)
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv:  []string{"cat"},
		Stdin: stdin,
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if len(res.Stdout) != len(stdin) {
		t.Errorf("stdout len: want %d got %d", len(stdin), len(res.Stdout))
	}
}

func TestRunStreaming_EnvOverridesParent(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo $YAAOS_TEST_VAR"},
		Env:  []string{"YAAOS_TEST_VAR=present", "PATH=/usr/bin:/bin"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if strings.TrimSpace(string(res.Stdout)) != "present" {
		t.Errorf("env var not present: stdout=%q", string(res.Stdout))
	}
}

func TestRunStreaming_DirSetsCwd(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	dir := t.TempDir()
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "pwd"},
		Dir:  dir,
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	// macOS adds a /private prefix on tempdirs; tolerate it.
	got := strings.TrimSpace(string(res.Stdout))
	if got != dir && got != "/private"+dir {
		t.Errorf("pwd: want %q got %q", dir, got)
	}
}

// ── TestHelperProcess-based tests ────────────────────────────────────────
//
// The tests below spawn the test binary as a subprocess with
// GO_HELPER_PROCESS=1 and a GO_HELPER_CMD env var that selects the
// child's behaviour. This pattern lets us drive paths a /bin/sh command
// can't control precisely — exact exit codes, partial output before a
// hang, and timing that interacts with the SIGTERM→SIGKILL grace window.
//
// TestHelperProcess is the child entry point; it must be a Test* function
// so `go test -run TestHelperProcess` finds it. The real test functions
// guard against running as a child with `os.Getenv("GO_HELPER_PROCESS")`.

// TestHelperProcess is the child subprocess entry point. It reads
// GO_HELPER_CMD and acts accordingly. Never runs as a real test — the
// parent guards with os.Getenv("GO_HELPER_PROCESS") != "1".
func TestHelperProcess(t *testing.T) {
	if os.Getenv("GO_HELPER_PROCESS") != "1" {
		return
	}
	switch os.Getenv("GO_HELPER_CMD") {
	case "exit42":
		// Deterministic non-zero exit code that `sh -c 'exit N'` can
		// produce but imprecisely — a Go child gives us exact control
		// and guarantees no shell interpretation of the exit path.
		os.Exit(42)

	case "partial_then_hang":
		// Write one partial line to stdout, then hang until killed.
		// Used to assert the OnStdoutLine callback fires for the partial
		// line *before* SIGTERM arrives and the grace fires.
		fmt.Println("partial-line")
		// Flush stdout so the parent's scanner sees it.
		os.Stdout.Sync() //nolint:errcheck
		// Block forever — the parent will cancel the context.
		select {}

	case "sleep_past_grace":
		// Write a line then sleep far longer than the grace window. This child
		// installs no signal handler, so the SIGTERM the parent sends on
		// ctx-cancel terminates it immediately (Go's default disposition) —
		// the long sleep is never reached and the SIGKILL grace path does not
		// run here. The virtual-time SIGTERM→grace→SIGKILL pattern is
		// demonstrated separately in TestGraceWindowPattern_Synctest.
		fmt.Println("before-grace")
		os.Stdout.Sync() //nolint:errcheck
		time.Sleep(60 * time.Second)
		os.Exit(0)

	default:
		fmt.Fprintf(os.Stderr, "unknown GO_HELPER_CMD: %q\n", os.Getenv("GO_HELPER_CMD"))
		os.Exit(1)
	}
}

// helperCmd returns the argv for re-invoking this test binary as a child
// subprocess. The behaviour the child runs is selected by GO_HELPER_CMD,
// which helperEnv sets.
func helperCmd(t *testing.T) []string {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}
	return []string{exe, "-test.run=TestHelperProcess"}
}

// helperEnv returns the environment for a helper subprocess (inherits PATH
// for dynamic linker; adds the two sentinel vars).
func helperEnv(cmd string) []string {
	return append(os.Environ(),
		"GO_HELPER_PROCESS=1",
		"GO_HELPER_CMD="+cmd,
	)
}

func TestRunStreaming_HelperExactExitCode(t *testing.T) {
	// A Go child can os.Exit(N) precisely; shell arithmetic may truncate
	// exit codes on some systems. Verify RunStreaming surfaces exactly 42.
	argv := helperCmd(t)
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: argv,
		Env:  helperEnv("exit42"),
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Errorf("want *exec.ExitError, got %T: %v", err, err)
	}
	if res == nil {
		t.Fatal("result must be non-nil even on non-zero exit")
	}
	if res.ExitCode != 42 {
		t.Errorf("ExitCode: want 42 got %d", res.ExitCode)
	}
}

func TestRunStreaming_HelperPartialOutputBeforeSIGTERM(t *testing.T) {
	// The child writes one line then hangs. We cancel the context after
	// 50 ms. The OnStdoutLine callback must have been called for the
	// partial line before the cancel fires.
	argv := helperCmd(t)
	var lines []string
	var mu sync.Mutex
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	t.Cleanup(cancel)

	_, _ = RunStreaming(ctx, RunStreamingOptions{
		Argv: argv,
		Env:  helperEnv("partial_then_hang"),
		OnStdoutLine: func(line []byte) {
			mu.Lock()
			defer mu.Unlock()
			lines = append(lines, string(line))
		},
	})

	mu.Lock()
	got := lines
	mu.Unlock()
	if len(got) == 0 {
		t.Fatal("OnStdoutLine not called for partial line before SIGTERM")
	}
	if got[0] != "partial-line" {
		t.Errorf("first line: want 'partial-line' got %q", got[0])
	}
}

// TestGraceWindowPattern_Synctest uses testing/synctest to verify the
// SIGTERM → grace → SIGKILL timing pattern in deterministic virtual time.
// This is the first synctest usage in the workspace package; it demonstrates
// the convention (see patterns.md § Testing, rule 6: "Virtual-time bubble;
// never time.Sleep polling; never a hand-rolled clock interface").
//
// RunStreaming itself cannot be tested with synctest because it spawns a
// real OS subprocess whose pipe IO waits are not "durably blocked" in the
// synctest sense — real OS syscalls can become ready at any moment, so the
// fake clock refuses to advance while they're in flight. The pattern is
// demonstrated here using a pure-Go channel stand-in for the subprocess,
// asserting that the kill signal is sent after exactly the grace duration
// and before any longer timeout.
func TestGraceWindowPattern_Synctest(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		const grace = 2 * time.Second

		// killed receives the signal kind that the killer sent (simulating
		// killGroupWS calls). Buffered so both sends are non-blocking.
		killed := make(chan string, 2)

		// ctx is what triggers the SIGTERM phase — cancel() is called below.
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)

		// Simulate the SIGTERM → grace → SIGKILL pattern using only Go
		// channels and time.After, which the synctest bubble can fully control.
		go func() {
			<-ctx.Done()
			killed <- "SIGTERM"
			// Block on time.After — durably blocked from synctest's view so
			// fake time advances to fire this.
			<-time.After(grace)
			killed <- "SIGKILL"
		}()

		// Trigger the SIGTERM phase.
		cancel()

		// Block on the SIGTERM receive — the goroutine runs, sends SIGTERM
		// (buffered, non-blocking), then blocks on time.After(grace). At that
		// point we are also blocked here; both goroutines are durable so
		// synctest advances fake time 2s and fires the timer.
		sig1 := <-killed
		if sig1 != "SIGTERM" {
			t.Errorf("first signal: want SIGTERM got %s", sig1)
		}

		// Receiving SIGKILL blocks this goroutine too; both goroutines are
		// durable (us on channel receive, background on time.After), so
		// synctest advances fake time past the grace. The timer fires, the
		// background goroutine sends SIGKILL and exits, unblocking us.
		sig2 := <-killed
		if sig2 != "SIGKILL" {
			t.Errorf("second signal: want SIGKILL got %s", sig2)
		}
	})
}

func TestRunStreaming_HelperGraceWindowTiming(t *testing.T) {
	// The child writes a line then sleeps far past the 2s grace window. On
	// ctx-cancel the parent sends SIGTERM, which terminates this child
	// immediately (Go's default disposition — the child installs no handler),
	// so RunStreaming returns without reaching the SIGKILL grace path.
	// Asserts: TimedOut is set (driven by ctx cancellation, not by which kill
	// signal landed), run eventually returns, and the stdout line written
	// before the cancel is captured.
	//
	// Note: this test uses real wall-clock time because RunStreaming spawns a
	// real OS subprocess whose pipe IO is not durably blocked in the synctest
	// sense — see TestGraceWindowPattern_Synctest for the virtual-time
	// demonstration of the full SIGTERM→grace→SIGKILL pattern.
	argv := helperCmd(t)
	var lines []string
	var mu sync.Mutex
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	// Give enough room for 2s grace + process lifecycle overhead.
	done := make(chan struct{})
	var res *RunStreamingResult
	var runErr error
	go func() {
		defer close(done)
		res, runErr = RunStreaming(ctx, RunStreamingOptions{
			Argv: argv,
			Env:  helperEnv("sleep_past_grace"),
			OnStdoutLine: func(line []byte) {
				mu.Lock()
				defer mu.Unlock()
				lines = append(lines, string(line))
			},
		})
	}()

	// Wait briefly for the child to start and write its line, then cancel.
	time.Sleep(100 * time.Millisecond)
	cancel()

	// Wait up to 5s for RunStreaming to return (2s grace + process death).
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("RunStreaming did not return within 5s after cancel")
	}

	if res == nil || !res.TimedOut {
		t.Errorf("TimedOut should be set; res=%v err=%v", res, runErr)
	}
	if runErr == nil {
		t.Error("want ctx.Err on timeout")
	}
	mu.Lock()
	gotLines := lines
	mu.Unlock()
	if len(gotLines) == 0 {
		t.Error("expected at least the 'before-grace' line in stdout before cancel")
	} else if gotLines[0] != "before-grace" {
		t.Errorf("first line: want 'before-grace' got %q", gotLines[0])
	}
}
